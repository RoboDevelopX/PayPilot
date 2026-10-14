"""Server-rendered pages (Jinja) with HTMX for partial updates.

Every button here calls the same services as the JSON API, so the UI cannot do
anything the API's rules would refuse. Expected failures (rate limit, PayPal
down, an approval already used) come back as HTML fragments with a 4xx/5xx
status; static/app.js lets HTMX swap them in, so the user sees a message
instead of a dead button.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select

from app.container import Container
from app.models import Approval, AuditEvent, Invoice, Payout, Recipient
from app.policy.engine import TONES, PolicyError
from app.routers.approvals import RecipientIn, payout_row
from app.routers.chat import ChatError, run_chat
from app.routers.invoices import invoice_row
from app.schemas import ChatIn
from app.services.approvals import (
    ApprovalError,
    ApprovalRequest,
    set_allowlisted,
    update_policy_from_ui,
)
from app.services.dashboard import dashboard
from app.services.store import find_invoices, load_policy

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
router = APIRouter(include_in_schema=False)

KIND_LABELS = {
    "send_payout": "Payout",
    "send_reminder": "Payment reminder",
    "update_policy": "Rule change",
    "create_invoice": "Invoice",
}
STATUS_STYLES = {
    "executed": "ok",
    "approved": "ok",
    "PAID": "ok",
    "SUCCESS": "ok",
    "needs_approval": "warn",
    "SENT": "info",
    "info": "info",
    "escalated": "danger",
    "blocked": "danger",
    "rejected": "muted",
    "DRAFT": "muted",
    "CANCELLED": "muted",
    "invalid": "danger",
    "paypal_error": "danger",
    "failed": "danger",
}


# ------------------------------------------------------------ jinja helpers
def money(amount: Any, currency: str = "USD") -> str:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return str(amount)
    return f"${value:,.2f}" if currency == "USD" else f"{value:,.2f} {currency}"


def when(value: str | dt.datetime | None) -> str:
    if not value:
        return ""
    d = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return d.strftime("%b %d, %H:%M UTC")


def risk_level(p: float | None) -> str:
    if p is None:
        return "muted"
    return "danger" if p >= 0.7 else "warn" if p >= 0.4 else "ok"


templates.env.filters.update(
    money=money,
    when=when,
    risk_level=risk_level,
    style=lambda s: STATUS_STYLES.get(s or "", "muted"),
    label=lambda s: (s or "").replace("_", " "),
    pretty=lambda d: json.dumps(d, indent=2, sort_keys=True, default=str),
)
templates.env.globals.update(KIND_LABELS=KIND_LABELS, TONES=TONES)


def _c(request: Request) -> Container:
    return request.app.state.container


def _pending_count(c: Container) -> int:
    with c.sessions() as s:
        return len(s.scalars(select(Approval.id).where(Approval.decision.is_(None))).all())


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    c = _c(request)
    s = c.settings
    base = {
        "demo_mode": s.demo_mode,
        "paypal_configured": s.paypal_configured,
        "paypal_simulated": s.paypal_simulated,
        "llm": s.assistant_label,
        "ml": c.ml.status,
        "pending_count": _pending_count(c),
        "path": request.url.path,
    }
    return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)


# -------------------------------------------------------------------- pages
@router.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    c = _c(request)
    return render(request, "dashboard.html", title="Dashboard", d=dashboard(c.actions, c.ml))


@router.get("/api/dashboard", include_in_schema=True)
def api_dashboard(request: Request) -> dict[str, Any]:
    c = _c(request)
    return dashboard(c.actions, c.ml)


@router.get("/chat", response_class=HTMLResponse)
def page_chat(request: Request):
    return render(request, "chat.html", title="Chat")


@router.post("/ui/chat", response_class=HTMLResponse)
async def ui_chat(request: Request, message: str = Form(""), conversation_id: str = Form("")):
    try:
        body = ChatIn(message=message.strip(), conversation_id=conversation_id or None)
        result = await run_chat(request, body)
    except ValidationError:
        return render(
            request,
            "partials/chat_turn.html",
            422,
            user=message,
            error="Type a message (up to 2,000 characters).",
        )
    except ChatError as e:
        return render(request, "partials/chat_turn.html", e.status_code, user=message, error=str(e))
    return render(request, "partials/chat_turn.html", user=message, result=result)


def _approval_cards(c: Container) -> list[dict[str, Any]]:
    rows = c.approvals.pending()
    with c.sessions() as s:
        allowlisted = {
            r.email
            for r in s.scalars(select(Recipient).where(Recipient.allowlisted_at.is_not(None)))
        }
    for r in rows:
        email = r["payload"].get("recipient_email")
        r["needs_allowlist"] = bool(email) and email.lower() not in allowlisted
    return rows


@router.get("/approvals", response_class=HTMLResponse)
def page_approvals(request: Request):
    return render(
        request, "approvals.html", title="Approvals", approvals=_approval_cards(_c(request))
    )


async def _decide(request: Request, approval_id: str, approve: bool, req: ApprovalRequest):
    c = _c(request)
    with c.sessions() as s:
        a = s.get(Approval, approval_id)
        kind = a.action.kind if a else None
    try:
        if approve:
            outcome = await run_in_threadpool(c.approvals.approve, approval_id, req)
        else:
            outcome = await run_in_threadpool(c.approvals.reject, approval_id)
    except ApprovalError as e:
        return render(
            request, "partials/decision.html", e.status_code, error=str(e), approval_id=approval_id
        )
    return render(
        request,
        "partials/decision.html",
        outcome={"tool": kind, **outcome.to_dict()},
        approval_id=approval_id,
    )


@router.post("/ui/approvals/{approval_id}/approve", response_class=HTMLResponse)
async def ui_approve(
    request: Request,
    approval_id: str,
    acknowledge_anomaly: bool = Form(False),
    allowlist_recipient: bool = Form(False),
):
    req = ApprovalRequest(acknowledge_anomaly, allowlist_recipient)
    return await _decide(request, approval_id, True, req)


@router.post("/ui/approvals/{approval_id}/reject", response_class=HTMLResponse)
async def ui_reject(request: Request, approval_id: str):
    return await _decide(request, approval_id, False, ApprovalRequest())


def _invoices(c: Container) -> list[dict[str, Any]]:
    with c.sessions() as s:
        return [invoice_row(i) for i in find_invoices(s, "", limit=200)]


@router.get("/invoices", response_class=HTMLResponse)
def page_invoices(request: Request):
    return render(request, "invoices.html", title="Invoices", invoices=_invoices(_c(request)))


def _invoice_row_response(request: Request, invoice_id: str, note: dict | None = None):
    c = _c(request)
    with c.sessions() as s:
        inv = s.get(Invoice, invoice_id)
        if inv is None:
            return render(request, "partials/flash.html", 404, error="Invoice not found.")
        row = invoice_row(inv)
    return render(request, "partials/invoice_row.html", inv=row, note=note)


@router.post("/ui/invoices/{invoice_id}/refresh", response_class=HTMLResponse)
async def ui_refresh_invoice(request: Request, invoice_id: str):
    c = _c(request)
    status = await run_in_threadpool(c.actions.refresh_invoice, invoice_id, actor="user")
    return _invoice_row_response(
        request, invoice_id, {"status": "info", "message": f"PayPal says: {status}"}
    )


@router.post("/ui/invoices/{invoice_id}/remind", response_class=HTMLResponse)
async def ui_remind_invoice(request: Request, invoice_id: str):
    c = _c(request)
    with c.sessions() as s:
        if s.get(Invoice, invoice_id) is None:
            return render(request, "partials/flash.html", 404, error="Invoice not found.")
    outcome = await run_in_threadpool(c.actions.remind_invoice, invoice_id, source="ui")
    return _invoice_row_response(request, invoice_id, outcome.to_dict())


@router.post("/ui/followups", response_class=HTMLResponse)
async def ui_followups(request: Request):
    c = _c(request)
    outcomes = await run_in_threadpool(c.actions.sweep_followups)
    return render(
        request,
        "partials/invoice_table.html",
        invoices=_invoices(c),
        swept=[o.to_dict() for o in outcomes],
    )


@router.get("/payouts", response_class=HTMLResponse)
def page_payouts(request: Request):
    with _c(request).sessions() as s:
        rows = [
            payout_row(p)
            for p in s.scalars(select(Payout).order_by(Payout.created_at.desc()).limit(200))
        ]
    return render(request, "payouts.html", title="Payouts", payouts=rows)


def _recipients(c: Container) -> list[dict[str, Any]]:
    with c.sessions() as s:
        return [
            {"email": r.email, "name": r.name, "allowlisted": r.allowlisted_at is not None}
            for r in s.scalars(select(Recipient).order_by(Recipient.email))
        ]


def _policy(c: Container) -> dict[str, Any]:
    with c.sessions() as s:
        return load_policy(s).to_dict()


@router.get("/rules", response_class=HTMLResponse)
def page_rules(request: Request):
    c = _c(request)
    return render(
        request, "rules.html", title="Rules", policy=_policy(c), recipients=_recipients(c)
    )


@router.post("/ui/policy", response_class=HTMLResponse)
async def ui_policy(
    request: Request,
    auto_approve_limit: str = Form(...),
    daily_cap: str = Form(...),
    monthly_cap: str = Form(...),
    invoice_cap: str = Form(...),
    allowed_currencies: str = Form(...),
    auto_remind_max_tone: str = Form("firm"),
    auto_remind: bool = Form(False),
):
    c = _c(request)
    data = {
        "auto_approve_limit": auto_approve_limit.strip(),
        "daily_cap": daily_cap.strip(),
        "monthly_cap": monthly_cap.strip(),
        "invoice_cap": invoice_cap.strip(),
        "allowed_currencies": allowed_currencies,
        "auto_remind": auto_remind,
        "auto_remind_max_tone": auto_remind_max_tone,
    }
    try:
        policy = update_policy_from_ui(c.actions, data)
    except PolicyError as e:
        return render(
            request, "partials/policy_form.html", 422, policy={**_policy(c), **data}, error=str(e)
        )
    return render(request, "partials/policy_form.html", policy=policy.to_dict(), saved=True)


@router.post("/ui/recipients", response_class=HTMLResponse)
async def ui_recipients(
    request: Request, email: str = Form(...), name: str = Form(""), allowlisted: bool = Form(True)
):
    c = _c(request)
    try:
        body = RecipientIn(email=email.strip(), name=name.strip(), allowlisted=allowlisted)
    except ValidationError:
        return render(
            request,
            "partials/recipients.html",
            422,
            recipients=_recipients(c),
            error=f"{email!r} is not a valid email address.",
        )
    set_allowlisted(c.actions, str(body.email), body.name, body.allowlisted)
    return render(request, "partials/recipients.html", recipients=_recipients(c))


def _audit(c: Container, action_id: str | None, before_id: int | None, limit: int = 50):
    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if action_id:
        q = q.where(AuditEvent.action_id == action_id)
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    with c.sessions() as s:
        return [
            {
                "id": e.id,
                "ts": e.ts,
                "actor": e.actor,
                "event": e.event,
                "action_id": e.action_id,
                "paypal_request_id": e.paypal_request_id,
                "paypal_debug_id": e.paypal_debug_id,
                "detail": e.detail,
            }
            for e in s.scalars(q)
        ]


@router.get("/audit", response_class=HTMLResponse)
def page_audit(request: Request, action_id: str | None = None):
    events = _audit(_c(request), action_id, None)
    return render(request, "audit.html", title="Audit log", events=events, action_id=action_id)


@router.get("/ui/audit", response_class=HTMLResponse)
def ui_audit(request: Request, before_id: int, action_id: str | None = None):
    events = _audit(_c(request), action_id, before_id)
    return render(request, "partials/audit_rows.html", events=events, action_id=action_id)
