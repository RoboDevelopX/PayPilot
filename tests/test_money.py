from decimal import Decimal

import pytest

from app.money import MoneyError, from_cents, parse_amount_string, to_cents, to_money


def test_to_money_rounds_half_up_to_cents():
    assert to_money("10.005") == Decimal("10.01")
    assert to_money(5) == Decimal("5.00")


def test_floats_are_rejected():
    with pytest.raises(MoneyError):
        to_money(0.1)


@pytest.mark.parametrize("raw", ["450", "450.0", "450.00", "0.01", "9999999.99"])
def test_amount_strings_accepted(raw):
    assert parse_amount_string(raw) == Decimal(raw).quantize(Decimal("0.01"))


@pytest.mark.parametrize("raw", ["-5", "1e3", "$40", "40.001", "1,000", "", "NaN", "10000000"])
def test_amount_strings_rejected(raw):
    with pytest.raises(MoneyError):
        parse_amount_string(raw)


def test_cents_roundtrip():
    assert from_cents(to_cents(Decimal("123.45"))) == Decimal("123.45")
