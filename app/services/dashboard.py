"""Numbers for the dashboard: money in, money out, what needs attention."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.models import Approval, Invoice, Payout
from app.services.actions import ActionService
from app.services.ml import MLScorers
from app.services.store import days_overdue, load_policy, spend


def _totals(pairs) -> dict[str, str]:
    out: dict[str, Decimal] = defaultdict(Decimal)
    for currency, amount in pairs:
        out[currency] += amount
    return {k: f"{v:.2f}" for k, v in sorted(out.items())}


def _pct(part: Decimal, whole: Decimal) -> int:
    return min(100, int(part * 100 / whole)) if whole > 0 else 0


def dashboard(actions: ActionService, ml: MLScorers) -> dict[str, Any]:
    now = actions.clock()
    today = now.date()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with actions.sessions() as s:
        policy = load_policy(s)
        spent = spend(s, "USD", now)
        invoices = s.scalars(select(Invoice).where(Invoice.status.in_(("SENT", "PAID")))).all()
        open_invoices = [i for i in invoices if i.status == "SENT"]
        overdue = [i for i in open_invoices if days_overdue(i, today) > 0]
        paid_month = [
            i for i in invoices if i.status == "PAID" and i.paid_at and i.paid_at >= month_start
        ]
        pending = s.scalars(select(Approval).where(Approval.decision.is_(None))).all()
        flagged = s.scalars(
            select(Payout)
            .where(Payout.anomaly_flag.is_(True))
            .order_by(Payout.created_at.desc())
            .limit(5)
        ).all()
        at_risk = sorted(
            (i for i in open_invoices if i.risk_score is not None),
            key=lambda i: i.risk_score,
            reverse=True,
        )[:5]
        return {
            "as_of": now.isoformat(),
            "outstanding": _totals((i.currency, i.amount) for i in open_invoices),
            "outstanding_count": len(open_invoices),
            "overdue": _totals((i.currency, i.amount) for i in overdue),
            "overdue_count": len(overdue),
            "paid_this_month": _totals((i.currency, i.amount) for i in paid_month),
            "paid_this_month_count": len(paid_month),
            "spend": {
                "currency": "USD",
                "today": f"{spent.today:.2f}",
                "daily_cap": f"{policy.daily_cap:.2f}",
                "today_pct": _pct(spent.today, policy.daily_cap),
                "month": f"{spent.month:.2f}",
                "monthly_cap": f"{policy.monthly_cap:.2f}",
                "month_pct": _pct(spent.month, policy.monthly_cap),
                "auto_approve_limit": f"{policy.auto_approve_limit:.2f}",
            },
            "pending_approvals": len(pending),
            "escalated_approvals": sum(1 for a in pending if a.level == "escalated"),
            "at_risk": [
                {
                    "id": i.id,
                    "client": i.client.name,
                    "number": i.number,
                    "amount": f"{i.amount:.2f}",
                    "currency": i.currency,
                    "due_date": i.due_date.isoformat(),
                    "days_overdue": days_overdue(i, today),
                    "risk": round(i.risk_score, 2),
                    "reasons": i.risk_reasons or [],
                }
                for i in at_risk
            ],
            "anomalies": [
                {
                    "recipient": p.recipient.email,
                    "amount": f"{p.amount:.2f}",
                    "currency": p.currency,
                    "reason": p.anomaly_reason,
                    "score": p.anomaly_score,
                    "created_at": p.created_at.isoformat(),
                }
                for p in flagged
            ],
            "insights": ml.insights(s, now),
            "ml": ml.status,
        }
