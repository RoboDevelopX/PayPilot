"""Process verified PayPal webhook events: dedupe, then update local state.

Why this works (webhooks): PayPal pushes events (invoice paid, payout
settled) to our URL so the dashboard updates without polling. Deliveries can
be retried or arrive twice, so each event id is stored once and repeats are
ignored. Webhooks can also be missed, which is why the follow-up job polls
open invoices as a fallback.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db import session_scope
from app.models import Payout, WebhookEvent, utcnow
from app.paypal.webhooks import invoice_id_from, payout_batch_id_from
from app.services import audit
from app.services.actions import ActionService

INVOICE_EVENT_STATUS = {
    "INVOICING.INVOICE.PAID": "PAID",
    "INVOICING.INVOICE.CANCELLED": "CANCELLED",
    "INVOICING.INVOICE.REFUNDED": "REFUNDED",
}
PAYOUT_FAILED = {"DENIED", "FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED", "CANCELED"}


def handle_event(
    sessions: sessionmaker[Session], actions: ActionService, event: dict[str, Any]
) -> str:
    """Returns 'duplicate', 'ignored' or 'processed'."""
    event_id = event.get("id") or ""
    event_type = event.get("event_type") or ""
    if not event_id:
        return "ignored"
    with session_scope(sessions) as s:
        if s.get(WebhookEvent, event_id):
            return "duplicate"
        s.add(WebhookEvent(paypal_event_id=event_id, event_type=event_type, verified=True))
        audit.record(
            s,
            actor="paypal",
            event="webhook_received",
            event_type=event_type,
            paypal_event_id=event_id,
        )

    result = "ignored"
    if event_type.startswith("INVOICING.INVOICE."):
        paypal_invoice_id = invoice_id_from(event)
        if paypal_invoice_id:
            status = INVOICE_EVENT_STATUS.get(event_type) or (event.get("resource") or {}).get(
                "status"
            )
            if status:
                actions.apply_invoice_status(
                    paypal_invoice_id, status, actor="paypal", source="webhook"
                )
            result = "processed"
    elif event_type.startswith(("PAYMENT.PAYOUTSBATCH.", "PAYMENT.PAYOUTS-ITEM.")):
        batch_id = payout_batch_id_from(event)
        new_status = event_type.rsplit(".", 1)[-1]
        with session_scope(sessions) as s:
            for p in s.query(Payout).filter_by(paypal_batch_id=batch_id):
                p.paypal_status = new_status
                if new_status in PAYOUT_FAILED and p.status == "executed":
                    p.status = "failed"  # failed money no longer counts against caps
                audit.record(
                    s,
                    actor="paypal",
                    event="payout_status_changed",
                    action_id=p.action_id,
                    paypal_batch_id=batch_id,
                    paypal_status=new_status,
                )
        result = "processed"

    with session_scope(sessions) as s:
        s.get(WebhookEvent, event_id).processed_at = utcnow()
    return result
