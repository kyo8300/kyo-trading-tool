"""AC-12: sell-side rule checks (R-13). Priority: stop loss -> holding
period -> partial take profit -> trailing stop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.domain.models import ExitReason, Position
from trader.domain.money import Price, Quantity
from trader.domain.result import Ok
from trader.rules.exit_checks import check_exit
from trader.rules.schema import load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"
_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _rules():
    result = load_rules(FIXTURES / "valid.yaml")
    assert isinstance(result, Ok)
    return result.value


def _position(**overrides: object) -> Position:
    defaults: dict[str, object] = {
        "ticker": "AAPL",
        "qty": Quantity(7),
        "avg_cost": Price(Decimal("100")),
        "opened_at": _NOW - timedelta(days=1),
        "high_watermark": Price(Decimal("100")),
        "partial_tp_done": False,
    }
    defaults.update(overrides)
    return Position(**defaults)  # type: ignore[arg-type]


def test_stop_loss_triggers_when_price_falls_15_pct() -> None:
    rules = _rules()
    position = _position(avg_cost=Price(Decimal("100")), high_watermark=Price(Decimal("105")))

    signal = check_exit(position, Price(Decimal("84")), rules, _NOW)

    assert signal is not None
    assert signal.reason == ExitReason.stop_loss
    assert signal.fraction == Decimal(1)


def test_stop_loss_fires_before_trailing_stop_when_partial_tp_not_done() -> None:
    rules = _rules()
    # $100 -> $105 (never triggers +30% partial TP) -> $84: stop loss fires
    # first, since partial_tp_done is False so trailing stop never applies.
    position = _position(
        avg_cost=Price(Decimal("100")),
        high_watermark=Price(Decimal("105")),
        partial_tp_done=False,
    )

    signal = check_exit(position, Price(Decimal("84")), rules, _NOW)

    assert signal is not None
    assert signal.reason == ExitReason.stop_loss


def test_trailing_stop_triggers_after_partial_take_profit_done() -> None:
    rules = _rules()
    # $100 -> $150 (high watermark) -> $120: trailing stop at 150 * 0.8 = 120.
    position = _position(
        avg_cost=Price(Decimal("100")),
        high_watermark=Price(Decimal("150")),
        partial_tp_done=True,
    )

    signal = check_exit(position, Price(Decimal("120")), rules, _NOW)

    assert signal is not None
    assert signal.reason == ExitReason.trailing_stop
    assert signal.fraction == Decimal(1)


def test_trailing_stop_does_not_apply_before_partial_take_profit() -> None:
    rules = _rules()
    # Same $150 high watermark / $120 price, but partial TP not done yet:
    # trailing stop must not fire (and price is above stop loss level).
    position = _position(
        avg_cost=Price(Decimal("100")),
        high_watermark=Price(Decimal("150")),
        partial_tp_done=False,
    )

    signal = check_exit(position, Price(Decimal("120")), rules, _NOW)

    assert signal is None


def test_partial_take_profit_triggers_at_plus_30_pct() -> None:
    rules = _rules()
    position = _position(avg_cost=Price(Decimal("100")), partial_tp_done=False)

    signal = check_exit(position, Price(Decimal("130")), rules, _NOW)

    assert signal is not None
    assert signal.reason == ExitReason.partial_take_profit
    assert signal.fraction == Decimal("0.5")


def test_partial_take_profit_does_not_retrigger_once_done() -> None:
    rules = _rules()
    position = _position(
        avg_cost=Price(Decimal("100")),
        high_watermark=Price(Decimal("130")),
        partial_tp_done=True,
    )

    signal = check_exit(position, Price(Decimal("130")), rules, _NOW)

    # partial_tp_done=True routes to the trailing-stop check instead; 130 is
    # above 130 * 0.8 = 104, so no trailing stop fires either.
    assert signal is None


def test_max_holding_days_triggers_at_120_days() -> None:
    rules = _rules()
    position = _position(
        avg_cost=Price(Decimal("100")),
        opened_at=_NOW - timedelta(days=120),
        partial_tp_done=False,
    )

    signal = check_exit(position, Price(Decimal("100")), rules, _NOW)

    assert signal is not None
    assert signal.reason == ExitReason.max_holding_days
    assert signal.fraction == Decimal(1)


def test_max_holding_days_does_not_trigger_before_120_days() -> None:
    rules = _rules()
    position = _position(
        avg_cost=Price(Decimal("100")),
        opened_at=_NOW - timedelta(days=119),
        partial_tp_done=False,
    )

    signal = check_exit(position, Price(Decimal("100")), rules, _NOW)

    assert signal is None


def test_no_signal_when_no_condition_is_met() -> None:
    rules = _rules()
    position = _position(avg_cost=Price(Decimal("100")), partial_tp_done=False)

    signal = check_exit(position, Price(Decimal("102")), rules, _NOW)

    assert signal is None
