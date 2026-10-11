"""The rules engine: "the AI proposes, the code disposes".

Everything here is deterministic and has no I/O. The agent's proposed action is
validated (shape, amounts, currency, recipient) and then checked against the
user's rules, which yields one of three decisions:

    deny             the action breaks a hard rule and never reaches PayPal
    needs_approval   the action is valid but exceeds what the user lets the agent do alone
    auto_approve     the action is within the user's rules and runs immediately

The core money rule is the one from the write-up:

    auto-approve  if a <= L
    ask the user  if a >  L

plus a daily auto-approval budget, a currency allowlist, a hard ceiling, and an
optional "first payment to a new recipient needs approval" check.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Decision = Literal["auto_approve", "needs_approval", "deny"]


@dataclass
class Rules:
    spending_limit: Decimal = Decimal("50.00")        # L: auto-approve payouts at or under this
    daily_limit: Decimal = Decimal("150.00")          # total auto-approved payouts per day
    hard_cap: Decimal = Decimal("1000.00")            # never send more than this, even with approval
    invoice_cap: Decimal = Decimal("10000.00")        # largest invoice the agent may create
    allowed_currencies: list[str] = field(default_factory=lambda: ["USD"])
    approve_new_recipients: bool = False              # optional: first payout to someone new needs approval
    auto_reminders: bool = True
    reminder_after_hours: float = 72.0                # first reminder this long after sending
    reminder_every_hours: float = 72.0                # then this often
    max_reminders: int = 3

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("spending_limit", "daily_limit", "hard_cap", "invoice_cap"):
            d[k] = str(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rules":
        base = cls()
        merged = {**base.to_dict(), **{k: v for k, v in d.items() if k in base.to_dict()}}
        rules = cls(
            spending_limit=money(merged["spending_limit"]),
            daily_limit=money(merged["daily_limit"]),
            hard_cap=money(merged["hard_cap"]),
            invoice_cap=money(merged["invoice_cap"]),
            allowed_currencies=[c.strip().upper() for c in merged["allowed_currencies"] if c.strip()],
            approve_new_recipients=bool(merged["approve_new_recipients"]),
            auto_reminders=bool(merged["auto_reminders"]),
            reminder_after_hours=float(merged["reminder_after_hours"]),
            reminder_every_hours=float(merged["reminder_every_hours"]),
            max_reminders=int(merged["max_reminders"]),
        )
        rules.check()
        return rules

    def check(self) -> None:
        for name in ("spending_limit", "daily_limit", "hard_cap", "invoice_cap"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.spending_limit > self.hard_cap:
            raise ValueError("spending_limit cannot exceed hard_cap")
        if not self.allowed_currencies:
            raise ValueError("allow at least one currency")
        if self.reminder_after_hours < 0 or self.reminder_every_hours <= 0:
            raise ValueError("reminder timings must be positive")
        if not 0 <= self.max_reminders <= 10:
            raise ValueError("max_reminders must be between 0 and 10")


class ValidationError(ValueError):
    """The proposed action is malformed. It is rejected before any rule is consulted."""


def money(value: Any) -> Decimal:
    try:
        d = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise ValidationError(f"'{value}' is not a valid amount")
    if not d.is_finite():
        raise ValidationError(f"'{value}' is not a valid amount")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def email(value: Any) -> str:
    v = str(value or "").strip().lower()
    if not EMAIL_RE.match(v):
        raise ValidationError(f"'{value}' is not a valid email address")
    return v


def currency(value: Any) -> str:
    v = str(value or "USD").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", v):
        raise ValidationError(f"'{value}' is not a currency code")
    return v


@dataclass
class Verdict:
    decision: Decision
    reasons: list[str]
    action: dict[str, Any]          # the normalized, validated action

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "reasons": self.reasons, "action": self.action}


# ---------------------------------------------------------------------------
# Normalizers: turn the model's proposal into a clean, typed action or raise.
# ---------------------------------------------------------------------------

def normalize_payout(p: dict[str, Any]) -> dict[str, Any]:
    amount = money(p.get("amount"))
    if amount <= 0:
        raise ValidationError("payout amount must be greater than zero")
    note = str(p.get("note") or "").strip()[:255]
    return {
        "type": "send_payout",
        "recipient_email": email(p.get("recipient_email")),
        "amount": str(amount),
        "currency": currency(p.get("currency")),
        "note": note,
    }


def normalize_invoice(p: dict[str, Any]) -> dict[str, Any]:
    items_in = p.get("items") or []
    if not isinstance(items_in, list) or not items_in:
        raise ValidationError("an invoice needs at least one line item")
    if len(items_in) > 25:
        raise ValidationError("an invoice can have at most 25 line items")
    items, total = [], Decimal("0.00")
    for it in items_in:
        name = str(it.get("description") or it.get("name") or "").strip()
        if not name:
            raise ValidationError("every line item needs a description")
        qty = it.get("quantity", 1)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise ValidationError(f"quantity '{qty}' must be a whole number")
        if qty <= 0 or qty > 10000:
            raise ValidationError("quantity must be between 1 and 10000")
        unit = money(it.get("unit_amount"))
        if unit <= 0:
            raise ValidationError("line item amounts must be greater than zero")
        items.append({"description": name[:200], "quantity": qty, "unit_amount": str(unit)})
        total += unit * qty
    due = p.get("due_in_days", 14)
    try:
        due = int(due)
    except (TypeError, ValueError):
        raise ValidationError("due_in_days must be a whole number")
    if not 0 <= due <= 365:
        raise ValidationError("due_in_days must be between 0 and 365")
    return {
        "type": "create_invoice",
        "recipient_email": email(p.get("recipient_email")),
        "recipient_name": str(p.get("recipient_name") or "").strip()[:100],
        "currency": currency(p.get("currency")),
        "items": items,
        "total": str(total.quantize(Decimal("0.01"))),
        "note": str(p.get("note") or "").strip()[:1000],
        "due_in_days": due,
        "send": bool(p.get("send", True)),
    }


# ---------------------------------------------------------------------------
# Policy: the deterministic gate between a valid action and PayPal.
# ---------------------------------------------------------------------------

def evaluate_payout(
    action: dict[str, Any],
    rules: Rules,
    *,
    spent_today: Decimal,
    known_recipients: set[str],
) -> Verdict:
    a = Decimal(action["amount"])
    deny, ask = [], []

    if action["currency"] not in rules.allowed_currencies:
        deny.append(f"{action['currency']} is not an allowed currency ({', '.join(rules.allowed_currencies)})")
    if a > rules.hard_cap:
        deny.append(f"{fmt(a)} is above the hard cap of {fmt(rules.hard_cap)}")
    if deny:
        return Verdict("deny", deny, action)

    if a > rules.spending_limit:
        ask.append(f"{fmt(a)} is above your auto-approve limit of {fmt(rules.spending_limit)}")
    if spent_today + a > rules.daily_limit:
        ask.append(
            f"this would bring today's auto-approved total to {fmt(spent_today + a)},"
            f" above your daily limit of {fmt(rules.daily_limit)}"
        )
    if rules.approve_new_recipients and action["recipient_email"] not in known_recipients:
        ask.append(f"{action['recipient_email']} has never been paid before")
    if ask:
        return Verdict("needs_approval", ask, action)

    return Verdict("auto_approve", [f"{fmt(a)} is within your limit of {fmt(rules.spending_limit)}"], action)


def evaluate_invoice(action: dict[str, Any], rules: Rules) -> Verdict:
    total = Decimal(action["total"])
    if action["currency"] not in rules.allowed_currencies:
        return Verdict("deny", [f"{action['currency']} is not an allowed currency"], action)
    if total > rules.invoice_cap:
        return Verdict("deny", [f"{fmt(total)} is above the invoice cap of {fmt(rules.invoice_cap)}"], action)
    # Invoices request money *in*, so they never spend the user's balance.
    return Verdict("auto_approve", ["invoices request money, so they do not use your spending limit"], action)


def fmt(d: Decimal) -> str:
    return f"${d:,.2f}"
