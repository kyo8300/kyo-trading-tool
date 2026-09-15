"""Money / Price / Quantity: the only representations of amounts in `src/`.

`decimal.Decimal` is used exclusively (N-1). `float` must never appear in
this module's type annotations, `float()` calls, or return values --
`scripts/check_no_float_money.py` enforces this statically (AC-23).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from trader.domain.result import Err, Ok, Result

_CENT = Decimal("0.01")
_PRICE_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class Money:
    """A USD amount, quantized to the cent using ROUND_HALF_EVEN."""

    amount: Decimal

    def __post_init__(self) -> None:
        quantized = self.amount.quantize(_CENT, rounding=ROUND_HALF_EVEN)
        object.__setattr__(self, "amount", quantized)

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        return Money(self.amount - other.amount)

    def __mul__(self, factor: Decimal | int) -> Money:
        return Money(self.amount * Decimal(factor))

    def __lt__(self, other: Money) -> bool:
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        return self.amount >= other.amount


@dataclass(frozen=True, slots=True)
class Price:
    """A per-share price, quantized to 4 decimal places."""

    amount: Decimal

    def __post_init__(self) -> None:
        quantized = self.amount.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)
        object.__setattr__(self, "amount", quantized)

    def __mul__(self, factor: Decimal | int) -> Price:
        return Price(self.amount * Decimal(factor))

    def __lt__(self, other: Price) -> bool:
        return self.amount < other.amount

    def __le__(self, other: Price) -> bool:
        return self.amount <= other.amount

    def __gt__(self, other: Price) -> bool:
        return self.amount > other.amount

    def __ge__(self, other: Price) -> bool:
        return self.amount >= other.amount


class QuantityError(ValueError):
    """A `Quantity` was requested for a non-integer or negative value."""


@dataclass(frozen=True, slots=True)
class Quantity:
    """A whole number of shares (v1 trades integer shares only)."""

    shares: int

    @staticmethod
    def parse(value: str) -> Result[Quantity, QuantityError]:
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            return Err(QuantityError(f"'{value}' is not a valid number"))
        if decimal_value != decimal_value.to_integral_value():
            return Err(QuantityError(f"'{value}' is not a whole number of shares"))
        shares = int(decimal_value)
        if shares < 0:
            return Err(QuantityError(f"'{value}' must not be negative"))
        return Ok(Quantity(shares))


def pct_of(capital: Money, pct: Decimal) -> Money:
    """Derive an absolute `Money` amount as `capital * pct / 100` (R-9)."""
    return Money(capital.amount * pct / Decimal(100))


def to_canonical(value: Decimal) -> str:
    """Render `value` as a plain (non-scientific) decimal string for storage."""
    return format(value, "f")


class CanonicalDecimalError(ValueError):
    """A stored canonical decimal string could not be parsed."""


def from_canonical(value: str) -> Result[Decimal, CanonicalDecimalError]:
    """Parse a canonical decimal string back into a `Decimal`.

    Only plain (non-scientific) finite decimal strings -- the shape produced
    by `to_canonical` -- are accepted. Decimal specials (`NaN`, `sNaN`,
    `Infinity`, `-Infinity`) and scientific notation (e.g. `1e5`) are
    rejected even though `decimal.Decimal` itself can parse them.
    """
    if "e" in value.lower():
        return Err(CanonicalDecimalError(f"'{value}' is not a valid canonical decimal"))
    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        return Err(CanonicalDecimalError(f"'{value}' is not a valid canonical decimal"))
    if not decimal_value.is_finite():
        return Err(CanonicalDecimalError(f"'{value}' is not a valid canonical decimal"))
    return Ok(decimal_value)
