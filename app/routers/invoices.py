"""Invoice API, the follow-up job trigger, and the PayPal webhook receiver."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.container import Container
from app.models import Invoice
from app.paypal.errors import PayPalError
from app.paypal.webhooks import verify_signature
from app.services.store import days_overdue, find_invoices
from app.services.webhooks import handle_event

log = logging.getLogger("paypilot.webhooks")
router = APIRouter()


def invoice_row(inv: Invoice) -> dict[str, Any]:
    return {
        "id": inv.id,
        "number": inv.number,
        "paypal_invoice_id": inv.paypal_invoice_id,
        "client": inv.client.name,
        "client_email": inv.client.email,
        "amount": f"{inv.amount:.2f}",
        "currency": inv.currency,
        "description": inv.description,
        "status": inv.status,
        "due_date": inv.due_date.isoformat(),
        "days_overdue": days_overdue(inv),
        "reminders_sent": inv.reminders_sent,
        "risk_score": inv.risk_score,
        "risk_reasons": inv.risk_reasons or [],
        "pay_url": inv.pay_url,
        "is_seeded": inv.is_seeded,
    }


@router.get("/api/invoices")
def list_invoices(request: Request) -> list[dict[str, Any]]:
    c: Container = request.app.state.container
    with c.sessions() as s:
        return [invoice_row(i) for i in find_invoices(s, "", limit=200)]


@router.post("/api/invoices/{invoice_id}/refresh")
def refresh_invoice(invoice_id: str, request: Request) -> dict[str, Any]:
    c: Container = request.app.state.container
    return {"status": c.actions.refresh_invoice(invoice_id, actor="user")}


@router.post("/api/invoices/{invoice_id}/remind")
def remind_invoice(invoice_id: str, request: Request) -> dict[str, Any]:
    c: Container = request.app.state.container
    with c.sessions() as s:
        if s.get(Invoice, invoice_id) is None:
            raise HTTPException(404, "invoice not found")
    return c.actions.remind_invoice(invoice_id, source="ui").to_dict()


@router.post("/api/jobs/followups")
def run_followups(request: Request) -> dict[str, Any]:
    c: Container = request.app.state.container
    return {"outcomes": [o.to_dict() for o in c.actions.sweep_followups()]}


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request) -> dict[str, str]:
    c: Container = request.app.state.container
    if c.paypal is None or not c.settings.paypal_webhook_id:
        raise HTTPException(503, "webhooks are not configured (set PAYPAL_WEBHOOK_ID)")
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError as e:
        raise HTTPException(400, "invalid JSON") from e
    try:
        ok = await run_in_threadpool(
            verify_signature, c.paypal, dict(request.headers), event, c.settings.paypal_webhook_id
        )
    except PayPalError as e:
        # 5xx makes PayPal retry the delivery later, which is what we want.
        log.warning("could not verify webhook: %s", e)
        raise HTTPException(503, "could not verify signature right now") from e
    if not ok:
        log.warning("rejected webhook with an invalid signature")
        raise HTTPException(400, "invalid signature")
    result = await run_in_threadpool(handle_event, c.sessions, c.actions, event)
    return {"result": result}
