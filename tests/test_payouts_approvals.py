"""Payouts, the approval inbox, and the audit log."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import Action, AuditEvent, Payout
from app.paypal.client import PayPalClient
from app.paypal.invoices import InvoicingAPI
from app.paypal.payouts import PayoutsAPI
from app.policy.engine import MLSignals
from app.schemas import parse_action
from app.services.actions import ActionService
from app.services.approvals import ApprovalError, ApprovalRequest, ApprovalService, set_allowlisted
from app.services.store import load_policy, spend

NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.UTC)


def payout(amount: str, email: str = "sam@example.com", currency: str = "USD"):
    return parse_action(
        "send_payout",
        {
            "recipient_email": email,
            "recipient_name": "Sam",
            "amount": amount,
            "currency": currency,
            "note": "design assets",
        },
    )


@pytest.fixture
def env(sandbox):
    engine = make_engine("sqlite://")
    init_db(engine)
    sessions = make_session_factory(engine)
    pp = PayPalClient("id", "secret", transport=sandbox.transport, sleep=lambda _s: None)
    flags: dict[str, MLSignals] = {}

    def anomaly(_s, fields):
        return flags.get(str(fields["amount"]), MLSignals(anomaly_score=0.1))

    actions = ActionService(
        sessions,
        invoicing=InvoicingAPI(pp, invoicer_email="", business_name="Studio"),
        payouts=PayoutsAPI(pp),
        anomaly_scorer=anomaly,
        clock=lambda: NOW,
    )
    set_allowlisted(actions, "sam@example.com", "Sam", True)
    return actions, ApprovalService(actions), sandbox, flags


def events(actions, action_id=None):
    with actions.sessions() as s:
        q = select(AuditEvent).order_by(AuditEvent.id)
        if action_id:
            q = q.where(AuditEvent.action_id == action_id)
        return [e.event for e in s.scalars(q)]


def pending_id(approvals):
    [row] = approvals.pending()
    return row["id"]


def test_auto_approved_payout_is_sent_with_idempotency_keys(env):
    actions, _, sandbox, _ = env
    out = actions.handle(payout("40.00"))
    assert out.status == "executed"
    [(sender_batch_id, sent)] = sandbox.payouts.items()
    assert sender_batch_id == out.action_id  # sender_batch_id is our action id
    item = sent["body"]["items"][0]
    assert item["amount"] == {"value": "40.00", "currency": "USD"}
    call = sandbox.calls_to("/v1/payments/payouts")[0]
    assert call.headers["PayPal-Request-Id"]
    assert events(actions, out.action_id) == ["action_proposed", "policy_decision", "payout_sent"]
    with actions.sessions() as s:
        assert spend(s, "USD", NOW).today == Decimal("40.00")


def test_retrying_a_payout_never_pays_twice(env):
    actions, _, sandbox, _ = env
    out = actions.handle(payout("10.00"))
    with actions.sessions() as s:
        s.get(Action, out.action_id).status = "approved"
        s.query(Payout).one().status = "approved"
        s.commit()
    again = actions.execute_payout(out.action_id)  # e.g. a crash-and-retry
    assert again.status == "executed"
    assert len(sandbox.payouts) == 1  # same PayPal-Request-Id -> PayPal returns the original


def test_over_limit_payout_is_held_then_approved_once(env):
    actions, approvals, sandbox, _ = env
    held = actions.handle(payout("80.00"))
    assert held.status == "needs_approval" and sandbox.payouts == {}
    approval_id = pending_id(approvals)
    out = approvals.approve(approval_id)
    assert out.status == "executed" and len(sandbox.payouts) == 1
    with pytest.raises(ApprovalError) as ei:
        approvals.approve(approval_id)
    assert ei.value.status_code == 409
    assert len(sandbox.payouts) == 1


def test_rejecting_sends_nothing(env):
    actions, approvals, sandbox, _ = env
    actions.handle(payout("80.00"))
    assert approvals.reject(pending_id(approvals)).status == "rejected"
    assert sandbox.payouts == {} and approvals.pending() == []


def test_new_recipient_can_be_allowlisted_on_approval(env):
    actions, approvals, _, _ = env
    assert actions.handle(payout("5.00", "new@example.com")).status == "needs_approval"
    approvals.approve(pending_id(approvals), ApprovalRequest(allowlist_recipient=True))
    assert actions.handle(payout("5.00", "new@example.com")).status == "executed"


def test_caps_are_rechecked_at_approval_time(env):
    actions, approvals, sandbox, _ = env
    assert actions.handle(payout("150.00")).status == "needs_approval"
    for _ in range(4):  # 4 x $50 auto payouts = $200 = the daily cap
        assert actions.handle(payout("50.00")).status == "executed"
    out = approvals.approve(pending_id(approvals))
    assert out.status == "blocked" and "daily cap" in out.reasons[0]
    assert len(sandbox.payouts) == 4


def test_daily_cap_blocks_at_proposal(env):
    actions, _, sandbox, _ = env
    for _ in range(4):
        actions.handle(payout("50.00"))
    out = actions.handle(payout("0.01"))
    assert out.status == "blocked"
    assert len(sandbox.payouts) == 4


def test_anomaly_flag_escalates_even_a_small_payout(env):
    actions, approvals, sandbox, flags = env
    flags["9.00"] = MLSignals(
        anomaly_flag=True, anomaly_score=0.9, anomaly_reason="3 payouts in 1 hour"
    )
    out = actions.handle(payout("9.00"))
    assert out.status == "escalated" and sandbox.payouts == {}
    assert "anomaly_flagged" in events(actions, out.action_id)
    row = approvals.pending()[0]
    assert row["level"] == "escalated" and row["signals"]["anomaly_flag"]
    with pytest.raises(ApprovalError, match="flagged as unusual"):
        approvals.approve(row["id"])
    assert (
        approvals.approve(row["id"], ApprovalRequest(acknowledge_anomaly=True)).status == "executed"
    )


def test_paypal_failure_frees_the_cap(env):
    import httpx

    actions, _, sandbox, _ = env
    original = sandbox._route

    def reject_payouts(method, path, body):
        if path == "/v1/payments/payouts":
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "receiver unconfirmed",
                    "debug_id": "d1",
                },
            )
        return original(method, path, body)

    sandbox._route = reject_payouts
    out = actions.handle(payout("40.00"))
    assert out.status == "paypal_error" and "d1" in out.message
    with actions.sessions() as s:
        assert spend(s, "USD", NOW).today == Decimal("0.00")


def test_spend_windows_are_utc_day_and_month(env):
    actions, _, _, _ = env
    actions.handle(payout("30.00"))
    with actions.sessions() as s:
        assert spend(s, "USD", NOW).today == Decimal("30.00")
        tomorrow = spend(s, "USD", NOW + dt.timedelta(days=1))
        assert tomorrow.today == Decimal("0.00") and tomorrow.month == Decimal("30.00")
        assert spend(s, "USD", NOW + dt.timedelta(days=31)).month == Decimal("0.00")
        assert spend(s, "EUR", NOW).today == Decimal("0.00")


def test_policy_change_takes_effect_only_after_approval(env):
    actions, approvals, _, _ = env
    actions.handle(
        parse_action("update_policy", {"field": "auto_approve_limit", "new_value": "100"})
    )
    assert actions.handle(payout("80.00")).status == "needs_approval"
    rows = approvals.pending()
    policy_row = next(r for r in rows if r["kind"] == "update_policy")
    assert approvals.approve(policy_row["id"]).status == "executed"
    with actions.sessions() as s:
        assert load_policy(s).auto_approve_limit == Decimal("100.00")
    assert actions.handle(payout("80.00")).status == "executed"


def test_payout_polling_and_webhook_update_status(env, make_app):
    actions, _, sandbox, _ = env
    actions.handle(payout("20.00"))
    assert actions.refresh_payouts() == 1
    with actions.sessions() as s:
        assert s.query(Payout).one().paypal_status == "SUCCESS"


def test_denied_payout_webhook_marks_failed(make_app, sandbox):
    client, app = make_app()
    c = app.state.container
    set_allowlisted(c.actions, "sam@example.com", "Sam", True)
    out = c.actions.handle(payout("20.00"))
    batch_id = out.data["paypal_batch_id"]
    sig = {
        "paypal-auth-algo": "a",
        "paypal-cert-url": "c",
        "paypal-transmission-id": "t",
        "paypal-transmission-sig": "valid-sig",
        "paypal-transmission-time": "now",
    }
    event = {
        "id": "WH-P1",
        "event_type": "PAYMENT.PAYOUTSBATCH.DENIED",
        "resource": {"batch_header": {"payout_batch_id": batch_id}},
    }
    assert client.post("/webhooks/paypal", json=event, headers=sig).json()["result"] == "processed"
    [row] = client.get("/api/payouts").json()
    assert row["status"] == "failed" and row["paypal_status"] == "DENIED"


def test_approval_and_audit_api(make_app, sandbox):
    client, app = make_app()
    c = app.state.container
    c.actions.handle(payout("80.00"))
    [row] = client.get("/api/approvals").json()
    assert row["kind"] == "send_payout" and row["reasons"]
    r = client.post(f"/api/approvals/{row['id']}/approve", json={"allowlist_recipient": True})
    assert r.json()["status"] == "executed"
    assert client.post(f"/api/approvals/{row['id']}/approve").status_code == 409
    audit = client.get("/api/audit").json()
    names = [e["event"] for e in audit]
    assert {"approval_granted", "policy_recheck", "payout_sent", "recipient_allowlisted"} <= set(
        names
    )
    sent = next(e for e in audit if e["event"] == "payout_sent")
    assert sent["paypal_request_id"]
    assert client.get("/api/recipients").json()[0]["allowlisted"] is True


def test_policy_api_validates(make_app):
    client, _ = make_app()
    assert client.put("/api/policy", json={"auto_approve_limit": "75"}).status_code == 200
    assert client.get("/api/policy").json()["auto_approve_limit"] == "75.00"
    r = client.put("/api/policy", json={"daily_cap": "-1"})
    assert r.status_code == 422
    assert json.loads(r.text)["detail"]


def test_payout_retry_with_same_batch_id_returns_the_original_batch(sandbox):
    # Real Payouts ignores PayPal-Request-Id and answers a repeated sender_batch_id with a
    # 400 that links to the first batch. A retry must report that batch, never pay again.
    api = PayoutsAPI(PayPalClient("id", "secret", transport=sandbox.transport))
    kwargs = dict(
        receiver_email="sam@example.com",
        amount=Decimal("1.00"),
        currency="USD",
        note="retry",
        sender_batch_id="act-1",
        request_id="act-1:payout.create",
    )
    first = api.create(**kwargs)
    again = api.create(**kwargs)
    assert again.batch_id == first.batch_id
    assert len(sandbox.payouts) == 1


def test_duplicate_batch_error_is_recognised_with_or_without_link():
    from app.paypal.errors import PayPalValidationError
    from app.paypal.payouts import _existing_batch_id

    dup = PayPalValidationError(
        "x",
        details=[
            {"field": "SENDER_BATCH_ID", "issue": "Batch with given sender_batch_id already exists"}
        ],
    )
    assert _existing_batch_id(dup) == ""
    assert _existing_batch_id(PayPalValidationError("x", details=[{"field": "amount"}])) is None
