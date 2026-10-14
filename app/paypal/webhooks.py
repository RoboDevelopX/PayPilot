"""Webhook signature verification.

Why this works: anyone can POST to our public /webhooks/paypal URL, so the
payload proves nothing by itself. PayPal signs each delivery; we send the
signature headers, our webhook id and the event back to PayPal's
verify-webhook-signature endpoint, which answers SUCCESS only if PayPal really
sent it to *our* webhook. Unverified events are dropped.
Endpoint: POST /v1/notifications/verify-webhook-signature (checked Oct 2026).
"""

from __future__ import annotations

from typing import Any

from app.paypal.client import PayPalClient

SIGNATURE_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


def verify_signature(
    client: PayPalClient, headers: dict[str, str], event: dict[str, Any], webhook_id: str
) -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    body: dict[str, Any] = {k: lowered.get(h) for k, h in SIGNATURE_HEADERS.items()}
    if not all(body.values()) or not webhook_id:
        return False
    body["webhook_id"] = webhook_id
    body["webhook_event"] = event
    resp = client.request(
        "POST", "/v1/notifications/verify-webhook-signature", json=body, prefer_representation=False
    )
    return resp.data.get("verification_status") == "SUCCESS"


def invoice_id_from(event: dict[str, Any]) -> str | None:
    resource = event.get("resource") or {}
    return resource.get("id") or (resource.get("invoice") or {}).get("id")


def payout_batch_id_from(event: dict[str, Any]) -> str | None:
    resource = event.get("resource") or {}
    return (resource.get("batch_header") or {}).get("payout_batch_id") or resource.get(
        "payout_batch_id"
    )
