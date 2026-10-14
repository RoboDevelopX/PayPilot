"""The HTMX pages: every page renders, and each button reaches the same rules as the API."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.models import Client, Invoice, utcnow
from app.policy.engine import MLSignals
from app.schemas import parse_action
from app.services.approvals import set_allowlisted
from tests.fakes import ScriptedLLM, message, text, tool

PAGES = ["/", "/chat", "/approvals", "/invoices", "/payouts", "/rules", "/audit"]
INVOICE_ARGS = {
    "client_name": "Acme Corp",
    "client_email": "ap@acme.example",
    "items": [{"name": "Logo design", "quantity": 1, "unit_amount": "450"}],
    "currency": "USD",
    "due_in_days": 14,
    "note": "",
}


def payout(amount: str, email: str = "sam@example.com"):
    return parse_action(
        "send_payout",
        {
            "recipient_email": email,
            "recipient_name": "Sam",
            "amount": amount,
            "currency": "USD",
            "note": "design assets",
        },
    )


def htmx(client, url, data=None):
    return client.post(url, data=data or {}, headers={"HX-Request": "true"})


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_with_the_sandbox_label(make_app, path):
    client, _ = make_app()
    r = client.get(path)
    assert r.status_code == 200
    assert "SANDBOX" in r.text and 'name="viewport"' in r.text
    assert "DEMO MODE" not in r.text


def test_demo_mode_is_labeled_everywhere(make_app):
    client, _ = make_app(demo_mode=True)
    for path in PAGES:
        assert "DEMO MODE" in client.get(path).text


def test_static_assets_are_served(make_app):
    client, _ = make_app()
    assert client.get("/static/htmx.min.js").status_code == 200
    assert "prefers-color-scheme: dark" in client.get("/static/style.css").text


def test_chat_turn_renders_reply_and_action_card(make_app, sandbox):
    llm = ScriptedLLM(
        message(tool("create_invoice", INVOICE_ARGS)),
        message(text("Sent invoice #0001 to Acme for $450.00.")),
    )
    client, _ = make_app(llm)
    r = htmx(client, "/ui/chat", {"message": "Invoice Acme <b>$450</b>", "conversation_id": ""})
    assert r.status_code == 200
    assert "Sent invoice #0001" in r.text
    assert "executed" in r.text
    assert "Open the sandbox invoice" in r.text  # the payer's link, for testing the payment
    assert "&lt;b&gt;$450&lt;/b&gt;" in r.text  # user text is escaped, never rendered as HTML
    conv = re.search(r'id="conversation_id" name="conversation_id" value="([^"]+)"', r.text)
    assert conv and 'hx-swap-oob="true"' in r.text


def test_chat_errors_are_shown_not_swallowed(make_app):
    client, _ = make_app()  # no LLM key
    r = htmx(client, "/ui/chat", {"message": "hi"})
    assert r.status_code == 503 and "not configured" in r.text
    r = htmx(client, "/ui/chat", {"message": "   "})
    assert r.status_code == 422 and "Type a message" in r.text


def test_chat_rate_limit_message(make_app):
    llm = ScriptedLLM(*[message(text("ok")) for _ in range(5)])
    client, _ = make_app(llm, chat_rate_limit_per_minute=2)
    assert htmx(client, "/ui/chat", {"message": "a"}).status_code == 200
    assert htmx(client, "/ui/chat", {"message": "b"}).status_code == 200
    r = htmx(client, "/ui/chat", {"message": "c"})
    assert r.status_code == 429 and "Too many messages" in r.text


def test_approve_from_inbox_runs_once(make_app, sandbox):
    client, app = make_app()
    c = app.state.container
    c.actions.handle(payout("80.00"))
    page = client.get("/approvals").text
    assert "needs approval" in page and "$80.00" in page
    assert "Also add sam@example.com to my allowlist" in page
    [row] = c.approvals.pending()
    r = htmx(client, f"/ui/approvals/{row['id']}/approve", {"allowlist_recipient": "true"})
    assert r.status_code == 200 and "executed" in r.text and "Payout" in r.text
    assert 'id="approval-count"' in r.text and 'data-n="0"' in r.text
    again = htmx(client, f"/ui/approvals/{row['id']}/approve")
    assert again.status_code == 409 and "already used" in again.text
    assert len(sandbox.payouts) == 1


def test_escalated_card_is_red_and_needs_the_tick(make_app, sandbox):
    client, app = make_app()
    c = app.state.container
    set_allowlisted(c.actions, "sam@example.com", "Sam", True)
    c.actions.anomaly_scorer = lambda _s, _f: MLSignals(
        anomaly_flag=True, anomaly_score=0.81, anomaly_reason="$45.00 is 18x what you usually pay"
    )
    assert c.actions.handle(payout("45.00")).status == "escalated"
    page = client.get("/approvals").text
    assert "approval escalated" in page and "18x what you usually pay" in page
    assert "checked this payout and it" in page and "data-needs-ack disabled" in page
    [row] = c.approvals.pending()
    r = htmx(client, f"/ui/approvals/{row['id']}/approve")  # tick bypassed: server still refuses
    assert r.status_code == 400 and "flagged as unusual" in r.text
    assert sandbox.payouts == {}
    r = htmx(client, f"/ui/approvals/{row['id']}/approve", {"acknowledge_anomaly": "true"})
    assert r.status_code == 200 and "executed" in r.text


def test_reject_from_inbox(make_app, sandbox):
    client, app = make_app()
    app.state.container.actions.handle(payout("80.00"))
    [row] = app.state.container.approvals.pending()
    r = htmx(client, f"/ui/approvals/{row['id']}/reject")
    assert "rejected" in r.text and sandbox.payouts == {}


def test_invoice_buttons(make_app, sandbox):
    client, app = make_app()
    c = app.state.container
    c.actions.handle(parse_action("create_invoice", INVOICE_ARGS))
    page = client.get("/invoices").text
    assert "Acme Corp" in page and "$450.00" in page
    inv_id = re.search(r'id="inv-([0-9a-f-]+)"', page).group(1)
    r = htmx(client, f"/ui/invoices/{inv_id}/refresh")
    assert r.status_code == 200 and "PayPal says: SENT" in r.text
    r = htmx(client, f"/ui/invoices/{inv_id}/remind")
    assert r.status_code == 200 and f'id="inv-{inv_id}"' in r.text
    assert htmx(client, "/ui/invoices/nope/remind").status_code == 404
    r = htmx(client, "/ui/followups")
    assert "Follow-ups ran" in r.text


def test_policy_form_saves_and_explains_errors(make_app):
    client, _ = make_app()
    form = {
        "auto_approve_limit": "75",
        "daily_cap": "300",
        "monthly_cap": "1500",
        "invoice_cap": "10000",
        "allowed_currencies": "USD, EUR",
        "auto_remind_max_tone": "friendly",
    }
    r = htmx(client, "/ui/policy", form)
    assert r.status_code == 200 and "Saved" in r.text
    got = client.get("/api/policy").json()
    assert got["auto_approve_limit"] == "75.00" and got["allowed_currencies"] == ["EUR", "USD"]
    assert got["auto_remind"] is False  # unchecked box means off
    r = htmx(client, "/ui/policy", {**form, "daily_cap": "10"})  # below the auto limit
    assert r.status_code == 422 and "Not saved" in r.text
    assert client.get("/api/policy").json()["daily_cap"] == "300.00"


def test_recipient_allowlist_add_and_remove(make_app):
    client, _ = make_app()
    r = htmx(client, "/ui/recipients", {"email": "Pat@Example.com", "name": "Pat"})
    assert "allowlisted" in r.text
    r = htmx(client, "/ui/recipients", {"email": "pat@example.com", "allowlisted": "false"})
    assert "needs approval" in r.text
    r = htmx(client, "/ui/recipients", {"email": "not-an-email"})
    assert r.status_code == 422 and "not a valid email" in r.text


def test_audit_page_filters_and_pages(make_app, sandbox):
    client, app = make_app()
    c = app.state.container
    set_allowlisted(c.actions, "sam@example.com", "Sam", True)
    outs = [c.actions.handle(payout("1.00")) for _ in range(20)]
    page = client.get("/audit").text
    assert "payout sent" in page and "PayPal-Request-Id" in page
    assert "Load older entries" in page
    older = client.get(f"/ui/audit?before_id={10}").text
    assert older.count("<tr>") == 9
    one = client.get(f"/audit?action_id={outs[0].action_id}").text
    assert "Showing one action" in one and one.count("<tr>") == 4  # header + 3 events


def test_dashboard_numbers(make_app, sandbox):
    client, app = make_app()
    c = app.state.container
    set_allowlisted(c.actions, "sam@example.com", "Sam", True)
    c.actions.handle(payout("40.00"))
    c.actions.handle(payout("80.00"))
    c.actions.handle(parse_action("create_invoice", INVOICE_ARGS))
    d = client.get("/api/dashboard").json()
    assert d["outstanding"] == {"USD": "450.00"} and d["outstanding_count"] == 1
    assert d["spend"]["today"] == "40.00" and d["spend"]["today_pct"] == 20
    assert d["pending_approvals"] == 1
    page = client.get("/").text
    assert "$40.00 of $200.00" in page and "$450.00" in page


def test_dashboard_with_ml_shows_advice(make_app):
    client, app = make_app(ml_enabled=True)
    now = utcnow()
    with app.state.sessions() as s:
        acme = Client(name="Acme Corp", email="ap@acme.example")
        s.add(acme)
        s.flush()
        for i in range(10):  # ten invoices on net-30, each paid about two weeks late
            issued = now - dt.timedelta(days=400 - 35 * i)
            due = (issued + dt.timedelta(days=30)).date()
            s.add(
                Invoice(
                    client_id=acme.id,
                    amount=Decimal("900.00"),
                    terms_days=30,
                    issued_at=issued,
                    due_date=due,
                    status="PAID",
                    paid_at=issued + dt.timedelta(days=44),
                    is_seeded=True,
                )
            )
        s.commit()
    page = client.get("/").text
    assert "Acme Corp is likely to pay late" in page and "consider net-7" in page
    assert "late" in page.split("consider net-7", 1)[1]  # the reasons follow the advice


def test_optional_password_protects_everything_but_health_and_webhooks(make_app):
    client, _ = make_app(app_password=SecretStr("s3cret-demo"))
    assert client.get("/").status_code == 401
    assert client.get("/api/approvals").status_code == 401
    assert client.get("/", auth=("paypilot", "wrong")).status_code == 401
    assert client.get("/", auth=("paypilot", "s3cret-demo")).status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.post("/webhooks/paypal", json={"id": "x"}).status_code == 400  # signature check


def test_rate_limit_cannot_be_dodged_with_a_fake_forwarded_for(make_app):
    llm = ScriptedLLM(*[message(text("ok")) for _ in range(5)])
    client, _ = make_app(llm, chat_rate_limit_per_minute=1)
    first = {"x-forwarded-for": "1.1.1.1, 10.0.0.9"}
    spoofed = {"x-forwarded-for": "2.2.2.2, 10.0.0.9"}  # same real client behind the proxy
    assert client.post("/api/chat", json={"message": "a"}, headers=first).status_code == 200
    assert client.post("/api/chat", json={"message": "b"}, headers=spoofed).status_code == 429
