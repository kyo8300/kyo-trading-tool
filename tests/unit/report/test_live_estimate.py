"""AC-22: the real-money cost estimate is computed from `CostAssumptions`
coefficients read from the rules file (R-23, Q10)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader.domain.models import Fill
from trader.domain.money import Money, Price, Quantity
from trader.report.live_estimate import estimate
from trader.rules.schema import CostAssumptions, load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _fill(fill_id: str, qty: int, price: str) -> Fill:
    return Fill(
        id=fill_id,
        order_id="order-1",
        filled_at=_NOW,
        qty=Quantity(qty),
        price=Price(Decimal(price)),
        fee=Money(Decimal("0")),
    )


def test_estimate_matches_hand_computed_values_with_valid_yaml_coefficients() -> None:
    rule_set = load_rules(FIXTURES / "valid.yaml").value
    cost = rule_set.cost_assumptions
    # slippage 0.5% / commission 0% (no cap) / fixed 0 / fx 1.0% -- matches spec's decided values.
    assert cost.slippage_pct_per_side == Decimal("0.5")
    assert cost.commission_pct == Decimal("0")
    assert cost.commission_cap_usd is None
    assert cost.commission_usd_per_order == Decimal("0")
    assert cost.fx_cost_pct_one_way == Decimal("1.0")

    fills = (_fill("f-1", 7, "10"), _fill("f-2", 8, "10"))  # notional 70 + 80 = 150

    result = estimate(
        paper_pnl=Money(Decimal("12.00")),
        fills=fills,
        order_count=1,
        capital=Money(Decimal("500")),
        cost=cost,
    )

    assert result.slippage_cost == Money(Decimal("0.75"))
    assert result.commission_cost == Money(Decimal("0"))
    assert result.fixed_commission_cost == Money(Decimal("0"))
    assert result.fx_cost == Money(Decimal("10.00"))
    assert result.estimated_live_pnl == Money(Decimal("1.25"))


def test_estimate_applies_commission_cap_per_fill() -> None:
    cost = CostAssumptions(
        slippage_pct_per_side=Decimal("0"),
        commission_pct=Decimal("0.132"),
        commission_cap_usd=Decimal("22"),
        commission_usd_per_order=Decimal("0"),
        fx_cost_pct_one_way=Decimal("0"),
    )
    # notional 20000 -> uncapped commission 26.40, capped to 22.00.
    fills = (_fill("f-1", 1000, "20"),)

    result = estimate(
        paper_pnl=Money(Decimal("0")),
        fills=fills,
        order_count=1,
        capital=Money(Decimal("500")),
        cost=cost,
    )

    assert result.commission_cost == Money(Decimal("22.00"))


def test_estimate_sums_fixed_commission_across_order_count() -> None:
    cost = CostAssumptions(
        slippage_pct_per_side=Decimal("0"),
        commission_pct=Decimal("0"),
        commission_cap_usd=None,
        commission_usd_per_order=Decimal("1.50"),
        fx_cost_pct_one_way=Decimal("0"),
    )

    result = estimate(
        paper_pnl=Money(Decimal("0")),
        fills=(),
        order_count=3,
        capital=Money(Decimal("500")),
        cost=cost,
    )

    assert result.fixed_commission_cost == Money(Decimal("4.50"))


def test_estimate_with_zero_fills_and_zero_capital_is_paper_pnl() -> None:
    cost = CostAssumptions(
        slippage_pct_per_side=Decimal("0.5"),
        commission_pct=Decimal("0"),
        commission_cap_usd=None,
        commission_usd_per_order=Decimal("0"),
        fx_cost_pct_one_way=Decimal("1.0"),
    )

    result = estimate(
        paper_pnl=Money(Decimal("42.00")),
        fills=(),
        order_count=0,
        capital=Money(Decimal("0")),
        cost=cost,
    )

    assert result.estimated_live_pnl == Money(Decimal("42.00"))
