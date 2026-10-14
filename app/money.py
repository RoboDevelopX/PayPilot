"""Money helpers. Every amount in PayPilot is a `Decimal` with two places.

Why this works: floats are binary fractions, so 0.1 + 0.2 != 0.3. Payment APIs
(PayPal included) exchange amounts as decimal *strings* like "450.00", and we
keep them as `Decimal` end to end, so what the user typed is exactly what PayPal sees.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
AMOUNT_PATTERN = r"^\d{1,7}(\.\d{1,2})?$"  # what the LLM is allowed to emit: "450", "39.99"
_AMOUNT_RE = re.compile(AMOUNT_PATTERN)


class MoneyError(ValueError):
    pass


def to_money(value: str | int | Decimal) -> Decimal:
    """Parse a decimal string or int into a 2-place Decimal. Floats are rejected on purpose."""
    if isinstance(value, float):
        raise MoneyError("floats are not allowed for money; pass a string like '12.50'")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation as e:
        raise MoneyError(f"{value!r} is not a valid amount") from e
    if not d.is_finite():
        raise MoneyError(f"{value!r} is not a valid amount")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount_string(value: str) -> Decimal:
    """Strict parser for model-proposed amounts: digits, optional 1-2 decimals, nothing else."""
    if not isinstance(value, str) or not _AMOUNT_RE.match(value.strip()):
        raise MoneyError(f"{value!r} must look like 450 or 450.00")
    return to_money(value)


def to_cents(d: Decimal) -> int:
    return int((to_money(d) * 100).to_integral_value())


def from_cents(c: int) -> Decimal:
    return (Decimal(c) / 100).quantize(CENT)


def fmt(d: Decimal, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency)
    return f"{symbol}{d:,.2f}" if symbol else f"{d:,.2f} {currency}"
