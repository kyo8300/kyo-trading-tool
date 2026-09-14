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
