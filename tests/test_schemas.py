from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agent.tools import TOOLS
from app.schemas import ACTION_MODELS, CreateInvoice, SendPayout, parse_action

GOOD_INVOICE = {
    "client_name": "Acme Corp",
    "client_email": "ap@acme.example",
    "items": [{"name": "Logo design", "quantity": 1, "unit_amount": "450"}],
    "currency": "USD",
    "due_in_days": 14,
    "note": "",
}


def test_valid_invoice_parses_to_decimal():
    a = parse_action("create_invoice", GOOD_INVOICE)
    assert isinstance(a, CreateInvoice)
    assert a.total == Decimal("450.00")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_email", "not-an-email"),
        ("currency", "usd"),
        ("due_in_days", 0),
        ("due_in_days", 365),
        ("items", []),
    ],
)
def test_invalid_invoice_fields_rejected(field, value):
    with pytest.raises(ValidationError):
        parse_action("create_invoice", {**GOOD_INVOICE, field: value})


@pytest.mark.parametrize("amount", ["$40", "40.001", "-40", "1e3", "forty", "1,000"])
def test_payout_amount_must_be_plain_decimal_string(amount):
    with pytest.raises(ValidationError):
        parse_action(
            "send_payout",
            {
                "recipient_email": "sam@example.com",
                "recipient_name": "",
                "amount": amount,
                "currency": "USD",
                "note": "",
            },
        )


def test_float_amounts_are_rejected():
    with pytest.raises(ValidationError):
        SendPayout(recipient_email="sam@example.com", amount=40.0, currency="USD")


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        parse_action("create_invoice", {**GOOD_INVOICE, "auto_approve": True})


def test_unknown_tool_raises_keyerror():
    with pytest.raises(KeyError):
        parse_action("transfer_everything", {})


def test_tool_definitions_match_action_models_and_are_strict():
    assert {t["name"] for t in TOOLS} == set(ACTION_MODELS)
    for t in TOOLS:
        assert t["strict"] is True

        def check(schema):
            if schema.get("type") == "object":
                assert schema["additionalProperties"] is False
                assert set(schema["required"]) == set(schema["properties"])
                for sub in schema["properties"].values():
                    check(sub)
            if schema.get("type") == "array":
                check(schema["items"])

        check(t["input_schema"])
