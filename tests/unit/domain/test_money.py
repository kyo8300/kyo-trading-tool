"""Money quantization, pct derivation, and canonical decimal strings."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trader.domain.money import (
    Money,
    Price,
    Quantity,
    QuantityError,
    from_canonical,
    pct_of,
    to_canonical,
)
from trader.domain.result import Err, Ok


def test_money_quantizes_to_cent_with_round_half_even() -> None:
    assert Money(Decimal("10.005")).amount == Decimal("10.00")
    assert Money(Decimal("10.015")).amount == Decimal("10.02")


def test_price_quantizes_to_four_decimal_places() -> None:
    assert Price(Decimal("10.123456")).amount == Decimal("10.1235")


@pytest.mark.parametrize(
    ("capital", "pct", "expected"),
    [
        (Decimal("500"), Decimal("15"), Decimal("75.00")),
        (Decimal("500"), Decimal("3"), Decimal("15.00")),
        (Decimal("500"), Decimal("6"), Decimal("30.00")),
    ],
)
def test_pct_of_derives_absolute_amount(capital: Decimal, pct: Decimal, expected: Decimal) -> None:
    assert pct_of(Money(capital), pct) == Money(expected)


def test_money_arithmetic() -> None:
    assert Money(Decimal("10")) + Money(Decimal("5")) == Money(Decimal("15"))
    assert Money(Decimal("10")) - Money(Decimal("5")) == Money(Decimal("5"))
    assert Money(Decimal("10")) * Decimal("2") == Money(Decimal("20"))
    assert Money(Decimal("5")) < Money(Decimal("10"))
    assert Money(Decimal("10")) >= Money(Decimal("10"))


def test_quantity_parse_accepts_whole_numbers() -> None:
    result = Quantity.parse("7")
    assert isinstance(result, Ok)
    assert result.value.shares == 7


def test_quantity_parse_rejects_non_integer() -> None:
    result = Quantity.parse("1.5")
    assert isinstance(result, Err)
    assert isinstance(result.error, QuantityError)


def test_quantity_parse_rejects_negative() -> None:
    result = Quantity.parse("-1")
    assert isinstance(result, Err)


def test_quantity_parse_rejects_garbage() -> None:
    result = Quantity.parse("not-a-number")
    assert isinstance(result, Err)


@pytest.mark.parametrize("value", ["500", "75.00", "-15.25", "0"])
def test_canonical_roundtrip(value: str) -> None:
    decimal_value = Decimal(value)
    canonical = to_canonical(decimal_value)
    result = from_canonical(canonical)
    assert isinstance(result, Ok)
    assert result.value == decimal_value


def test_from_canonical_rejects_invalid_string() -> None:
    result = from_canonical("not-a-decimal")
    assert isinstance(result, Err)
