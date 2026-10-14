"""Load the trained models and turn feature vectors into scores with reasons.

Both models return a plain-English reason with every score, because a person
approving a payout or choosing reminder terms should see *why*, not just a
number. The app calls these through app/services/ml.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from ml.features import ANOMALY_FEATURES, RISK_FEATURES, prior_late_count

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
RISK_ARTIFACT = ARTIFACTS_DIR / "risk_model.joblib"
ANOMALY_ARTIFACT = ARTIFACTS_DIR / "anomaly_model.joblib"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MIN_CONTRIBUTION = 0.02  # a factor must move the probability by 2 points to be named


def _money(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if currency == "USD" else f"{amount:,.2f} {currency}"


def _times(ratio: float) -> str:
    return f"{ratio:.0f}x" if ratio >= 3 else f"{ratio:.1f}x"


# ---------------------------------------------------------------- risk model
@dataclass
class RiskModel:
    artifact: dict[str, Any]

    @classmethod
    def load(cls, path: Path = RISK_ARTIFACT) -> RiskModel:
        artifact = joblib.load(path)
        if (
            artifact.get("kind") != "late_payment_risk"
            or tuple(artifact["features"]) != RISK_FEATURES
        ):
            raise ValueError(f"{path} is not a compatible late-payment risk model")
        return cls(artifact)

    def predict(self, features: dict[str, float], currency: str = "USD") -> tuple[float, list[str]]:
        """Calibrated probability that the invoice is paid late, and up to two reasons."""
        x = np.array([[features[f] for f in RISK_FEATURES]], dtype=float)
        p = float(self.artifact["model"].predict_proba(x)[0, 1])
        return p, self.reasons(features, p, currency)

    def contributions(self, features: dict[str, float]) -> dict[str, float]:
        """Occlusion: how much each feature moves the (uncalibrated) probability
        compared with a typical value for it. Positive means it raises the risk."""
        x = np.array([features[f] for f in RISK_FEATURES], dtype=float)
        ref = self.artifact["reference"]
        rows = np.tile(x, (len(RISK_FEATURES) + 1, 1))
        for j, f in enumerate(RISK_FEATURES):
            rows[j + 1, j] = ref[f]
        p = self.artifact["base"].predict_proba(rows)[:, 1]
        return {f: float(p[0] - p[j + 1]) for j, f in enumerate(RISK_FEATURES)}

    def reasons(self, features: dict[str, float], p: float, currency: str = "USD") -> list[str]:
        contrib = self.contributions(features)
        raising = p >= self.artifact["train_base_rate"]
        ranked = sorted(contrib.items(), key=lambda kv: kv[1], reverse=raising)
        out: list[str] = []
        for name, c in ranked:
            if (c if raising else -c) < MIN_CONTRIBUTION:
                break
            text = _risk_reason(name, features, raising, currency)
            if text and text not in out:
                out.append(text)
            if len(out) == 2:
                break
        if not out:
            out.append("similar invoices were often paid late" if raising else "no warning signs")
        return out


def _risk_reason(name: str, f: dict[str, float], raising: bool, currency: str) -> str | None:
    n = int(f["prior_count"])
    if name in ("prior_late_rate", "prior_mean_days_late") and n:
        late = prior_late_count(f)
        if not raising:
            return f"paid {n - late} of {n} past invoices on time"
        if name == "prior_mean_days_late" and f["prior_mean_days_late"] >= 1:
            return f"pays {f['prior_mean_days_late']:.0f} days late on average"
        return f"paid {late} of {n} past invoices late"
    if name == "prior_count" and raising and n < 3:
        return "little payment history with this client"
    if name == "prior_open_overdue" and raising and f["prior_open_overdue"] >= 1:
        k = int(f["prior_open_overdue"])
        return f"{k} earlier invoice{'s are' if k > 1 else ' is'} still unpaid past due"
    if name == "terms_days":
        t = int(f["terms_days"])
        return f"long payment terms (net-{t})" if raising else f"short payment terms (net-{t})"
    if name == "log_amount":
        amount = _money(math.expm1(f["log_amount"]), currency)
        return f"a large amount ({amount})" if raising else f"a small amount ({amount})"
    if name == "month_end" and raising and f["month_end"]:
        return "issued at month end"
    if name == "weekday" and raising:
        return f"issued on a {WEEKDAYS[int(f['weekday'])]}"
    return None


# ------------------------------------------------------------- anomaly model
@dataclass
class AnomalyModel:
    artifact: dict[str, Any]

    @classmethod
    def load(cls, path: Path = ANOMALY_ARTIFACT) -> AnomalyModel:
        artifact = joblib.load(path)
        if (
            artifact.get("kind") != "payout_anomaly"
            or tuple(artifact["features"]) != ANOMALY_FEATURES
        ):
            raise ValueError(f"{path} is not a compatible payout anomaly model")
        return cls(artifact)

    @property
    def threshold(self) -> float:
        return float(self.artifact["threshold"])

    def score(self, features: dict[str, float]) -> float:
        """Isolation Forest anomaly score in (0, 1): about 0.5 is ordinary, near 1 is isolated."""
        x = np.array([[features[f] for f in ANOMALY_FEATURES]], dtype=float)
        return float(-self.artifact["model"].score_samples(x)[0])

    def tail_probabilities(self, features: dict[str, float]) -> dict[str, float]:
        """Share of training payouts at least this extreme, per feature (high = unusual)."""
        out = {}
        for f in ANOMALY_FEATURES:
            seen = self.artifact["tails"][f]
            out[f] = float(seen.size - np.searchsorted(seen, features[f], side="left")) / seen.size
        return out

    def predict(
        self, features: dict[str, float], amount: float, currency: str = "USD"
    ) -> tuple[bool, float, str | None]:
        """(flagged, score, reason). The reason names the most unusual feature."""
        s = self.score(features)
        flagged = s >= self.threshold
        return flagged, s, self.reason(features, amount, currency) if flagged else None

    def reason(self, features: dict[str, float], amount: float, currency: str = "USD") -> str:
        tails = self.tail_probabilities(features)
        name = min(tails, key=tails.get)
        money = _money(amount, currency)
        new = features["is_new_recipient"] >= 1
        if name == "recipient_ratio":
            return f"{money} is {_times(math.exp(features['recipient_ratio']))} what you usually pay this recipient"
        if name in ("user_ratio", "log_amount", "is_new_recipient") and features[
            "user_ratio"
        ] > math.log(2):
            text = f"{money} is {_times(math.exp(features['user_ratio']))} your typical payout"
            return f"first payout to this recipient, and {text}" if new else text
        if name == "recipient_24h":
            return (
                f"payout number {int(features['recipient_24h']) + 1} to this recipient in 24 hours"
            )
        if name == "user_1h":
            return f"{int(features['user_1h']) + 1} payouts within one hour"
        if new:
            return "first payout to this recipient"
        pct = 100 * (1 - tails["log_amount"])
        return f"{money} is larger than {pct:.0f}% of past payouts"
