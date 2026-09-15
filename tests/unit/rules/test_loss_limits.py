"""Daily/weekly loss and drawdown breach detection (R-19)."""

from __future__ import annotations

from datetime import UTC, date, datetime
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


def test_daily_loss_boundary_14_99_is_none() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("-14.99")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("0")),
        equity=Money(Decimal("500")),
        peak_equity=Money(Decimal("500")),
    )

    assert breach is None


def test_daily_loss_boundary_15_00_is_breach() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("-15.00")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("0")),
        equity=Money(Decimal("500")),
        peak_equity=Money(Decimal("500")),
    )

    assert breach is not None
    assert breach.kind == "daily"


def test_weekly_loss_boundary_29_99_is_none() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("0")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("-29.99")),
        equity=Money(Decimal("500")),
        peak_equity=Money(Decimal("500")),
    )

    assert breach is None


def test_weekly_loss_boundary_30_00_is_breach() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("0")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("-30.00")),
        equity=Money(Decimal("500")),
        peak_equity=Money(Decimal("500")),
    )

    assert breach is not None
    assert breach.kind == "weekly"


def test_drawdown_boundary_14_99_pct_is_none() -> None:
    limits = _limits()

    breach = evaluate(
        limits,
        realized_today=Money(Decimal("0")),
        unrealized_now=Money(Decimal("0")),
        realized_week=Money(Decimal("0")),
        equity=Money(Decimal("425.05")),
        peak_equity=Money(Decimal("500.00")),
    )

    assert breach is None


def test_drawdown_boundary_15_00_pct_is_breach() -> None:
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


def test_trading_day_matches_spec_example_utc_to_ny_monday() -> None:
    # plan.md リスク 4 / tester instructions: UTC 2026-09-15T03:00 is NY
    # 2026-09-14 (Monday); the week start is that same Monday.
    now_utc = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)

    assert trading_day(now_utc) == date(2026, 9, 14)
    assert trading_week_start(now_utc) == date(2026, 9, 14)


def test_trading_week_start_crosses_utc_sunday_midnight_boundary() -> None:
    # UTC Monday 01:00 is still Sunday 21:00 in New York (EDT, UTC-4), so it
    # belongs to the *previous* week (Monday 2026-09-07), not the week that
    # starts on the UTC-calendar Monday (2026-09-14).
    now_utc = datetime(2026, 9, 14, 1, 0, tzinfo=UTC)

    assert trading_day(now_utc) == date(2026, 9, 13)
    assert trading_week_start(now_utc) == date(2026, 9, 7)
