"""AC-11: pre-buy rule checks (R-12). Each rejection is tested at least once
and rejection reasons carry both the derived amount and the ratio.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trader.domain.money import Money, Price
from trader.domain.result import Err, Ok
from trader.rules.entry_checks import check_entry
from trader.rules.loss_limits import LossLimitBreach
from trader.rules.schema import derive_limits, load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"


def _rules_and_limits():
    result = load_rules(FIXTURES / "valid.yaml")
    assert isinstance(result, Ok)
    rules = result.value
    return rules, derive_limits(rules)


def test_excluded_ticker_is_rejected() -> None:
    rules, limits = _rules_and_limits()
    excluded_entry = rules.entry.model_copy(update={"excluded_tickers": ("BADCO",)})
    rules = rules.model_copy(update={"entry": excluded_entry})

    result = check_entry(
        "BADCO",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "excluded_ticker"
    assert "BADCO" in result.error.reason


def test_already_held_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "AAPL",
        rules,
        limits,
        holdings=("AAPL",),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "already_held"


def test_max_concurrent_positions_is_rejected() -> None:
    rules, limits = _rules_and_limits()
    holdings = ("A", "B", "C", "D", "E")  # max_concurrent_positions = 5

    result = check_entry(
        "F",
        rules,
        limits,
        holdings=holdings,
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "max_concurrent_positions"
    assert "5" in result.error.reason


def test_market_closed_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "AAPL",
        rules,
        limits,
        holdings=(),
        market_open=False,
        latest_price=Price(Decimal("10")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "market_closed"


def test_loss_limit_reached_is_rejected() -> None:
    rules, limits = _rules_and_limits()
    breach = LossLimitBreach(
        kind="daily",
        observed=Money(Decimal("-15.00")),
        limit=Money(Decimal("15.00")),
        reason="daily P&L -15.00 <= -15.00",
    )

    result = check_entry(
        "AAPL",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=500_000,
        loss_state=breach,
    )

    assert isinstance(result, Err)
    assert result.error.code == "loss_limit_reached"
    assert result.error.reason == breach.reason


def test_price_below_min_price_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "PENNY",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("1.50")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "min_price"
    assert "1.5" in result.error.reason
    assert "2" in result.error.reason


def test_avg_volume_below_minimum_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "THIN",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=1_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "min_avg_volume"
    assert "1000" in result.error.reason
    assert "200000" in result.error.reason


def test_price_over_per_ticker_limit_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "PRCY",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("80")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "price_over_limit"
    assert "80.0000" in result.error.reason
    assert "75.00" in result.error.reason
    assert "15" in result.error.reason
    assert "500" in result.error.reason


def test_accepted_candidate_yields_floor_share_count() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "GOOD",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Ok)
    plan = result.value
    assert plan.qty.shares == 7
    assert plan.notional == Money(Decimal("70.00"))
    assert plan.limit == Money(Decimal("75.00"))


def test_excluded_ticker_wins_over_already_held_by_check_order() -> None:
    # AC-11: when multiple rejection conditions hold at once, the plan's
    # documented order (excluded -> already_held -> ...) decides which
    # reason comes back, not an arbitrary one.
    rules, limits = _rules_and_limits()
    excluded_entry = rules.entry.model_copy(update={"excluded_tickers": ("AAPL",)})
    rules = rules.model_copy(update={"entry": excluded_entry})

    result = check_entry(
        "AAPL",
        rules,
        limits,
        holdings=("AAPL",),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "excluded_ticker"


def test_price_equal_to_limit_buys_exactly_one_share() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "EDGE",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("75")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Ok)
    plan = result.value
    assert plan.qty.shares == 1
    assert plan.notional == Money(Decimal("75.00"))


def test_price_slightly_over_limit_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "EDGE",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("75.01")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "price_over_limit"


def test_floor_share_count_leaves_a_fractional_remainder_unbought() -> None:
    # $75 limit / $11 price = 6.81... shares -> floor to 6, notional 66.00,
    # not a fractional or rounded-up share (spec "整数株の制約").
    rules, limits = _rules_and_limits()

    result = check_entry(
        "FRAC",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("11")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Ok)
    plan = result.value
    assert plan.qty.shares == 6
    assert plan.notional == Money(Decimal("66.00"))


def test_min_price_usd_boundary_exactly_two_is_accepted() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "MINP",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("2.00")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Ok)


def test_min_price_usd_boundary_just_below_two_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "MINP",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("1.9999")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "min_price"


def test_avg_volume_boundary_exactly_at_minimum_is_accepted() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "VOL",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=200_000,
        loss_state=None,
    )

    assert isinstance(result, Ok)


def test_avg_volume_boundary_one_below_minimum_is_rejected() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "VOL",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("10")),
        avg_volume=199_999,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert result.error.code == "min_avg_volume"


def test_price_over_limit_reason_includes_derived_amount_and_ratio() -> None:
    rules, limits = _rules_and_limits()

    result = check_entry(
        "PRCY",
        rules,
        limits,
        holdings=(),
        market_open=True,
        latest_price=Price(Decimal("80")),
        avg_volume=500_000,
        loss_state=None,
    )

    assert isinstance(result, Err)
    assert "75.00" in result.error.reason
    assert "15%" in result.error.reason
