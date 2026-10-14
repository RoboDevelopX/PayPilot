"""The six tools Claude can call. Each one is a *proposal*, never a PayPal call.

`strict: true` makes the API guarantee the arguments match these schemas
(objects need `additionalProperties: false`). Strict schemas cannot express
numeric ranges or string patterns, so those rules live in `app/schemas.py`.
Amounts are strings so no float ever represents money.
"""

from __future__ import annotations

from typing import Any

from app.schemas import POLICY_FIELDS


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


AMOUNT = {
    "type": "string",
    "description": "Decimal amount as a string with at most 2 decimals, e.g. '450' or '39.99'. No symbols or commas.",
}
CURRENCY = {"type": "string", "description": "ISO 4217 code, e.g. USD. Default to USD."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_invoice",
        "description": (
            "Propose creating and sending a PayPal invoice to a client. Use only when you know the "
            "client's email, at least one line item with an amount, and the currency. If the email "
            "or amount is missing, call ask_clarification instead. Never invent an email."
        ),
        "input_schema": _obj(
            {
                "client_name": {"type": "string", "description": "Client or company name."},
                "client_email": {"type": "string", "description": "Client billing email."},
                "items": {
                    "type": "array",
                    "items": _obj(
                        {
                            "name": {"type": "string", "description": "What was delivered."},
                            "quantity": {"type": "integer", "description": "Whole number, >= 1."},
                            "unit_amount": AMOUNT,
                        }
                    ),
                },
                "currency": CURRENCY,
                "due_in_days": {
                    "type": "integer",
                    "description": "Days until due (1-120). Default 14 if unstated.",
                },
                "note": {"type": "string", "description": "Short friendly note, or empty."},
            }
        ),
    },
    {
        "name": "send_payout",
        "description": (
            "Propose sending money to someone through PayPal Payouts. The user's rules decide "
            "whether it runs now, waits for approval, or is blocked. Never split one payment into "
            "several to get around a limit."
        ),
        "input_schema": _obj(
            {
                "recipient_email": {"type": "string", "description": "Recipient PayPal email."},
                "recipient_name": {"type": "string", "description": "Name, or empty."},
                "amount": AMOUNT,
                "currency": CURRENCY,
                "note": {"type": "string", "description": "What the payment is for, or empty."},
            }
        ),
    },
    {
        "name": "send_reminder",
        "description": (
            "Propose sending a payment reminder for an unpaid invoice. The tone (friendly, firm, "
            "final) is chosen by code from how overdue the invoice is."
        ),
        "input_schema": _obj(
            {
                "invoice_ref": {
                    "type": "string",
                    "description": "Invoice number, PayPal invoice id, or the client's name.",
                }
            }
        ),
    },
    {
        "name": "get_status",
        "description": "Look up invoices, payouts, or an overall summary (read-only).",
        "input_schema": _obj(
            {
                "target": {"type": "string", "enum": ["invoice", "payout", "summary"]},
                "ref": {
                    "type": "string",
                    "description": "Invoice number, id, client or recipient name/email; empty for all.",
                },
            }
        ),
    },
    {
        "name": "ask_clarification",
        "description": "Ask the user one short question when a required detail is missing or ambiguous.",
        "input_schema": _obj(
            {
                "question": {"type": "string"},
                "missing_fields": {"type": "array", "items": {"type": "string"}},
            }
        ),
    },
    {
        "name": "update_policy",
        "description": (
            "Propose changing one of the user's rules. This never takes effect by itself: it is "
            "queued for the user's explicit approval."
        ),
        "input_schema": _obj(
            {
                "field": {"type": "string", "enum": list(POLICY_FIELDS)},
                "new_value": {
                    "type": "string",
                    "description": "New value as text, e.g. '75.00', 'USD,EUR', 'true', 'firm'.",
                },
            }
        ),
    },
]

for _tool in TOOLS:
    _tool["strict"] = True
