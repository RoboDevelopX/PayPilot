"""The policy engine: "the AI proposes, the code disposes".

Pure Python, no I/O, no clock, no database. Everything it needs (the policy,
today's and this month's spend, whether the recipient is allowlisted, the ML
signals) is passed in, so every rule is a unit-testable function of its inputs.

For a payout of amount a in currency c to recipient r, with spend so far
today s_d and this month s_m:

    BLOCK           if c not in C,  or  s_d + a > D,  or  s_m + a > M
    NEEDS_APPROVAL  if r not in R (new recipient),  or  a > L
    ESCALATED       if the anomaly detector flagged the payout
    AUTO            otherwise (a <= L, known recipient, within caps, not flagged)

Levels are ordered AUTO < NEEDS_APPROVAL < ESCALATED < BLOCK and the result is
the maximum over all rules. ML signals can only add ESCALATED: they can never
lower a level, never produce BLOCK, and never remove one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Any

from app.money import fmt, to_money

TONES = ("friendly", "firm", "final")


class Level(IntEnum):
    AUTO = 0
    NEEDS_APPROVAL = 1
    ESCALATED = 2
    BLOCK = 3

    @property
    def label(self) -> str:
        return {
            Level.AUTO: "auto-approved",
            Level.NEEDS_APPROVAL: "needs your approval",
            Level.ESCALATED: "escalated: needs a careful review",
            Level.BLOCK: "blocked",
        }[self]


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    auto_approve_limit: Decimal = Decimal("50.00")  # L
    daily_cap: Decimal = Decimal("200.00")  # D, hard block
    monthly_cap: Decimal = Decimal("1000.00")  # M, hard block
    invoice_cap: Decimal = Decimal("10000.00")  # largest invoice the agent may create
    allowed_currencies: frozenset[str] = frozenset({"USD"})  # C
    auto_remind: bool = True
    auto_remind_max_tone: str = "firm"  # friendly/firm go out alone; "final" needs approval

    def validate(self) -> Policy:
        for name in ("auto_approve_limit", "daily_cap", "monthly_cap", "invoice_cap"):
            if getattr(self, name) < 0:
                raise PolicyError(f"{name} cannot be negative")
        if self.auto_approve_limit > self.daily_cap:
            raise PolicyError("the auto-approve limit cannot be above the daily cap")
        if self.daily_cap > self.monthly_cap:
            raise PolicyError("the daily cap cannot be above the monthly cap")
        if not self.allowed_currencies:
            raise PolicyError("allow at least one currency")
        if any(len(c) != 3 or not c.isalpha() or not c.isupper() for c in self.allowed_currencies):
            raise PolicyError("currencies must be 3-letter codes like USD")
        if self.auto_remind_max_tone not in TONES:
            raise PolicyError(f"auto_remind_max_tone must be one of {', '.join(TONES)}")
        return self

    # --- (de)serialization for the DB and the settings form ---------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_approve_limit": str(self.auto_approve_limit),
            "daily_cap": str(self.daily_cap),
            "monthly_cap": str(self.monthly_cap),
            "invoice_cap": str(self.invoice_cap),
            "allowed_currencies": sorted(self.allowed_currencies),
            "auto_remind": self.auto_remind,
            "auto_remind_max_tone": self.auto_remind_max_tone,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        base = cls().to_dict()
        merged = {**base, **{k: v for k, v in data.items() if k in base}}
        try:
            currencies = merged["allowed_currencies"]
            if isinstance(currencies, str):
                currencies = currencies.split(",")
            return cls(
                auto_approve_limit=to_money(merged["auto_approve_limit"]),
                daily_cap=to_money(merged["daily_cap"]),
                monthly_cap=to_money(merged["monthly_cap"]),
                invoice_cap=to_money(merged["invoice_cap"]),
                allowed_currencies=frozenset(c.strip().upper() for c in currencies if c.strip()),
                auto_remind=_as_bool(merged["auto_remind"]),
                auto_remind_max_tone=str(merged["auto_remind_max_tone"]).lower(),
            ).validate()
        except (ValueError, TypeError) as e:
            raise PolicyError(str(e)) from e


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Spend:
    """Outgoing payouts (approved or executed) in the action's currency."""

    today: Decimal = Decimal("0.00")
    month: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class MLSignals:
    anomaly_flag: bool = False
    anomaly_score: float | None = None
    anomaly_reason: str | None = None


@dataclass(frozen=True)
class Check:
    rule: str
    passed: bool
    level_if_failed: Level
    reason: str


@dataclass(frozen=True)
class Decision:
    level: Level
    reasons: tuple[str, ...]
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def outcome(self) -> str:
        return self.level.name.lower()

    @property
    def can_execute_now(self) -> bool:
        return self.level == Level.AUTO

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.outcome,
            "label": self.level.label,
            "reasons": list(self.reasons),
            "checks": [
                {"rule": c.rule, "passed": c.passed, "reason": c.reason} for c in self.checks
            ],
        }


def _decide(checks: list[Check], ok_reason: str) -> Decision:
    failed = [c for c in checks if not c.passed]
    if not failed:
        return Decision(Level.AUTO, (ok_reason,), tuple(checks))
    level = max(c.level_if_failed for c in failed)
    # Show the reasons that set the final level first, then the rest.
    failed.sort(key=lambda c: -c.level_if_failed)
    return Decision(level, tuple(c.reason for c in failed), tuple(checks))


# --------------------------------------------------------------------------
# Payouts (money out): the full rule set.
# --------------------------------------------------------------------------
def evaluate_payout(
    *,
    amount: Decimal,
    currency: str,
    recipient: str,
    policy: Policy,
    spend: Spend,
    recipient_allowlisted: bool,
    ml: MLSignals | None = None,
) -> Decision:
    a = amount
    L, D, M = policy.auto_approve_limit, policy.daily_cap, policy.monthly_cap
    ml = ml or MLSignals()
    checks = [
        Check(
            "positive_amount",
            a > 0,
            Level.BLOCK,
            f"the amount must be greater than zero (got {fmt(a, currency)})",
        ),
        Check(
            "currency_allowlist",
            currency in policy.allowed_currencies,
            Level.BLOCK,
            f"{currency} is not an allowed currency (allowed: {', '.join(sorted(policy.allowed_currencies))})",
        ),
        Check(
            "daily_cap",
            spend.today + a <= D,
            Level.BLOCK,
            f"this would bring today's payouts to {fmt(spend.today + a, currency)}, "
            f"over your daily cap of {fmt(D, currency)}",
        ),
        Check(
            "monthly_cap",
            spend.month + a <= M,
            Level.BLOCK,
            f"this would bring this month's payouts to {fmt(spend.month + a, currency)}, "
            f"over your monthly cap of {fmt(M, currency)}",
        ),
        Check(
            "recipient_allowlist",
            recipient_allowlisted,
            Level.NEEDS_APPROVAL,
            f"{recipient} is a new recipient (not on your allowlist)",
        ),
        Check(
            "auto_approve_limit",
            a <= L,
            Level.NEEDS_APPROVAL,
            f"{fmt(a, currency)} is above your auto-approve limit of {fmt(L, currency)}",
        ),
        # Advisory ML input: can only raise the level to ESCALATED.
        Check(
            "anomaly_detector",
            not ml.anomaly_flag,
            Level.ESCALATED,
            "unusual payout flagged by the anomaly detector"
            + (f": {ml.anomaly_reason}" if ml.anomaly_reason else ""),
        ),
    ]
    return _decide(
        checks,
        f"{fmt(a, currency)} is within your auto-approve limit of {fmt(L, currency)}, "
        f"to an allowlisted recipient, and within your caps",
    )


# --------------------------------------------------------------------------
# Invoices (money in): they never spend the user's balance.
# --------------------------------------------------------------------------
def evaluate_invoice(*, total: Decimal, currency: str, policy: Policy) -> Decision:
    checks = [
        Check("positive_amount", total > 0, Level.BLOCK, "an invoice total must be above zero"),
        Check(
            "currency_allowlist",
            currency in policy.allowed_currencies,
            Level.BLOCK,
            f"{currency} is not an allowed currency (allowed: {', '.join(sorted(policy.allowed_currencies))})",
        ),
        Check(
            "invoice_cap",
            total <= policy.invoice_cap,
            Level.BLOCK,
            f"{fmt(total, currency)} is above your invoice cap of {fmt(policy.invoice_cap, currency)}",
        ),
    ]
    return _decide(
        checks, "invoices request money, so they are sent without using your spending limits"
    )


# --------------------------------------------------------------------------
# Reminders: auto-send up to the configured tone, otherwise ask.
# --------------------------------------------------------------------------
def evaluate_reminder(*, tone: str, invoice_open: bool, policy: Policy) -> Decision:
    if tone not in TONES:
        raise PolicyError(f"unknown tone {tone!r}")
    within_auto = policy.auto_remind and TONES.index(tone) <= TONES.index(
        policy.auto_remind_max_tone
    )
    checks = [
        Check(
            "invoice_open",
            invoice_open,
            Level.BLOCK,
            "the invoice is not open (already paid or cancelled), so no reminder is needed",
        ),
        Check(
            "auto_remind",
            within_auto,
            Level.NEEDS_APPROVAL,
            (
                "automatic reminders are off"
                if not policy.auto_remind
                else f"a {tone} reminder is above the tone you allow to send automatically "
                f"({policy.auto_remind_max_tone})"
            ),
        ),
    ]
    return _decide(checks, f"automatic {tone} reminders are allowed by your policy")


# --------------------------------------------------------------------------
# Policy changes proposed by the agent: always need the user's approval.
# --------------------------------------------------------------------------
def evaluate_policy_change(*, current: Policy, changes: dict[str, Any]) -> tuple[Decision, Policy]:
    try:
        proposed = Policy.from_dict({**current.to_dict(), **changes})
    except PolicyError as e:
        return Decision(Level.BLOCK, (f"invalid policy change: {e}",)), current
    return (
        Decision(
            Level.NEEDS_APPROVAL,
            (
                "changes to your rules always need your approval; the assistant cannot change them alone",
            ),
        ),
        proposed,
    )
