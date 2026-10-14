"""The app's side of the ML layer: database rows in, advisory signals out.

Features are built with the same functions the models were trained with
(ml.features), from what is in the database at scoring time. Both signals are
advisory: the anomaly flag can only escalate a payout to a careful human review,
and the risk score only changes when and how firmly a reminder goes out.

If the model files are missing or fail to load, PayPilot runs without ML, and a
scoring error never blocks an action: it is logged and the policy decides alone,
exactly as it would with ML switched off.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Client, Invoice, Payout, utcnow
from app.policy.engine import MLSignals
from ml.features import PriorInvoice, invoice_features, payout_features
from ml.predict import ANOMALY_ARTIFACT, RISK_ARTIFACT, AnomalyModel, RiskModel

log = logging.getLogger("paypilot.ml")

ADVICE_MIN_RISK = 0.5  # suggest shorter terms from this late-payment risk upward
ADVICE_TERMS = 7
ADVICE_MIN_GAIN = 0.03  # only suggest net-7 if it lowers the estimate by 3 points


def _load(loader, path: Path, what: str):
    if not path.exists():
        log.warning(
            "%s model not found at %s; run `make train`. Continuing without it.", what, path
        )
        return None
    try:
        return loader(path)
    except Exception:
        log.exception("could not load the %s model; continuing without it", what)
        return None


def _history(s: Session, client_id: str, exclude_id: str | None = None) -> list[PriorInvoice]:
    rows = s.scalars(
        select(Invoice).where(Invoice.client_id == client_id, Invoice.status.in_(("SENT", "PAID")))
    )
    out = []
    for inv in rows:
        if inv.id == exclude_id or (inv.status == "PAID" and inv.paid_at is None):
            continue  # skip the invoice being scored, and paid ones with no known date
        out.append(PriorInvoice(inv.issued_at, inv.due_date, inv.paid_at))
    return out


class MLScorers:
    def __init__(self, risk: RiskModel | None = None, anomaly: AnomalyModel | None = None) -> None:
        self.risk = risk
        self.anomaly = anomaly

    @classmethod
    def load(
        cls, risk_path: Path = RISK_ARTIFACT, anomaly_path: Path = ANOMALY_ARTIFACT
    ) -> MLScorers:
        return cls(
            _load(RiskModel.load, risk_path, "late-payment risk"),
            _load(AnomalyModel.load, anomaly_path, "payout anomaly"),
        )

    @property
    def status(self) -> str:
        parts = [name for name, m in (("risk", self.risk), ("anomaly", self.anomaly)) if m]
        return "+".join(parts) if parts else "off"

    # ---------------------------------------------------------- late payment
    def client_risk(
        self,
        s: Session,
        client_id: str,
        *,
        amount: float,
        terms_days: int,
        at: dt.datetime,
        currency: str = "USD",
        exclude_id: str | None = None,
    ) -> tuple[float, list[str]]:
        assert self.risk is not None
        f = invoice_features(amount, terms_days, at, _history(s, client_id, exclude_id))
        return self.risk.predict(f, currency)

    def score_invoice(self, s: Session, inv: Invoice) -> tuple[float | None, list[str] | None]:
        """RiskScorer for ActionService: late-payment probability for a new invoice."""
        if self.risk is None:
            return None, None
        try:
            p, reasons = self.client_risk(
                s,
                inv.client_id,
                amount=float(inv.amount),
                terms_days=inv.terms_days,
                at=inv.issued_at,
                currency=inv.currency,
                exclude_id=inv.id,
            )
            return round(p, 4), reasons
        except Exception:
            log.exception("risk scoring failed for invoice %s", inv.id)
            return None, None

    def terms_advice(
        self, s: Session, client_id: str, now: dt.datetime | None = None
    ) -> dict[str, Any] | None:
        """For a client likely to pay late, estimate what net-7 terms would change."""
        if self.risk is None:
            return None
        client = s.get(Client, client_id)
        invoices = s.scalars(select(Invoice).where(Invoice.client_id == client_id)).all()
        if client is None or not invoices:
            return None
        now = now or utcnow()
        amount = float(median(float(i.amount) for i in invoices))
        usual_terms = Counter(i.terms_days for i in invoices).most_common(1)[0][0]
        p, reasons = self.client_risk(s, client_id, amount=amount, terms_days=usual_terms, at=now)
        if p < ADVICE_MIN_RISK:
            return None
        advice: dict[str, Any] = {
            "client_id": client_id,
            "client": client.name,
            "risk": round(p, 2),
            "usual_terms": usual_terms,
            "reasons": reasons,
            "text": f"{client.name} is likely to pay late ({p:.0%} risk).",
        }
        if usual_terms > ADVICE_TERMS:
            p7, _ = self.client_risk(s, client_id, amount=amount, terms_days=ADVICE_TERMS, at=now)
            if p - p7 >= ADVICE_MIN_GAIN:
                advice["suggested_terms"] = ADVICE_TERMS
                advice["risk_with_suggestion"] = round(p7, 2)
                advice["text"] = (
                    f"{client.name} is likely to pay late ({p:.0%} risk); consider "
                    f"net-{ADVICE_TERMS} terms (estimated {p7:.0%})."
                )
        return advice

    def insights(self, s: Session, now: dt.datetime | None = None, limit: int = 3) -> list[dict]:
        """The clients most likely to pay late, riskiest first, for the dashboard."""
        if self.risk is None:
            return []
        client_ids = s.scalars(select(Invoice.client_id).distinct()).all()
        found = [a for cid in client_ids if (a := self.terms_advice(s, cid, now))]
        return sorted(found, key=lambda a: a["risk"], reverse=True)[:limit]

    # --------------------------------------------------------------- payouts
    def score_payout(self, s: Session, fields: dict[str, Any]) -> MLSignals:
        """AnomalyScorer for ActionService: compare a proposed payout with past ones."""
        if self.anomaly is None:
            return MLSignals()
        try:
            at: dt.datetime = fields["at"]
            rows = s.execute(
                select(Payout.recipient_id, Payout.created_at, Payout.amount).where(
                    Payout.currency == fields["currency"],
                    Payout.status != "failed",
                    Payout.created_at < at,
                )
            ).all()
            times = np.array([r.created_at.timestamp() for r in rows], dtype=float)
            amounts = np.array([float(r.amount) for r in rows], dtype=float)
            mine = np.array([r.recipient_id == fields["recipient_id"] for r in rows], dtype=bool)
            amount = float(fields["amount"])
            f = payout_features(amount, at.timestamp(), times[mine], amounts[mine], times, amounts)
            flagged, score, reason = self.anomaly.predict(f, amount, fields["currency"])
            return MLSignals(
                anomaly_flag=flagged, anomaly_score=round(score, 3), anomaly_reason=reason
            )
        except Exception:
            log.exception("anomaly scoring failed; the policy decides without it")
            return MLSignals()
