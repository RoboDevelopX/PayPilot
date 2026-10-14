"""Policy engine tests, with the boundaries spelled out (a == L, cap edges, new recipients)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.policy.engine import (
    Level,
    MLSignals,
    Policy,
    PolicyError,
    Spend,
    evaluate_invoice,
    evaluate_payout,
    evaluate_policy_change,
    evaluate_reminder,
)

POLICY = Policy(
    auto_approve_limit=D("50.00"),
    daily_cap=D("200.00"),
    monthly_cap=D("1000.00"),
    invoice_cap=D("10000.00"),
    allowed_currencies=frozenset({"USD"}),
)


def payout(amount: str, *, spend=Spend(), known=True, currency="USD", ml=None, policy=POLICY):
    return evaluate_payout(
        amount=D(amount),
        currency=currency,
        recipient="sam@example.com",
        policy=policy,
        spend=spend,
        recipient_allowlisted=known,
        ml=ml,
    )


# --- the core rule: a <= L auto-approves, a > L asks ----------------------
def test_amount_equal_to_limit_auto_approves():
    d = payout("50.00")
    assert d.level == Level.AUTO
    assert "within your auto-approve limit" in d.reasons[0]


def test_one_cent_over_limit_needs_approval():
    d = payout("50.01")
    assert d.level == Level.NEEDS_APPROVAL
    assert d.reasons == ("$50.01 is above your auto-approve limit of $50.00",)


def test_small_amount_auto_approves():
    assert payout("0.01").level == Level.AUTO


# --- hard caps (block, even though approval would otherwise be possible) --
def test_exactly_hitting_daily_cap_is_allowed():
    d = payout("40.00", spend=Spend(today=D("160.00"), month=D("160.00")))
    assert d.level == Level.AUTO  # 160 + 40 == 200 == D


def test_one_cent_over_daily_cap_blocks():
    d = payout("40.01", spend=Spend(today=D("160.00"), month=D("160.00")))
    assert d.level == Level.BLOCK
    assert "daily cap of $200.00" in d.reasons[0]


def test_exactly_hitting_monthly_cap_is_allowed():
    d = payout("50.00", spend=Spend(today=D("0"), month=D("950.00")))
    assert d.level == Level.AUTO


def test_one_cent_over_monthly_cap_blocks():
    d = payout("50.00", spend=Spend(today=D("0"), month=D("950.01")))
    assert d.level == Level.BLOCK
    assert "monthly cap" in d.reasons[0]


def test_cap_block_beats_approval_reasons():
    d = payout("300.00", known=False)  # above L, new recipient, and over the daily cap
    assert d.level == Level.BLOCK
    assert "daily cap" in d.reasons[0]
    assert len(d.reasons) == 3


# --- currency allowlist ---------------------------------------------------
def test_disallowed_currency_blocks():
    d = payout("10.00", currency="EUR")
    assert d.level == Level.BLOCK
    assert "EUR is not an allowed currency" in d.reasons[0]


def test_non_positive_amount_blocks():
    assert payout("0.00").level == Level.BLOCK


# --- recipient allowlist --------------------------------------------------
def test_new_recipient_below_limit_still_needs_approval():
    d = payout("5.00", known=False)
    assert d.level == Level.NEEDS_APPROVAL
    assert "new recipient" in d.reasons[0]


def test_new_recipient_and_over_limit_lists_both_reasons():
    d = payout("60.00", known=False)
    assert d.level == Level.NEEDS_APPROVAL
    assert len(d.reasons) == 2


# --- ML is advisory: it can raise, never lower, never block -----------------
def test_anomaly_flag_escalates_an_otherwise_auto_payout():
    d = payout("20.00", ml=MLSignals(anomaly_flag=True, anomaly_reason="5x the usual amount"))
    assert d.level == Level.ESCALATED
    assert "5x the usual amount" in d.reasons[0]
    assert not d.can_execute_now


def test_anomaly_flag_escalates_above_needs_approval():
    assert payout("80.00", ml=MLSignals(anomaly_flag=True)).level == Level.ESCALATED


def test_anomaly_flag_cannot_unblock():
    d = payout("10.00", currency="EUR", ml=MLSignals(anomaly_flag=True))
    assert d.level == Level.BLOCK


def test_no_ml_signal_can_lower_a_level():
    # A "not anomalous" signal is the default; it changes nothing.
    assert payout("60.00", ml=MLSignals(anomaly_flag=False, anomaly_score=-0.9)).level == (
        Level.NEEDS_APPROVAL
    )


def test_levels_are_ordered():
    assert Level.AUTO < Level.NEEDS_APPROVAL < Level.ESCALATED < Level.BLOCK


# --- invoices ---------------------------------------------------------------
def test_invoice_within_cap_is_auto():
    assert evaluate_invoice(total=D("450.00"), currency="USD", policy=POLICY).level == Level.AUTO


def test_invoice_at_cap_is_auto_and_over_cap_blocks():
    assert evaluate_invoice(total=D("10000.00"), currency="USD", policy=POLICY).level == Level.AUTO
    assert evaluate_invoice(total=D("10000.01"), currency="USD", policy=POLICY).level == Level.BLOCK


def test_invoice_in_disallowed_currency_blocks():
    assert evaluate_invoice(total=D("10"), currency="GBP", policy=POLICY).level == Level.BLOCK


# --- reminders ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("tone", "expected"),
    [("friendly", Level.AUTO), ("firm", Level.AUTO), ("final", Level.NEEDS_APPROVAL)],
)
def test_reminder_tone_ladder(tone, expected):
    assert evaluate_reminder(tone=tone, invoice_open=True, policy=POLICY).level == expected


def test_reminders_off_means_every_reminder_asks():
    off = Policy(auto_remind=False)
    assert evaluate_reminder(tone="friendly", invoice_open=True, policy=off).level == (
        Level.NEEDS_APPROVAL
    )


def test_no_reminder_for_paid_invoice():
    assert evaluate_reminder(tone="friendly", invoice_open=False, policy=POLICY).level == (
        Level.BLOCK
    )


# --- policy changes from the agent ------------------------------------------
def test_agent_policy_change_always_needs_approval():
    decision, proposed = evaluate_policy_change(
        current=POLICY, changes={"auto_approve_limit": "100"}
    )
    assert decision.level == Level.NEEDS_APPROVAL
    assert proposed.auto_approve_limit == D("100.00")


def test_invalid_policy_change_is_blocked():
    decision, proposed = evaluate_policy_change(
        current=POLICY, changes={"auto_approve_limit": "500"}  # above the daily cap
    )
    assert decision.level == Level.BLOCK
    assert proposed == POLICY


# --- policy validation and round trip -----------------------------------------
def test_policy_roundtrip():
    assert Policy.from_dict(POLICY.to_dict()) == POLICY


@pytest.mark.parametrize(
    "bad",
    [
        {"daily_cap": "-1"},
        {"auto_approve_limit": "300"},
        {"daily_cap": "2000"},
        {"allowed_currencies": []},
        {"allowed_currencies": ["usd1"]},
        {"auto_remind_max_tone": "angry"},
        {"monthly_cap": "abc"},
    ],
)
def test_invalid_policies_are_rejected(bad):
    with pytest.raises(PolicyError):
        Policy.from_dict({**POLICY.to_dict(), **bad})


def test_decision_serializes_every_check():
    d = payout("60.00").to_dict()
    assert d["level"] == "needs_approval"
    assert {c["rule"] for c in d["checks"]} >= {"daily_cap", "auto_approve_limit"}


def test_policy_engine_has_no_io_imports():
    """The engine must stay pure: no network, DB, filesystem, clock or LLM imports."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/policy/engine.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"__future__", "dataclasses", "decimal", "enum", "typing", "app.money"}
