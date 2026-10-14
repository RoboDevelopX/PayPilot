"""Small query helpers shared by the services (policy, spend, lookups)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, func, or_, select
from sqlalchemy.orm import Session

from app.models import Client, Invoice, Payout, PolicyRow, Recipient, utcnow
from app.policy.engine import Policy, Spend

# Payouts in these states count toward the daily/monthly caps.
SPEND_STATUSES = ("approved", "executed")


def load_policy(session: Session) -> Policy:
    row = session.scalars(select(PolicyRow).order_by(PolicyRow.id.desc()).limit(1)).first()
    return Policy.from_dict(row.data) if row else Policy()


def save_policy(session: Session, policy: Policy) -> PolicyRow:
    row = PolicyRow(data=policy.validate().to_dict())
    session.add(row)
    session.flush()
    return row


def spend(session: Session, currency: str, now: dt.datetime | None = None) -> Spend:
    """Sum of outgoing payouts in `currency` today and this month (UTC)."""
    now = now or utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    def total(since: dt.datetime) -> Decimal:
        # The column stores integer cents; sum it as a plain integer, then convert once.
        cents = session.scalar(
            select(func.coalesce(func.sum(Payout.amount, type_=BigInteger), 0)).where(
                Payout.currency == currency,
                Payout.status.in_(SPEND_STATUSES),
                Payout.created_at >= since,
            )
        )
        return (Decimal(cents) / 100).quantize(Decimal("0.01"))

    return Spend(today=total(day_start), month=total(month_start))


def get_or_create_client(session: Session, *, name: str, email: str) -> Client:
    email = email.lower()
    client = session.scalars(select(Client).where(Client.email == email)).first()
    if client is None:
        client = Client(name=name, email=email)
        session.add(client)
        session.flush()
    return client


def get_or_create_recipient(session: Session, *, email: str, name: str = "") -> Recipient:
    email = email.lower()
    r = session.scalars(select(Recipient).where(Recipient.email == email)).first()
    if r is None:
        r = Recipient(email=email, name=name)
        session.add(r)
        session.flush()
    elif name and not r.name:
        r.name = name
    return r


def find_invoices(session: Session, ref: str, limit: int = 5) -> list[Invoice]:
    q = select(Invoice).join(Client).order_by(Invoice.issued_at.desc()).limit(limit)
    ref = ref.strip()
    if ref:
        like = f"%{ref.lower()}%"
        q = q.where(
            or_(
                Invoice.paypal_invoice_id == ref,
                Invoice.number == ref.lstrip("#"),
                func.lower(Client.name).like(like),
                func.lower(Client.email).like(like),
            )
        )
    return list(session.scalars(q))


def find_payouts(session: Session, ref: str, limit: int = 5) -> list[Payout]:
    q = select(Payout).join(Recipient).order_by(Payout.created_at.desc()).limit(limit)
    ref = ref.strip()
    if ref:
        like = f"%{ref.lower()}%"
        q = q.where(
            or_(
                Payout.paypal_batch_id == ref,
                func.lower(Recipient.name).like(like),
                func.lower(Recipient.email).like(like),
            )
        )
    return list(session.scalars(q))


def days_overdue(invoice: Invoice, today: dt.date | None = None) -> int:
    today = today or utcnow().date()
    if invoice.status != "SENT":
        return 0
    return max(0, (today - invoice.due_date).days)
