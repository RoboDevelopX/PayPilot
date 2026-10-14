"""Follow-ups: tone ladder, timing, the sweep job, LLM drafting, and webhooks."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import Action, Approval, AuditEvent, Invoice
from app.paypal.client import PayPalClient
from app.paypal.invoices import InvoicingAPI
from app.schemas import parse_action
from app.services.actions import ActionService
from app.services.followups import LLMDrafter, TemplateDrafter, reminder_due, tone_for
from tests.fakes import message, text

T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)


# --- pure rules ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("days", "risk", "sent", "tone"),
    [
        (1, None, 0, "friendly"),
        (7, None, 0, "friendly"),
        (8, None, 0, "firm"),
        (20, None, 0, "firm"),
        (21, None, 0, "final"),
        (3, 0.72, 0, "firm"),  # high risk: one step up
        (25, 0.9, 0, "final"),  # never beyond final
        (2, 0.1, 2, "final"),  # never softer than the last reminder
    ],
)
def test_tone_ladder(days, risk, sent, tone):
    assert tone_for(days, risk, sent) == tone


def test_reminder_timing():
    kw = dict(reminders_sent=0, last_reminder_at=None, now=T0)
    assert not reminder_due(days_overdue=2, risk=0.2, **kw)
    assert reminder_due(days_overdue=3, risk=0.2, **kw)
    assert reminder_due(days_overdue=1, risk=0.8, **kw)  # high risk: sooner
    assert not reminder_due(
        days_overdue=10,
        risk=None,
        reminders_sent=1,
        last_reminder_at=T0 - dt.timedelta(days=4),
        now=T0,
    )
    assert not reminder_due(
        days_overdue=40, risk=None, reminders_sent=3, last_reminder_at=None, now=T0
    )


# --- service-level sweep ------------------------------------------------------
class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


@pytest.fixture
def svc(sandbox):
    engine = make_engine("sqlite://")
    init_db(engine)
    sessions = make_session_factory(engine)
    clock = Clock(T0)
    pp = PayPalClient("id", "secret", transport=sandbox.transport, sleep=lambda _s: None)
    service = ActionService(
        sessions,
        invoicing=InvoicingAPI(pp, invoicer_email="", business_name="Studio"),
        drafter=TemplateDrafter(),
        clock=clock,
    )
    service.test_clock = clock
    return service


def create_invoice(service, days=14):
    a = parse_action(
        "create_invoice",
        {
            "client_name": "Acme Corp",
            "client_email": "ap@acme.example",
            "items": [{"name": "Logo design", "quantity": 1, "unit_amount": "450"}],
            "currency": "USD",
            "due_in_days": days,
            "note": "",
        },
    )
    out = service.handle(a)
    assert out.status == "executed", out
    return out


def test_sweep_sends_friendly_then_holds_final_for_approval(svc, sandbox):
    create_invoice(svc, days=14)
    assert svc.sweep_followups() == []  # not due yet

    svc.test_clock.t = T0 + dt.timedelta(days=14 + 3)  # 3 days overdue
    [out] = svc.sweep_followups()
    assert out.status == "executed" and out.data["tone"] == "friendly"
    assert len(sandbox.reminders) == 1
    assert "Friendly reminder" in sandbox.reminders[0]["subject"]

    svc.test_clock.t += dt.timedelta(days=2)  # too soon for another one
    assert svc.sweep_followups() == []

    svc.test_clock.t = T0 + dt.timedelta(days=14 + 25)  # 25 days overdue -> final
    [out] = svc.sweep_followups()
    assert out.status == "needs_approval"  # policy: final reminders need approval
    assert len(sandbox.reminders) == 1
    with svc.sessions() as s:
        assert s.scalars(select(Approval)).one()
    # A held reminder is not drafted again on the next tick.
    assert svc.sweep_followups() == []


def test_paid_invoice_is_never_chased(svc, sandbox):
    out = create_invoice(svc)
    sandbox.invoices[out.data["paypal_invoice_id"]]["status"] = "PAID"  # payer paid; webhook missed
    svc.test_clock.t = T0 + dt.timedelta(days=30)
    assert svc.sweep_followups() == []
    assert sandbox.reminders == []
    with svc.sessions() as s:
        inv = s.scalars(select(Invoice)).one()
        assert inv.status == "PAID" and inv.paid_at is not None
        assert (
            s.scalars(select(AuditEvent).where(AuditEvent.event == "invoice_status_changed"))
            .one()
            .detail["source"]
            == "poll"
        )


def test_reminder_via_chat_tool_when_not_overdue_is_friendly(svc, sandbox):
    create_invoice(svc)
    out = svc.handle(parse_action("send_reminder", {"invoice_ref": "Acme"}))
    assert out.status == "executed" and out.data["tone"] == "friendly"
    assert sandbox.calls_to("/v2/invoicing/invoices/")[-1].headers["PayPal-Request-Id"]


def test_reminder_for_unknown_invoice_returns_info(svc):
    out = svc.handle(parse_action("send_reminder", {"invoice_ref": "Nobody"}))
    assert out.status == "info"


# --- LLM drafting ---------------------------------------------------------------
def test_llm_drafter_uses_structured_output():
    calls = []

    def fake_create(**kw):
        calls.append(kw)
        return message(text(json.dumps({"subject": "Quick nudge", "note": "Hi Acme, ..."})))

    subject, note = LLMDrafter(fake_create, model="m").draft({"client": "Acme"}, "friendly")
    assert (subject, note) == ("Quick nudge", "Hi Acme, ...")
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_llm_drafter_falls_back_to_template_on_bad_output():
    def broken(**kw):
        return message(text("not json"))

    ctx = {
        "client": "Acme",
        "number": "#1",
        "amount": "$1.00",
        "due_date": "2026-10-01",
        "days": 9,
        "business": "",
    }
    subject, _ = LLMDrafter(broken, model="m").draft(ctx, "firm")
    assert subject == "Invoice #1 is 9 days overdue"


# --- webhooks -------------------------------------------------------------------
SIG = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT",
    "paypal-transmission-id": "tx-1",
    "paypal-transmission-sig": "valid-sig",
    "paypal-transmission-time": "2026-10-14T10:00:00Z",
}


def paid_event(paypal_id, event_id="WH-EVT-1"):
    return {
        "id": event_id,
        "event_type": "INVOICING.INVOICE.PAID",
        "resource": {"invoice": {"id": paypal_id, "status": "PAID"}},
    }


def test_signed_webhook_marks_invoice_paid_once(make_app, sandbox):
    client, app = make_app()
    svc = app.state.container.actions
    out = create_invoice(svc)
    pid = out.data["paypal_invoice_id"]

    r = client.post("/webhooks/paypal", json=paid_event(pid), headers=SIG)
    assert r.json() == {"result": "processed"}
    r = client.post("/webhooks/paypal", json=paid_event(pid), headers=SIG)
    assert r.json() == {"result": "duplicate"}
    with app.state.sessions() as s:
        assert s.scalars(select(Invoice)).one().status == "PAID"
    verify = sandbox.calls_to("/v1/notifications/verify-webhook-signature")[0]
    assert json.loads(verify.content)["webhook_id"] == "WH-TEST"


def test_forged_webhook_is_rejected(make_app):
    client, app = make_app()
    out = create_invoice(app.state.container.actions)
    r = client.post(
        "/webhooks/paypal",
        json=paid_event(out.data["paypal_invoice_id"]),
        headers={**SIG, "paypal-transmission-sig": "forged"},
    )
    assert r.status_code == 400
    with app.state.sessions() as s:
        assert s.scalars(select(Invoice)).one().status == "SENT"


def test_webhook_without_headers_or_config(make_app):
    client, _ = make_app()
    assert client.post("/webhooks/paypal", json={"id": "x"}).status_code == 400
    client2, _ = make_app(paypal_webhook_id="")
    assert client2.post("/webhooks/paypal", json={"id": "x"}, headers=SIG).status_code == 503


def test_invoice_api_lists_and_reminds(make_app, sandbox):
    client, app = make_app()
    create_invoice(app.state.container.actions)
    [row] = client.get("/api/invoices").json()
    assert row["status"] == "SENT" and row["amount"] == "450.00"
    r = client.post(f"/api/invoices/{row['id']}/remind").json()
    assert r["status"] == "executed"
    with app.state.sessions() as s:
        assert s.scalars(select(Action).where(Action.kind == "send_reminder")).one().source == "ui"
