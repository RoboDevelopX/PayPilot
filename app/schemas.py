"""Pydantic models for every action the LLM may propose.

Two layers of validation, on purpose:
1. The tool definitions use `strict: true`, so the API guarantees the *shape*
   (right fields, right JSON types).
2. These models enforce the *meaning*: amounts are decimal strings with at most
   two places, emails are real emails, currencies are 3-letter codes, and so on.
A failure here goes back to the model as an error tool result (max 2 retries).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field

from app.money import parse_amount_string

Amount = Annotated[Decimal, BeforeValidator(parse_amount_string)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
POLICY_FIELDS = (
    "auto_approve_limit",
    "daily_cap",
    "monthly_cap",
    "invoice_cap",
    "allowed_currencies",
    "auto_remind",
    "auto_remind_max_tone",
)


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InvoiceItemIn(_Action):
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=10_000)
    unit_amount: Amount


class CreateInvoice(_Action):
    kind: Literal["create_invoice"] = "create_invoice"
    client_name: str = Field(min_length=1, max_length=100)
    client_email: EmailStr
    items: list[InvoiceItemIn] = Field(min_length=1, max_length=25)
    currency: Currency
    due_in_days: int = Field(ge=1, le=120)
    note: str = Field(default="", max_length=1000)

    @property
    def total(self) -> Decimal:
        return sum((i.unit_amount * i.quantity for i in self.items), Decimal("0.00"))


class SendPayout(_Action):
    kind: Literal["send_payout"] = "send_payout"
    recipient_email: EmailStr
    recipient_name: str = Field(default="", max_length=100)
    amount: Amount
    currency: Currency
    note: str = Field(default="", max_length=255)


class SendReminder(_Action):
    kind: Literal["send_reminder"] = "send_reminder"
    invoice_ref: str = Field(min_length=1, max_length=100)


class GetStatus(_Action):
    kind: Literal["get_status"] = "get_status"
    target: Literal["invoice", "payout", "summary"]
    ref: str = Field(default="", max_length=100)


class AskClarification(_Action):
    kind: Literal["ask_clarification"] = "ask_clarification"
    question: str = Field(min_length=1, max_length=500)
    missing_fields: list[str] = Field(default_factory=list, max_length=10)


class UpdatePolicy(_Action):
    kind: Literal["update_policy"] = "update_policy"
    field: Literal[POLICY_FIELDS]  # type: ignore[valid-type]
    new_value: str = Field(min_length=1, max_length=100)


ActionModel = (
    CreateInvoice | SendPayout | SendReminder | GetStatus | AskClarification | UpdatePolicy
)

ACTION_MODELS: dict[str, type[_Action]] = {
    "create_invoice": CreateInvoice,
    "send_payout": SendPayout,
    "send_reminder": SendReminder,
    "get_status": GetStatus,
    "ask_clarification": AskClarification,
    "update_policy": UpdatePolicy,
}


def parse_action(tool_name: str, tool_input: dict) -> _Action:
    """Validate one tool call. Raises KeyError for unknown tools, ValidationError for bad input."""
    model = ACTION_MODELS[tool_name]
    return model.model_validate({**tool_input, "kind": tool_name})


# ---- API bodies -------------------------------------------------------------
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)
