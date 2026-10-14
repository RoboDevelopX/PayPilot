"""ORM tables. Money columns store integer cents and surface as `Decimal`."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.money import from_cents, to_cents


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class MoneyType(TypeDecorator):
    """Decimal in Python, integer cents in SQLite (which has no exact decimal type)."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> int | None:
        return None if value is None else to_cents(value)

    def process_result_value(self, value: int | None, dialect: Any) -> Decimal | None:
        return None if value is None else from_cents(value)


class UTCDateTime(TypeDecorator):
    """SQLite drops tzinfo; put UTC back on the way out so comparisons stay aware."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return None if value is None else value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        return None if value is None else value.replace(tzinfo=dt.UTC)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    invoices: Mapped[list[Invoice]] = relationship(back_populates="client")


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    action_id: Mapped[str | None] = mapped_column(String(36), index=True)
    paypal_invoice_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    number: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(MoneyType)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    description: Mapped[str] = mapped_column(Text, default="")
    terms_days: Mapped[int] = mapped_column(Integer, default=14)
    issued_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    due_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT/SENT/PAID/CANCELLED
    paid_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    pay_url: Mapped[str | None] = mapped_column(String(500))
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    risk_score: Mapped[float | None] = mapped_column(Float)
    risk_reasons: Mapped[list[str] | None] = mapped_column(JSON)
    is_seeded: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # demo data, never sent to PayPal
    client: Mapped[Client] = relationship(back_populates="invoices")


class Recipient(Base):
    __tablename__ = "recipients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    allowlisted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)  # None = needs approval


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action_id: Mapped[str] = mapped_column(String(36), unique=True)  # also the sender_batch_id
    recipient_id: Mapped[str] = mapped_column(ForeignKey("recipients.id"), index=True)
    paypal_batch_id: Mapped[str | None] = mapped_column(String(64))
    paypal_item_id: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(MoneyType)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    note: Mapped[str] = mapped_column(String(255), default="")
    # approved -> executed / failed ; counts toward caps while approved or executed
    status: Mapped[str] = mapped_column(String(20), default="approved")
    paypal_status: Mapped[str | None] = mapped_column(String(32))
    anomaly_score: Mapped[float | None] = mapped_column(Float)
    anomaly_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    anomaly_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    is_seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    recipient: Mapped[Recipient] = relationship()


class PolicyRow(Base):
    """One row per saved version; the newest row is the active policy."""

    __tablename__ = "policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class Action(Base):
    """Every structured action the agent (or scheduler, or UI) proposes."""

    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat/scheduler/ui
    decision: Mapped[str | None] = mapped_column(String(20))
    reasons: Mapped[list[str] | None] = mapped_column(JSON)
    signals: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # ML advisory inputs
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), unique=True)
    level: Mapped[str] = mapped_column(String(16), default="standard")  # standard/escalated
    decision: Mapped[str | None] = mapped_column(String(16))  # approved/rejected
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    action: Mapped[Action] = relationship()


class AuditEvent(Base):
    """Append-only (enforced by SQLite triggers in app.db)."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(16))  # user/agent/policy/paypal/system/ml
    event: Mapped[str] = mapped_column(String(48))
    action_id: Mapped[str | None] = mapped_column(String(36), index=True)
    paypal_request_id: Mapped[str | None] = mapped_column(String(64))
    paypal_debug_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    paypal_event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    processed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
