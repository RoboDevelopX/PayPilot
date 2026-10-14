"""Feature code shared by training (ml/*) and serving (app/services/ml.py).

Each model has one function that turns raw history into a feature vector, and
both the training scripts and the app call that same function, so the model
never sees features computed one way in training and another way in the app.

No leakage: features for an invoice issued at time t use only what was known
at t. A prior invoice counts as "paid late" only if it was paid before t; one
still unpaid at t counts as "open and overdue" if its due date had passed.
Payout features likewise look only at payouts made before the one scored.

Amounts are floats here: these are model inputs, not money being moved.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pandas is only needed for training, not by the running app
    import pandas as pd

# ------------------------------------------------------------ late-payment risk
RISK_FEATURES = (
    "log_amount",
    "terms_days",
    "prior_count",
    "prior_late_rate",
    "prior_mean_days_late",
    "prior_open_overdue",
    "weekday",
    "month_end",
)
# A client's late rate is shrunk toward PRIOR_LATE_RATE as if they had
# PRIOR_WEIGHT extra invoices, so one late payment from a new client does not
# read as "always late".
PRIOR_LATE_RATE = 0.3
PRIOR_WEIGHT = 2.0


@dataclass(frozen=True)
class PriorInvoice:
    issued_at: dt.datetime
    due_date: dt.date
    paid_at: dt.datetime | None  # None: not paid (yet)


def invoice_features(
    amount: float, terms_days: int, issued_at: dt.datetime, history: Sequence[PriorInvoice]
) -> dict[str, float]:
    """Features for an invoice issued at `issued_at`, given the client's other invoices."""
    n = late = open_overdue = 0
    days_late_total = 0
    today = issued_at.date()
    for h in history:
        if h.issued_at >= issued_at:
            continue  # did not exist yet
        if h.paid_at is not None and h.paid_at < issued_at:
            n += 1
            d = (h.paid_at.date() - h.due_date).days
            if d > 0:
                late += 1
                days_late_total += d
        elif h.due_date < today:
            open_overdue += 1  # unpaid at time t and already past due
    return {
        "log_amount": math.log1p(amount),
        "terms_days": float(terms_days),
        "prior_count": float(n),
        "prior_late_rate": (late + PRIOR_LATE_RATE * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT),
        "prior_mean_days_late": days_late_total / n if n else 0.0,
        "prior_open_overdue": float(open_overdue),
        "weekday": float(issued_at.weekday()),
        "month_end": float(issued_at.day >= 25),
    }


def prior_late_count(features: dict[str, float]) -> int:
    """Undo the shrinkage to recover how many past invoices were paid late."""
    n = features["prior_count"]
    return round(features["prior_late_rate"] * (n + PRIOR_WEIGHT) - PRIOR_LATE_RATE * PRIOR_WEIGHT)


def _ts(value: str | None) -> dt.datetime | None:
    if value is None or (isinstance(value, float) and math.isnan(value)) or value == "":
        return None
    return dt.datetime.fromisoformat(value)


def risk_frame(invoices: pd.DataFrame) -> pd.DataFrame:
    """Training rows: one per invoice, features from that client's earlier invoices only."""
    import pandas as pd

    rows = []
    for _, group in invoices.groupby("client_id", sort=False):
        history = [
            PriorInvoice(_ts(r.issued_at), dt.date.fromisoformat(r.due_date), _ts(r.paid_at))
            for r in group.itertuples()
        ]
        for r in group.itertuples():
            f = invoice_features(r.amount, r.terms_days, _ts(r.issued_at), history)
            rows.append({"invoice_id": r.invoice_id, "issued_at": r.issued_at, **f, "late": r.late})
    return pd.DataFrame(rows).sort_values(["issued_at", "invoice_id"]).reset_index(drop=True)


# -------------------------------------------------------------- payout anomaly
ANOMALY_FEATURES = (
    "log_amount",
    "recipient_ratio",
    "user_ratio",
    "is_new_recipient",
    "recipient_24h",
    "user_1h",
)
DAY = 86400.0
HOUR = 3600.0


def payout_features(
    amount: float,
    at: float,
    recipient_times: np.ndarray,
    recipient_amounts: np.ndarray,
    all_times: np.ndarray,
    all_amounts: np.ndarray,
) -> dict[str, float]:
    """Features for a payout at POSIX time `at`.

    The histories are (times, amounts) of earlier payouts to this recipient and
    to anyone; entries at or after `at` are ignored.
    """
    mine = recipient_times < at
    everyone = all_times < at
    r_amounts = recipient_amounts[mine]
    u_amounts = all_amounts[everyone]
    return {
        "log_amount": math.log1p(amount),
        # log(amount / typical): 0 when there is nothing to compare with
        "recipient_ratio": (
            math.log(amount / float(np.median(r_amounts))) if r_amounts.size else 0.0
        ),
        "user_ratio": math.log(amount / float(np.median(u_amounts))) if u_amounts.size else 0.0,
        "is_new_recipient": float(r_amounts.size == 0),
        "recipient_24h": float(np.count_nonzero(recipient_times[mine] >= at - DAY)),
        "user_1h": float(np.count_nonzero(all_times[everyone] >= at - HOUR)),
    }


def anomaly_frame(payouts: pd.DataFrame) -> pd.DataFrame:
    """Training rows: one per payout, features from earlier payouts only."""
    import pandas as pd

    df = payouts.sort_values(["at", "payout_id"]).reset_index(drop=True)
    times = df["at"].to_numpy(dtype=float)
    amounts = df["amount"].to_numpy(dtype=float)
    by_recipient = df.groupby("recipient_id").indices
    rows = []
    for i, r in enumerate(df.itertuples()):
        idx = by_recipient[r.recipient_id]
        idx = idx[idx < i]
        f = payout_features(r.amount, r.at, times[idx], amounts[idx], times[:i], amounts[:i])
        rows.append(
            {
                "payout_id": r.payout_id,
                "at": r.at,
                **f,
                "anomaly": r.anomaly,
                "anomaly_type": r.anomaly_type,
            }
        )
    return pd.DataFrame(rows)


# ------------------------------------------------------------------- splits
FIT_FRACTION = 0.7
HOLDOUT_FRACTION = 0.1  # calibration (risk) or threshold choice (anomaly)


def time_split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Chronological split of a time-sorted frame: fit 70%, holdout 10%, test 20%.

    Testing on the most recent rows mimics real use: the model is always judged
    on data from after everything it learned from.
    """
    n = len(frame)
    a, b = int(n * FIT_FRACTION), int(n * (FIT_FRACTION + HOLDOUT_FRACTION))
    return frame.iloc[:a], frame.iloc[a:b], frame.iloc[b:]
