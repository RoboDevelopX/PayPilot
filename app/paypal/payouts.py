"""PayPal Payouts API v1: send money to an email address.

Endpoints (checked against developer.paypal.com, Oct 2026):
  POST /v1/payments/payouts             create a batch (we always send one item)
  GET  /v1/payments/payouts/{batch_id}  batch + item status
Why this works (double-payment protection): `sender_batch_id` is our action id.
PayPal refuses a second batch with the same sender_batch_id for 30 days, so a
retried request can never pay twice. Payouts does NOT replay the first answer
for a repeated PayPal-Request-Id (checked live, Oct 2026): it returns 400
USER_BUSINESS_ERROR with a SENDER_BATCH_ID detail that links to the original
batch. `create` follows that link and returns the original batch, so a retry
after a timeout reports "already sent" instead of "failed" (which would free
up the spending caps for money that already left).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.paypal.client import PayPalClient
from app.paypal.errors import PayPalConflict, PayPalValidationError


@dataclass(frozen=True)
class PayoutInfo:
    batch_id: str
    batch_status: str
    item_id: str | None = None
    item_status: str | None = None
    debug_id: str | None = None


class PayoutsAPI:
    def __init__(self, client: PayPalClient) -> None:
        self.client = client

    def create(
        self,
        *,
        receiver_email: str,
        amount: Decimal,
        currency: str,
        note: str,
        sender_batch_id: str,
        request_id: str,
    ) -> PayoutInfo:
        body = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": "You have a payment (PayPal sandbox)",
                "email_message": note or "You received a payment via PayPilot.",
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": receiver_email,
                    "amount": {"value": f"{amount:.2f}", "currency": currency},
                    "note": note or "Payment via PayPilot",
                    "sender_item_id": sender_batch_id,
                }
            ],
        }
        try:
            resp = self.client.request(
                "POST",
                "/v1/payments/payouts",
                json=body,
                request_id=request_id,
                prefer_representation=False,
            )
        except PayPalValidationError as e:
            existing = _existing_batch_id(e)
            if existing is None:
                raise
            if not existing:
                raise PayPalConflict(
                    "This payout was already sent (duplicate sender_batch_id).",
                    status=e.status,
                    name=e.name,
                    details=e.details,
                    debug_id=e.debug_id,
                ) from e
            return self.get(existing)
        header = resp.data.get("batch_header", {})
        return PayoutInfo(
            batch_id=header.get("payout_batch_id", ""),
            batch_status=header.get("batch_status", "PENDING"),
            debug_id=resp.debug_id,
        )

    def get(self, batch_id: str) -> PayoutInfo:
        resp = self.client.request("GET", f"/v1/payments/payouts/{batch_id}")
        header = resp.data.get("batch_header", {})
        items = resp.data.get("items") or [{}]
        return PayoutInfo(
            batch_id=batch_id,
            batch_status=header.get("batch_status", "UNKNOWN"),
            item_id=items[0].get("payout_item_id"),
            item_status=items[0].get("transaction_status"),
            debug_id=resp.debug_id,
        )


def _existing_batch_id(e: PayPalValidationError) -> str | None:
    """Batch id from a duplicate-sender_batch_id error; "" if PayPal gave no link; None if
    the error is something else."""
    for d in e.details:
        if d.get("field") == "SENDER_BATCH_ID" and "already exists" in d.get("issue", ""):
            for link in d.get("link") or []:
                href = link.get("href", "")
                if "/v1/payments/payouts/" in href:
                    return href.rstrip("/").rsplit("/", 1)[-1]
            return ""
    return None
