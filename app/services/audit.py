"""Append-only audit trail: every proposal, decision, approval and PayPal call."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record(
    session: Session,
    *,
    actor: str,
    event: str,
    action_id: str | None = None,
    paypal_request_id: str | None = None,
    paypal_debug_id: str | None = None,
    **detail: Any,
) -> AuditEvent:
    ev = AuditEvent(
        actor=actor,
        event=event,
        action_id=action_id,
        paypal_request_id=paypal_request_id,
        paypal_debug_id=paypal_debug_id,
        detail=_jsonable(detail),
    )
    session.add(ev)
    session.flush()
    return ev


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)  # Decimal, date, enum...
