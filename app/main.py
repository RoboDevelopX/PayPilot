"""FastAPI entry point: HTTP API, static chat UI, reminder scheduler and PayPal webhooks."""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .actions import Service
from .agent import ClaudeAgent, MockAgent
from .config import settings
from .paypal import MockPayPal, PayPalError, SandboxPayPal
from .rules import ValidationError
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("paypilot")
STATIC = Path(__file__).resolve().parent.parent / "static"


def build(db_path: str | None = None, *, mock_paypal: bool | None = None, mock_ai: bool | None = None):
    use_mock_pp = settings.paypal_mode == "mock" if mock_paypal is None else mock_paypal
    store = Store(db_path or settings.db_path or (":memory:" if use_mock_pp else "paypal-sandbox.db"))
    paypal = MockPayPal() if use_mock_pp else SandboxPayPal(
        settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_base_url,
        settings.paypal_invoicer_email, settings.business_name,
    )
    service = Service(store, paypal)
    use_mock_ai = settings.ai_mode == "mock" if mock_ai is None else mock_ai
    agent = MockAgent(service) if use_mock_ai else ClaudeAgent(service, settings.anthropic_api_key, settings.anthropic_model, settings.anthropic_effort)
    service.drafter = agent.draft_reminder  # reminders are written by the AI when it is available
    return service, agent


service, agent = build()


async def reminder_loop() -> None:
    while True:
        await asyncio.sleep(settings.reminder_check_seconds)
        try:
            sent = await run_in_threadpool(service.reminder_sweep)
            if sent:
                log.info("reminder sweep: %s", sent)
        except Exception:
            log.exception("reminder sweep failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("PayPilot starting: PayPal=%s AI=%s", service.paypal.mode, agent.mode)
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()


app = FastAPI(title="PayPilot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# A small per-IP limiter so a public demo cannot run up the AI bill.
_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)


def _rate_limit(request: Request, limit: int = 30, window: int = 600) -> None:
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    q, t = _hits[ip], time.time()
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many messages. Please wait a few minutes.")
    q.append(t)


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "true", "paypal": service.paypal.mode, "ai": agent.mode}


@app.get("/api/state")
def state() -> dict[str, Any]:
    approvals = service.store.list("approval", 50)
    return {
        "modes": {"paypal": service.paypal.mode, "ai": agent.mode, "model": getattr(agent, "model", None)},
        "rules": service.get_rules().to_dict(),
        "spent_today": str(service.spent_today()),
        "invoices": service.store.list("invoice", 50),
        "payouts": service.store.list("payout", 50),
        "approvals": approvals,
        "pending": sum(1 for a in approvals if a["status"] == "pending"),
        "activity": service.store.list("activity", 60),
    }


@app.post("/api/chat")
def chat(body: ChatIn, request: Request) -> dict[str, Any]:
    _rate_limit(request)
    return agent.chat(body.session_id, body.message)


@app.put("/api/rules")
def update_rules(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"rules": service.set_rules(body).to_dict()}
    except (ValueError, ValidationError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/approvals/{approval_id}/{decision}")
def decide(approval_id: str, decision: str) -> dict[str, Any]:
    if decision not in {"approve", "reject"}:
        raise HTTPException(404)
    try:
        return service.decide(approval_id, decision == "approve")
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/reminders/run")
def run_reminders(force: bool = True) -> dict[str, Any]:
    """Runs the reminder sweep now. `force` ignores the waiting period, for demos."""
    return {"results": service.reminder_sweep(force=force)}


@app.post("/api/invoices/{invoice_id}/refresh")
def refresh_invoice(invoice_id: str) -> dict[str, Any]:
    try:
        return {"invoice": service.refresh_invoice(service.find_invoice(invoice_id))}
    except (ValidationError, PayPalError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/invoices/{invoice_id}/simulate-paid")
def simulate_paid(invoice_id: str) -> dict[str, Any]:
    try:
        return {"invoice": service.simulate_paid(invoice_id)}
    except (ValidationError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/reset")
def reset() -> dict[str, str]:
    if service.paypal.mode != "mock":
        raise HTTPException(403, "reset is only available in mock mode")
    service.store.reset()
    return {"ok": "true"}


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request) -> dict[str, str]:
    """Marks invoices paid and payouts settled as PayPal reports them."""
    event = await request.json()
    if isinstance(service.paypal, SandboxPayPal):
        if not settings.paypal_webhook_id:
            raise HTTPException(503, "PAYPAL_WEBHOOK_ID is not configured")
        headers = {k.lower(): v for k, v in request.headers.items()}
        ok = await run_in_threadpool(service.paypal.verify_webhook, headers, event, settings.paypal_webhook_id)
        if not ok:
            raise HTTPException(400, "webhook signature verification failed")
    etype = event.get("event_type", "")
    resource = event.get("resource", {}) or {}
    service.store.log("webhook", {"event_type": etype, "id": event.get("id")})
    if etype.startswith("INVOICING.INVOICE."):
        inv_id = (resource.get("invoice") or resource).get("id")
        for inv in service.store.list("invoice", 500):
            if inv["paypal_id"] == inv_id:
                await run_in_threadpool(service.refresh_invoice, inv)
    elif etype.startswith("PAYMENT.PAYOUTSBATCH."):
        batch_id = (resource.get("batch_header") or {}).get("payout_batch_id")
        status = (resource.get("batch_header") or {}).get("batch_status")
        for p in service.store.list("payout", 500):
            if p.get("paypal_id") == batch_id and status:
                service.store.update("payout", p["id"], status=status)
    return {"ok": "true"}
