"""DEMO_MODE: sample data, the recorded PayPal fallback and the scripted assistant.

Demo mode must work with no keys at all, label everything it simulates, and
never weaken the rules: the same policy engine decides every scripted request.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.agent.llm import LLMUnavailable
from app.agent.recorded import LABEL, DemoFallbackLLM, ScriptedLLM
from app.models import AuditEvent, Invoice
from app.paypal.client import PayPalClient
from app.paypal.errors import PayPalUnavailable
from app.paypal.recorded import RecordedSandbox
from app.schemas import parse_action
from app.services.seed import seed_demo
from tests.fakes import message, text


@pytest.fixture
def demo(make_app):
    """Demo mode with no PayPal or AI keys: everything simulated, clearly labeled."""
    client, app = make_app(
        demo_mode=True, ml_enabled=True, paypal_client_id="", paypal_client_secret=SecretStr("")
    )
    return client, app


def chat(client, text_, conv=None):
    r = client.post("/api/chat", json={"message": text_, "conversation_id": conv})
    assert r.status_code == 200, r.text
    return r.json()


def debug_ids(app, event):
    with app.state.sessions() as s:
        q = select(AuditEvent.paypal_debug_id).where(AuditEvent.event == event)
        return list(s.scalars(q))


def test_demo_boots_with_labeled_sample_data(demo):
    client, app = demo
    health = client.get("/healthz").json()
    assert health["paypal"] == "simulated (demo recordings)"
    assert health["llm"] == "demo script (no AI key)" and health["demo_mode"] is True
    page = client.get("/").text
    assert "DEMO MODE" in page and "simulated" in page and "demo-recorded-" in page
    d = client.get("/api/dashboard").json()
    assert d["overdue_count"] == 2 and d["overdue"] == {"USD": "2000.00"}
    assert d["paid_this_month"] == {"USD": "300.00"}
    assert d["spend"]["today"] == "0.00" and d["spend"]["month"] == "0.00"
    acme = d["at_risk"][0]
    assert acme["client"] == "Acme Corp" and acme["risk"] >= 0.7
    assert d["insights"][0]["client"] == "Acme Corp"
    # seeding is one-off
    assert seed_demo(app.state.container.actions) is False


def test_demo_script_runs_through_the_real_rules(demo):
    client, app = demo
    # 1. An invoice without an email gets a clarifying question, then goes out.
    r = chat(client, "Invoice Lumen Labs $600 for launch photos")
    assert r["clarification"] and r["reply"].startswith(LABEL) and r["actions"] == []
    r = chat(client, "finance@lumenlabs.example", r["conversation_id"])
    assert r["actions"][0]["status"] == "executed"
    assert all(d.startswith("demo-recorded-") for d in debug_ids(app, "invoice_sent"))
    # 2. Payouts: auto, held, and flagged as unusual.
    assert chat(client, "Pay sam@example.com $40 for the icon set")["actions"][0]["status"] == (
        "executed"
    )
    assert chat(client, "Pay sam@example.com $120 for the brand guide")["actions"][0]["status"] == (
        "needs_approval"
    )
    jo = chat(client, "Pay jo@example.com $160 for event photos")["actions"][0]
    assert jo["status"] == "escalated"
    assert any("first payout to this recipient" in r for r in jo["reasons"])
    # 3. The late payer gets a final-tone reminder, which waits for approval.
    remind = chat(client, "Send a reminder to Acme Corp")["actions"][0]
    assert remind["status"] == "needs_approval" and "final" in remind["message"]
    # 4. The assistant can't raise its own limit.
    limit = chat(client, "Raise my auto-approve limit to $75")["actions"][0]
    assert limit["status"] == "needs_approval"
    too_high = chat(client, "Raise my auto-approve limit to $500")["actions"][0]
    assert too_high["status"] == "blocked"  # can't exceed the daily cap
    # 5. Read-only question.
    overdue = chat(client, "Which invoices are overdue?")["reply"]
    assert "Acme Corp" in overdue and "Northwind Traders" in overdue

    # Approvals: the $120 goes out; Jo's $160 is then over the daily cap, ticked or not.
    approvals = {
        a["payload"].get("recipient_email"): a for a in client.get("/api/approvals").json()
    }
    sam = approvals["sam@example.com"]
    assert client.post(f"/api/approvals/{sam['id']}/approve").json()["status"] == "executed"
    jo_id = approvals["jo@example.com"]["id"]
    blocked = client.post(
        f"/api/approvals/{jo_id}/approve", json={"acknowledge_anomaly": True}
    ).json()
    assert blocked["status"] == "blocked" and "daily cap" in blocked["reasons"][0]


def test_unknown_requests_get_help_not_guesses(demo):
    client, _ = demo
    r = chat(client, "What's the weather like?")
    assert r["actions"] == [] and "demo script's phrasings" in r["reply"]


def test_reminder_on_seeded_invoice_uses_the_recording(demo):
    client, app = demo
    c = app.state.container
    with app.state.sessions() as s:
        north = s.scalars(
            select(Invoice).where(Invoice.status == "SENT", Invoice.number.is_not(None))
        ).all()
        north_id = next(i.id for i in north if i.client.name == "Northwind Traders")
    out = c.actions.remind_invoice(north_id, source="ui")
    assert out.status == "executed"  # 3 days late, moderate risk: friendly, goes out alone
    assert debug_ids(app, "reminder_sent")[0].startswith("demo-recorded-")


def test_recordings_stand_in_only_when_the_live_sandbox_is_down(settings, sandbox):
    def down(_request):
        raise httpx.ConnectError("sandbox unreachable")

    from fastapi.testclient import TestClient

    from app.main import create_app

    for demo_mode, expected in ((True, "executed"), (False, "paypal_error")):
        s = settings.model_copy(update={"demo_mode": demo_mode})
        app = create_app(s, paypal_transport=httpx.MockTransport(down))
        TestClient(app)
        out = app.state.container.actions.handle(
            parse_action(
                "create_invoice",
                {
                    "client_name": "Acme Corp",
                    "client_email": "ap@acme.example",
                    "items": [{"name": "Logo", "quantity": 1, "unit_amount": "450"}],
                    "currency": "USD",
                    "due_in_days": 14,
                    "note": "",
                },
            )
        )
        assert out.status == expected

    # When the live sandbox works, demo mode uses it, not the recordings.
    s = settings.model_copy(update={"demo_mode": True})
    app = create_app(s, paypal_transport=sandbox.transport)
    app.state.container.actions.handle(
        parse_action(
            "send_payout",
            {
                "recipient_email": "sam@example.com",
                "recipient_name": "Sam",
                "amount": "5.00",
                "currency": "USD",
                "note": "",
            },
        )
    )
    assert sandbox.calls_to("/v1/payments/payouts")


def test_recorded_sandbox_is_idempotent_and_never_verifies_webhooks():
    rec = RecordedSandbox(always=True)
    pp = PayPalClient("", "", recorded=rec)
    body = {
        "sender_batch_header": {"sender_batch_id": "a1"},
        "items": [{"amount": {"value": "5.00", "currency": "USD"}, "receiver": "x@example.com"}],
    }
    first = pp.request("POST", "/v1/payments/payouts", json=body, request_id="rid-1")
    again = pp.request("POST", "/v1/payments/payouts", json=body, request_id="rid-1")
    assert first.data == again.data and len(rec.payouts) == 1
    assert first.debug_id.startswith("demo-recorded-")
    with pytest.raises(PayPalUnavailable):
        pp.request("POST", "/v1/notifications/verify-webhook-signature", json={})


def test_scripted_fallback_when_the_model_is_unavailable():
    def broken(**_kw):
        raise LLMUnavailable("quota")

    llm = DemoFallbackLLM(broken)
    out = llm(messages=[{"role": "user", "content": "Pay sam@example.com $12"}])
    assert out.stop_reason == "tool_use" and out.content[0].input["amount"] == "12"

    ok = DemoFallbackLLM(lambda **_kw: message(text("real model")))
    assert ok(messages=[{"role": "user", "content": "hi"}]).content[0].text == "real model"


@pytest.mark.parametrize(
    ("said", "tool", "field", "value"),
    [
        (
            "Invoice Acme Corp (ap@acme.example) $450 for logo design, due in 14 days",
            "create_invoice",
            "due_in_days",
            14,
        ),
        (
            "invoice Blue Fern Studio (billing@bluefern.example) 99.50 for icons",
            "create_invoice",
            "client_name",
            "Blue Fern Studio",
        ),
        ("Pay maya@example.com $20 for copy edits", "send_payout", "note", "copy edits"),
        ("Remind Northwind Traders", "send_reminder", "invoice_ref", "Northwind Traders"),
        ("Which invoices are overdue?", "get_status", "target", "invoice"),
    ],
)
def test_scripted_patterns(said, tool, field, value):
    out = ScriptedLLM()(messages=[{"role": "user", "content": said}])
    assert out.content[0].name == tool and out.content[0].input[field] == value
    parse_action(tool, dict(out.content[0].input))  # always schema-valid
