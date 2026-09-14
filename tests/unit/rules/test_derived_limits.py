"""AC-11: absolute limits are correctly derived from `capital_usd * pct`."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trader.domain.money import Money
from trader.domain.result import Ok
from trader.rules.schema import derive_limits, load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"


def test_derive_limits_for_capital_500() -> None:
    result = load_rules(FIXTURES / "valid.yaml")
    assert isinstance(result, Ok)
    rule_set = result.value

    limits = derive_limits(rule_set)

    assert limits.max_notional_per_ticker == Money(Decimal("75.00"))
    assert limits.max_daily_loss == Money(Decimal("15.00"))
    assert limits.max_weekly_loss == Money(Decimal("30.00"))
    assert limits.max_drawdown_pct == Decimal("15")
