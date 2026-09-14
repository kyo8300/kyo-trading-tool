"""Daily/weekly loss and drawdown breach detection (R-19)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader.domain.money import Money
from trader.domain.result import Ok
from trader.rules.loss_limits import evaluate, trading_day, trading_week_start
from trader.rules.schema import derive_limits, load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"


def _limits():
    result = load_rules(FIXTURES / "valid.yaml")
    assert isinstance(result, Ok)
    return derive_limits(result.value)


def test_daily_loss_limit_reached() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("-10.00")),
        unrealized_now=Money(Decimal("-5.00")),
        realized_week=Money(Decimal("0")),
        equity=Money(Decimal("500")),
        peak_equity=Money(Decimal("500")),
    )

    assert breach is not None
    assert breach.kind == "daily"
    assert "-15.00" in breach.reason


def test_weekly_loss_limit_reached() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("0")),
        unrealized_now=Money(Decimal("-5.00")),
        realized_week=Money(Decimal("-25.00")),
        equity=Money(Decimal("500")),
        peak_equity=Money(Decimal("500")),
    )

    assert breach is not None
    assert breach.kind == "weekly"
    assert "-30.00" in breach.reason


def test_drawdown_limit_reached() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("0")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("0")),
        equity=Money(Decimal("425.00")),
        peak_equity=Money(Decimal("500.00")),
    )

    assert breach is not None
    assert breach.kind == "drawdown"
    assert breach.observed == Decimal("15")
    assert "15" in breach.reason


def test_drawdown_not_checked_when_peak_is_non_positive() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("0")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("0")),
        equity=Money(Decimal("0")),
        peak_equity=Money(Decimal("0")),
    )

    assert breach is None


def test_none_reached_returns_none() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("-5.00")),
        unrealized_now=Money(Decimal("-2.00")),
        realized_week=Money(Decimal("-10.00")),
        equity=Money(Decimal("490.00")),
        peak_equity=Money(Decimal("500.00")),
    )

    assert breach is None


def test_daily_is_checked_before_weekly_and_drawdown() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("-20.00")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("-40.00")),
        equity=Money(Decimal("400.00")),
        peak_equity=Money(Decimal("500.00")),
    )

    assert breach is not None
    assert breach.kind == "daily"


def test_trading_day_uses_america_new_york_calendar_date() -> None:
    # 03:00 UTC in January is 22:00 the previous day in New York (UTC-5).
    now_utc = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)

    assert trading_day(now_utc) == datetime(2026, 1, 14, tzinfo=UTC).date()


def test_trading_day_same_day_when_well_within_new_york_hours() -> None:
    now_utc = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)

    assert trading_day(now_utc) == datetime(2026, 1, 15, tzinfo=UTC).date()


def test_trading_week_start_is_the_preceding_monday() -> None:
    # 2026-01-15 is a Thursday in New York.
    now_utc = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)

    assert trading_week_start(now_utc) == datetime(2026, 1, 12, tzinfo=UTC).date()


def test_trading_week_start_on_monday_is_itself() -> None:
    # 2026-01-12 is a Monday in New York.
    now_utc = datetime(2026, 1, 12, 18, 0, tzinfo=UTC)

    assert trading_week_start(now_utc) == datetime(2026, 1, 12, tzinfo=UTC).date()
