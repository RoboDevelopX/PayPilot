from decimal import Decimal

import pytest

from app import rules as R


def payout(amount, email="bob@example.com", currency="USD"):
    return R.normalize_payout({"recipient_email": email, "amount": amount, "currency": currency})


def evaluate(action, rules=None, spent="0", known=("bob@example.com",)):
    return R.evaluate_payout(action, rules or R.Rules(), spent_today=Decimal(spent), known_recipients=set(known))


def test_at_or_under_limit_is_auto_approved():
    assert evaluate(payout("50.00")).decision == "auto_approve"
    assert evaluate(payout("49.99")).decision == "auto_approve"


def test_over_limit_needs_approval():
    v = evaluate(payout("50.01"))
    assert v.decision == "needs_approval"
    assert "above your auto-approve limit" in v.reasons[0]


def test_daily_budget_needs_approval():
    v = evaluate(payout("40"), spent="120")
    assert v.decision == "needs_approval"
    assert "daily limit" in v.reasons[0]


def test_new_recipient_needs_approval_when_enabled():
    assert evaluate(payout("10", "new@example.com"), known=()).decision == "auto_approve"
    rules = R.Rules(approve_new_recipients=True)
    assert evaluate(payout("10", "new@example.com"), rules, known=()).decision == "needs_approval"


def test_hard_cap_and_currency_are_denied():
    assert evaluate(payout("1000.01")).decision == "deny"
    assert evaluate(payout("10", currency="EUR")).decision == "deny"


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "NaN", "Infinity", ""])
def test_bad_amounts_are_invalid(bad):
    with pytest.raises(R.ValidationError):
        payout(bad)


def test_bad_email_is_invalid():
    with pytest.raises(R.ValidationError):
        payout("10", email="not-an-email")


def test_amounts_are_rounded_to_cents():
    assert payout("10.005")["amount"] == "10.01"
    assert payout("1,250")["amount"] == "1250.00"


def test_invoice_totals_and_cap():
    inv = R.normalize_invoice({"recipient_email": "a@b.co", "currency": "usd",
                               "items": [{"description": "Design", "quantity": 3, "unit_amount": "100"}]})
    assert inv["total"] == "300.00" and inv["currency"] == "USD"
    assert R.evaluate_invoice(inv, R.Rules()).decision == "auto_approve"
    assert R.evaluate_invoice(inv, R.Rules(invoice_cap=Decimal("299"))).decision == "deny"


def test_invoice_needs_items():
    with pytest.raises(R.ValidationError):
        R.normalize_invoice({"recipient_email": "a@b.co", "items": []})


def test_rules_roundtrip_and_checks():
    r = R.Rules.from_dict({"spending_limit": "75", "allowed_currencies": ["usd", "eur"]})
    assert r.spending_limit == Decimal("75.00") and r.allowed_currencies == ["USD", "EUR"]
    with pytest.raises(ValueError):
        R.Rules.from_dict({"spending_limit": "5000"})  # above hard cap
