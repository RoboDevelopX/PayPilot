"""Generate the SYNTHETIC datasets both models are trained and evaluated on.

Nothing here is real customer data. The generator invents a small business's
history so the models have something honest to learn from:

Invoices: about 60 clients, each with a hidden tendency to pay late. Whether an
invoice is paid late depends on that tendency plus the amount, the payment
terms (longer terms are paid late more often), month-end cash crunches and
Friday invoices. The model never sees the hidden tendency, only what a real
business could know: how the client paid before.

Payouts: about 120 recipients with their own typical amounts, appearing over
time. About 2% of rows are injected anomalies, labeled with their type, used
only to evaluate the (unsupervised) detector:
  spike      5-20x the recipient's usual amount
  new_large  a brand-new recipient receiving 4-12x the user's usual payout
  burst      three extra payouts to one recipient within 40 minutes

Run: python -m ml.generate_data   (writes ml/data/synthetic/*.csv, seed 42)
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
START = dt.datetime(2024, 10, 1, tzinfo=dt.UTC)
SPAN_DAYS = 730
DATA_DIR = Path(__file__).resolve().parent / "data" / "synthetic"
INVOICES_CSV = DATA_DIR / "invoices.csv"
PAYOUTS_CSV = DATA_DIR / "payouts.csv"

TERMS = np.array([7, 14, 30, 45, 60])
TERMS_P = [0.10, 0.30, 0.35, 0.15, 0.10]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def generate_invoices(
    n_invoices: int = 5000, n_clients: int = 60, seed: int = SEED
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    propensity = rng.normal(0.0, 1.0, n_clients)  # hidden: how late this client tends to pay
    typical = np.exp(rng.normal(np.log(800), 0.8, n_clients))
    activity = rng.gamma(1.5, 1.0, n_clients)
    activity /= activity.sum()
    preferred_terms = rng.choice(TERMS, size=n_clients, p=TERMS_P)

    n = n_invoices
    client = rng.choice(n_clients, size=n, p=activity)
    day = np.sort(rng.integers(0, SPAN_DAYS, n))
    minute = rng.integers(8 * 60, 18 * 60, n)
    terms = np.where(
        rng.random(n) < 0.8, preferred_terms[client], rng.choice(TERMS, size=n, p=TERMS_P)
    )
    amount = np.round(np.clip(typical[client] * np.exp(rng.normal(0, 0.45, n)), 20, 25000), 2)
    issued = [
        START + dt.timedelta(days=int(d), minutes=int(m)) for d, m in zip(day, minute, strict=True)
    ]
    weekday = np.array([t.weekday() for t in issued])
    month_end = np.array([t.day >= 25 for t in issued])

    logit = (
        -1.35
        + 1.25 * propensity[client]
        + 0.30 * np.log(amount / 800)
        + 0.025 * (terms - 14)
        + 0.35 * month_end
        + 0.25 * (weekday == 4)
        + rng.normal(0, 0.3, n)
    )
    late = rng.random(n) < _sigmoid(logit)
    days_late = 1 + np.floor(rng.gamma(1.5, 3 + 6 * _sigmoid(propensity[client]))).astype(int)
    days_early = rng.integers(0, terms + 1)  # paid this many days before the due date
    paid_hour = rng.integers(9, 21, n)

    rows = []
    for i in range(n):
        due = issued[i].date() + dt.timedelta(days=int(terms[i]))
        offset = int(days_late[i]) if late[i] else -int(days_early[i])
        paid_day = due + dt.timedelta(days=offset)
        paid_at = dt.datetime.combine(paid_day, dt.time(int(paid_hour[i])), tzinfo=dt.UTC)
        paid_at = max(paid_at, issued[i] + dt.timedelta(hours=1))
        rows.append(
            {
                "invoice_id": f"inv-{i:05d}",
                "client_id": f"client-{client[i]:02d}",
                "issued_at": issued[i].isoformat(),
                "amount": float(amount[i]),
                "terms_days": int(terms[i]),
                "due_date": due.isoformat(),
                "paid_at": paid_at.isoformat(),
                "days_late": (paid_at.date() - due).days,
                "late": int(paid_at.date() > due),
            }
        )
    return pd.DataFrame(rows)


def generate_payouts(
    n_normal: int = 6000, n_recipients: int = 120, seed: int = SEED + 1
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    typical = np.clip(np.exp(rng.normal(np.log(40), 0.6, n_recipients)), 5, 400)
    weight = 1.0 / (np.arange(n_recipients) + 1.0) ** 0.9  # a few regulars, a long tail
    rng.shuffle(weight)
    start_day = np.where(
        rng.random(n_recipients) < 0.2, 0, rng.integers(0, SPAN_DAYS - 30, n_recipients)
    )
    start_ts = START.timestamp()

    def at(day: float, seconds: float) -> int:
        return int(start_ts + day * 86400 + seconds)

    rows: list[dict] = []
    days = np.sort(rng.integers(0, SPAN_DAYS, n_normal))
    for d in days:
        active = start_day <= d
        p = weight * active
        r = int(rng.choice(n_recipients, p=p / p.sum()))
        amount = round(float(typical[r] * np.exp(rng.normal(0, 0.25))), 2)
        rows.append(
            {
                "at": at(d, rng.uniform(8 * 3600, 20 * 3600)),
                "recipient_id": f"rcpt-{r:03d}",
                "amount": max(amount, 1.0),
                "anomaly": 0,
                "anomaly_type": "",
            }
        )

    normal = list(rows)
    usual = float(np.median([r["amount"] for r in normal]))
    n_total_anomalies = round(0.02 * n_normal / 0.98)
    n_burst_groups = round(0.3 * n_total_anomalies / 3)
    n_new = round(0.3 * n_total_anomalies)
    n_spike = n_total_anomalies - n_new - 3 * n_burst_groups

    def pick_regular() -> dict:
        while True:  # a payout to a recipient who already has some history
            base = normal[int(rng.integers(len(normal) // 10, len(normal)))]
            r = int(base["recipient_id"].split("-")[1])
            if start_day[r] + 14 < (base["at"] - start_ts) / 86400:
                return base

    for _ in range(n_spike):
        base = pick_regular()
        r = int(base["recipient_id"].split("-")[1])
        rows.append(
            {
                "at": base["at"] + int(rng.integers(3600, 3 * 86400)),
                "recipient_id": base["recipient_id"],
                "amount": round(float(typical[r] * rng.uniform(5, 20)), 2),
                "anomaly": 1,
                "anomaly_type": "spike",
            }
        )
    for k in range(n_new):
        rows.append(
            {
                "at": at(int(rng.integers(30, SPAN_DAYS)), rng.uniform(8 * 3600, 20 * 3600)),
                "recipient_id": f"new-{k:03d}",
                "amount": round(usual * float(rng.uniform(4, 12)), 2),
                "anomaly": 1,
                "anomaly_type": "new_large",
            }
        )
    for _ in range(n_burst_groups):
        base = pick_regular()
        r = int(base["recipient_id"].split("-")[1])
        for offset in np.sort(rng.integers(120, 40 * 60, 3)):
            rows.append(
                {
                    "at": base["at"] + int(offset),
                    "recipient_id": base["recipient_id"],
                    "amount": round(float(typical[r] * np.exp(rng.normal(0, 0.25))), 2),
                    "anomaly": 1,
                    "anomaly_type": "burst",
                }
            )

    df = pd.DataFrame(rows).sort_values(["at", "recipient_id"], kind="stable")
    df = df.reset_index(drop=True)
    df.insert(0, "payout_id", [f"pay-{i:05d}" for i in range(len(df))])
    df["at_iso"] = [dt.datetime.fromtimestamp(t, dt.UTC).isoformat() for t in df["at"]]
    return df


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    invoices = generate_invoices()
    payouts = generate_payouts()
    invoices.to_csv(INVOICES_CSV, index=False)
    payouts.to_csv(PAYOUTS_CSV, index=False)
    print(
        f"SYNTHETIC data written to {DATA_DIR}:\n"
        f"  invoices.csv  {len(invoices)} rows, late rate {invoices['late'].mean():.1%}\n"
        f"  payouts.csv   {len(payouts)} rows, injected anomalies {payouts['anomaly'].mean():.1%}"
    )


if __name__ == "__main__":
    main()
