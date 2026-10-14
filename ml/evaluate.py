"""Evaluate both saved models on their untouched test windows and write the report.

Writes ml/reports/metrics.json and ml/reports/REPORT.md. Every number comes
from the most recent 20% of the SYNTHETIC data, which neither model saw while
being fitted, calibrated or thresholded. Baselines are reported next to each
model so the numbers can be read honestly, including where a simple rule does
about as well.

Run: python -m ml.evaluate
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)

from ml.features import risk_frame, time_split
from ml.generate_data import INVOICES_CSV, SEED
from ml.predict import AnomalyModel, RiskModel
from ml.train_anomaly import load_frame as anomaly_data
from ml.train_anomaly import matrix as anomaly_matrix
from ml.train_risk import matrix as risk_matrix

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
METRICS_JSON = REPORTS_DIR / "metrics.json"
REPORT_MD = REPORTS_DIR / "REPORT.md"
BOOTSTRAP = 1000


def _r(x: float) -> float:
    return round(float(x), 4)


def bootstrap_ci(y: np.ndarray, score: np.ndarray, metric) -> list[float]:
    """95% percentile interval of a metric over resampled test sets."""
    rng = np.random.default_rng(SEED)
    values = []
    for _ in range(BOOTSTRAP):
        idx = rng.integers(0, len(y), len(y))
        if y[idx].min() == y[idx].max():
            continue
        values.append(metric(y[idx], score[idx]))
    return [_r(np.percentile(values, 2.5)), _r(np.percentile(values, 97.5))]


def probability_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    return {
        "roc_auc": _r(roc_auc_score(y, p)),
        "pr_auc": _r(average_precision_score(y, p)),
        "brier": _r(brier_score_loss(y, p)),
        "log_loss": _r(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))),
    }


def calibration_bins(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        if mask.any():
            out.append(
                {
                    "bin": f"{lo:.1f}-{hi:.1f}",
                    "count": int(mask.sum()),
                    "mean_predicted": _r(p[mask].mean()),
                    "observed_late_rate": _r(y[mask].mean()),
                }
            )
    return out


def evaluate_risk() -> dict:
    model = RiskModel.load()
    art = model.artifact
    _, _, test = time_split(risk_frame(pd.read_csv(INVOICES_CSV)))
    y = test["late"].to_numpy()
    x = risk_matrix(test)
    p = art["model"].predict_proba(x)[:, 1]
    base_rate = art["train_base_rate"]
    history = test["prior_late_rate"].to_numpy()
    return {
        "test_rows": len(test),
        "test_late_rate": _r(y.mean()),
        "test_from": art["split"]["test_from"],
        "chosen": art["chosen"],
        "calibration": art["calibration"],
        "validation_roc_auc": {k: _r(v) for k, v in art["validation_roc_auc"].items()},
        "test": probability_metrics(y, p),
        "test_roc_auc_95ci": bootstrap_ci(y, p, roc_auc_score),
        "test_brier_95ci": bootstrap_ci(y, p, brier_score_loss),
        "all_candidates": {
            f"{name}+{method}": probability_metrics(y, cal.predict_proba(x)[:, 1])
            for name, by_method in art["candidates"].items()
            for method, cal in by_method.items()
        },
        "baselines": {
            "base_rate": {
                "description": f"predict the training late rate ({base_rate:.1%}) for every invoice",
                "roc_auc": 0.5,
                "pr_auc": _r(y.mean()),
                "brier": _r(brier_score_loss(y, np.full(len(y), base_rate))),
                "log_loss": _r(log_loss(y, np.full(len(y), base_rate))),
            },
            "client_history": {
                "description": "predict the client's own past late rate (shrunk toward 30%)",
                **probability_metrics(y, history),
            },
        },
        "calibration_bins": calibration_bins(y, p),
    }


def rule_flags(frame: pd.DataFrame) -> np.ndarray:
    """A hand-written rule that mirrors how the anomalies were injected."""
    return (
        (frame["recipient_ratio"] >= math.log(5))
        | ((frame["is_new_recipient"] == 1) & (frame["user_ratio"] >= math.log(4)))
        | (frame["recipient_24h"] >= 2)
    ).to_numpy()


def evaluate_anomaly() -> dict:
    model = AnomalyModel.load()
    _, _, test = time_split(anomaly_data())
    y = test["anomaly"].to_numpy()
    s = -model.artifact["model"].score_samples(anomaly_matrix(test))
    flagged = s >= model.threshold
    rule = rule_flags(test)
    types = test["anomaly_type"].to_numpy()
    return {
        "test_rows": len(test),
        "test_anomalies": int(y.sum()),
        "threshold": _r(model.threshold),
        "review_budget": model.artifact["review_budget"],
        "test": {
            "flagged": int(flagged.sum()),
            "flag_rate": _r(flagged.mean()),
            "precision": _r(precision_score(y, flagged)),
            "recall": _r(recall_score(y, flagged)),
            "f1": _r(f1_score(y, flagged)),
            "roc_auc": _r(roc_auc_score(y, s)),
            "pr_auc": _r(average_precision_score(y, s)),
        },
        "test_pr_auc_95ci": bootstrap_ci(y, s, average_precision_score),
        "recall_by_type": {
            t: {"count": int((types == t).sum()), "caught": int(flagged[types == t].sum())}
            for t in ("spike", "new_large", "burst")
        },
        "baselines": {
            "random": {
                "description": "flag at random; PR-AUC equals the anomaly rate",
                "pr_auc": _r(y.mean()),
            },
            "generator_rule": {
                "description": "hand-written rule mirroring how the anomalies were injected",
                "flagged": int(rule.sum()),
                "precision": _r(precision_score(y, rule)),
                "recall": _r(recall_score(y, rule)),
                "f1": _r(f1_score(y, rule)),
            },
        },
    }


def _table(rows: list[list], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def render_report(m: dict) -> str:
    r, a = m["late_payment_risk"], m["payout_anomaly"]
    chosen = f"{r['chosen']}+{r['calibration']}"
    risk_rows = [
        [f"**{k} (used)**" if k == chosen else k, *v.values()]
        for k, v in r["all_candidates"].items()
    ] + [
        [
            "baseline: client's past late rate",
            *[
                r["baselines"]["client_history"][k]
                for k in ("roc_auc", "pr_auc", "brier", "log_loss")
            ],
        ],
        [
            "baseline: base rate",
            *[r["baselines"]["base_rate"][k] for k in ("roc_auc", "pr_auc", "brier", "log_loss")],
        ],
    ]
    bins = [
        [b["bin"], b["count"], b["mean_predicted"], b["observed_late_rate"]]
        for b in r["calibration_bins"]
    ]
    t, g = a["test"], a["baselines"]["generator_rule"]
    anomaly_rows = [
        ["**Isolation Forest**", t["flagged"], t["precision"], t["recall"], t["f1"], t["pr_auc"]],
        ["baseline: generator rule", g["flagged"], g["precision"], g["recall"], g["f1"], "n/a"],
        ["baseline: random", "n/a", "n/a", "n/a", "n/a", a["baselines"]["random"]["pr_auc"]],
    ]
    by_type = [[k, v["count"], v["caught"]] for k, v in a["recall_by_type"].items()]
    return f"""# PayPilot ML report (SYNTHETIC data)

Generated by `make train` (`python -m ml.evaluate`). All numbers are on the most
recent 20% of the synthetic data, which the models never saw while being fitted,
calibrated or thresholded. The data is invented (see `ml/MODEL_CARD.md`), so these
numbers show the pipeline works and is evaluated honestly; they say nothing about
real customers.

## Late-payment risk

Test window: {r['test_rows']} invoices issued from {r['test_from'][:10]}, {r['test_late_rate']:.1%} paid late.
Candidate picked on the calibration window (ROC-AUC {r['validation_roc_auc']}), before the test window was scored.

{_table(risk_rows, ["model", "ROC-AUC", "PR-AUC", "Brier", "log loss"])}

ROC-AUC 95% bootstrap interval: {r['test_roc_auc_95ci']}; Brier: {r['test_brier_95ci']}.

**Reading it honestly:** {_risk_reading(r)}

Calibration of the chosen model on the test window:

{_table(bins, ["predicted", "invoices", "mean predicted", "observed late rate"])}

## Payout anomaly detector

Test window: {a['test_rows']} payouts, {a['test_anomalies']} injected anomalies.
Threshold {a['threshold']} = the score that flags the top {a['review_budget']:.0%} of the
holdout window (a review budget, chosen without labels).

{_table(anomaly_rows, ["detector", "flagged", "precision", "recall", "F1", "PR-AUC"])}

ROC-AUC {t['roc_auc']}; PR-AUC 95% bootstrap interval {a['test_pr_auc_95ci']}.
With only {a['test_anomalies']} anomalies in the test window these numbers move a lot
between seeds; the interval shows how much.

{_table(by_type, ["injected type", "in test window", "flagged"])}

**Reading it honestly:** {_anomaly_reading(a)}
In PayPilot a flag only escalates a payout for a careful human review; it never
blocks or sends anything.
"""


def _risk_reading(r: dict) -> str:
    model, hist, base = r["test"], r["baselines"]["client_history"], r["baselines"]["base_rate"]
    gain = model["roc_auc"] - hist["roc_auc"]
    return (
        f"the model's Brier score is {model['brier']} against {base['brier']} for the base rate, "
        f"so its probabilities are informative. Most of the signal is the client's own payment "
        f"history: ranking invoices by each client's past late rate alone gives ROC-AUC "
        f"{hist['roc_auc']}, {abs(gain):.3f} {'below' if gain >= 0 else 'above'} the model. "
        f"The model adds calibrated probabilities and the smaller effects (terms, amount, "
        f'month end) behind advice such as "consider net-7 terms".'
    )


def _anomaly_reading(a: dict) -> str:
    t, g = a["test"], a["baselines"]["generator_rule"]
    weakest = min(
        a["recall_by_type"].items(), key=lambda kv: kv[1]["caught"] / max(kv[1]["count"], 1)
    )
    if g["f1"] >= t["f1"]:
        rule = (
            f"a hand-written rule that mirrors how the anomalies were injected scores F1 "
            f"{g['f1']} against the detector's {t['f1']}; the rule has the answer key, the "
            f"detector was never told it."
        )
    else:
        rule = (
            f"a hand-written rule that mirrors how the anomalies were injected flags "
            f"{g['flagged']} payouts (F1 {g['f1']}) against the detector's {t['flagged']} "
            f"(F1 {t['f1']}): knowing the recipe still over-flags busy recipients."
        )
    return (
        f"{rule} The hardest type for the detector is `{weakest[0]}` "
        f"({weakest[1]['caught']} of {weakest[1]['count']} flagged)."
    )


def main() -> None:
    metrics = {
        "synthetic_data": True,
        "late_payment_risk": evaluate_risk(),
        "payout_anomaly": evaluate_anomaly(),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    METRICS_JSON.write_text(json.dumps(metrics, indent=2) + "\n")
    REPORT_MD.write_text(render_report(metrics))
    r, a = metrics["late_payment_risk"], metrics["payout_anomaly"]
    print(
        f"risk: ROC-AUC {r['test']['roc_auc']} Brier {r['test']['brier']} "
        f"(base rate Brier {r['baselines']['base_rate']['brier']})\n"
        f"anomaly: precision {a['test']['precision']} recall {a['test']['recall']} "
        f"PR-AUC {a['test']['pr_auc']}\n"
        f"wrote {METRICS_JSON} and {REPORT_MD.name}"
    )


if __name__ == "__main__":
    main()
