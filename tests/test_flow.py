"""End-to-end flow through the HTTP API in mock mode (no keys needed)."""
import os

os.environ["PAYPILOT_MOCK_PAYPAL"] = "1"
os.environ["PAYPILOT_MOCK_AI"] = "1"
os.environ["PAYPILOT_DB"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client():
    main.service.store.reset()
    main._hits.clear()
    with TestClient(main.app) as c:
        yield c


def say(client, text):
    r = client.post("/api/chat", json={"session_id": "t", "message": text})
    assert r.status_code == 200
    return r.json()


def test_invoice_reminder_and_payment(client):
    res = say(client, "Invoice alice@example.com $1,200 for the website redesign, due in 30 days")
    ev = res["events"][0]
    assert ev["tool"] == "create_invoice" and ev["result"]["status"] == "executed"
    inv = ev["result"]["invoice"]
    assert inv["status"] == "SENT" and inv["total"] == "1200.00" and inv["due_in_days"] == 30

    res = say(client, "Remind alice@example.com about her invoice")
    assert res["events"][0]["result"]["status"] == "executed"
    assert res["events"][0]["result"]["reminder_number"] == 1

    client.post(f"/api/invoices/{inv['id']}/simulate-paid")
    res = say(client, "Remind alice@example.com about her invoice")
    assert res["events"] == []  # nothing open to remind

    state = client.get("/api/state").json()
    assert state["invoices"][0]["status"] == "PAID"


def test_payout_auto_approved_then_queued(client):
    client.put("/api/rules", json={"approve_new_recipients": False})
    res = say(client, "Pay bob@example.com $25 for lunch")
    assert res["events"][0]["result"]["status"] == "executed"

    res = say(client, "Send carol@example.com $400 for the photo shoot")
    r = res["events"][0]["result"]
    assert r["status"] == "pending_approval"
    state = client.get("/api/state").json()
    assert state["pending"] == 1 and len(state["payouts"]) == 1  # nothing sent yet

    out = client.post(f"/api/approvals/{r['approval_id']}/approve").json()
    assert out["status"] == "executed" and out["payout"]["approved_by"] == "user"
    assert client.post(f"/api/approvals/{r['approval_id']}/approve").status_code == 409  # no double-pay


def test_hard_cap_denied_and_rejection(client):
    res = say(client, "Pay dan@example.com $5000 for the car")
    assert res["events"][0]["result"]["status"] == "denied"

    client.put("/api/rules", json={"approve_new_recipients": True})
    res = say(client, "Pay erin@example.com $20 for coffee")  # new recipient -> approval
    r = res["events"][0]["result"]
    assert r["status"] == "pending_approval"
    assert client.post(f"/api/approvals/{r['approval_id']}/reject").json()["status"] == "rejected"
    assert client.get("/api/state").json()["payouts"] == []


def test_daily_limit(client):
    client.put("/api/rules", json={"approve_new_recipients": False, "daily_limit": "60"})
    assert say(client, "Pay a@example.com $40")["events"][0]["result"]["status"] == "executed"
    assert say(client, "Pay b@example.com $40")["events"][0]["result"]["status"] == "pending_approval"


def test_scheduled_reminders_respect_max(client):
    client.put("/api/rules", json={"max_reminders": 2})
    say(client, "Invoice zoe@example.com $90 for a logo")
    for expected in (1, 1, 0):
        results = client.post("/api/reminders/run").json()["results"]
        assert len([x for x in results if x["status"] == "executed"]) == expected


def test_cancel_requires_approval(client):
    inv = say(client, "Invoice yan@example.com $50 for tutoring")["events"][0]["result"]["invoice"]
    r = say(client, f"Cancel {inv['id']}")["events"][0]["result"]
    assert r["status"] == "pending_approval"
    client.post(f"/api/approvals/{r['approval_id']}/approve")
    assert client.get("/api/state").json()["invoices"][0]["status"] == "CANCELLED"


def test_invalid_rules_rejected(client):
    assert client.put("/api/rules", json={"spending_limit": "-1"}).status_code == 400


def test_tool_router_rejects_unknown_and_invalid():
    svc = main.service
    assert svc.handle_tool("transfer_everything", {})["status"] == "error"
    assert svc.handle_tool("send_payout", {"recipient_email": "x", "amount": "5", "currency": "USD"})["status"] == "invalid"
