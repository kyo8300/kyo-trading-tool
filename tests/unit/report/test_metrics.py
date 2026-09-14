"""AC-21: report metrics match hand-computed expectations for a known
dataset (R-22)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from trader.domain.models import (
    Action,
    Decision,
    EquitySnapshot,
    ExitReason,
    Origin,
    Position,
    RuleCheck,
    Trade,
)
from trader.domain.money import Money, Price, Quantity
from trader.report.metrics import Metrics, Period, compute

_IN_PERIOD = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
_OUT_OF_PERIOD = datetime(2025, 12, 1, 15, 0, tzinfo=UTC)
_PERIOD = Period(start=date(2026, 1, 1), end=date(2026, 1, 31))


def _trade(
    trade_id: str,
    realized_pnl: str,
    *,
    exit_reason: ExitReason = ExitReason.stop_loss,
    closed_at: datetime = _IN_PERIOD,
) -> Trade:
    return Trade(
        id=trade_id,
        ticker="ABCD",
        opened_at=closed_at,
        closed_at=closed_at,
        entry_decision_id=f"{trade_id}-entry",
        exit_decision_ids=(f"{trade_id}-exit",),
        exit_reason=exit_reason,
        realized_pnl=Money(Decimal(realized_pnl)),
        fees=Money(Decimal("0")),
        holding_days=3,
    )


def _decision(
    decision_id: str,
    *,
    action: Action,
    rule_check: RuleCheck,
    decided_at: datetime = _IN_PERIOD,
) -> Decision:
    return Decision(
        id=decision_id,
        cycle_id="cycle-1",
        decided_at=decided_at,
        mode="paper",
        ticker="ABCD",
        action=action,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=("m-1", "m-2"),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=rule_check,
        rule_check_reason="ok",
        proposed_notional=Money(Decimal("70.00")),
        reference_price=Price(Decimal("10")),
    )


def _snapshot(snapshot_date: str, equity: str) -> EquitySnapshot:
    return EquitySnapshot(
        snapshot_date=snapshot_date,
        mode="paper",
        cash=Money(Decimal(equity)),
        positions_value=Money(Decimal("0")),
        equity=Money(Decimal(equity)),
        peak_equity=Money(Decimal(equity)),
        drawdown_pct=Decimal("0"),
        taken_at=_IN_PERIOD,
    )


def test_period_pnl_win_rate_and_avg_pnl_for_known_trades() -> None:
    trades = (
        _trade("t-1", "20.00", exit_reason=ExitReason.partial_take_profit),
        _trade("t-2", "-14.00", exit_reason=ExitReason.stop_loss),
        _trade("t-3", "6.00", exit_reason=ExitReason.trailing_stop),
    )

    metrics = compute(trades, (), (), (), _PERIOD)

    assert metrics.period_pnl == Money(Decimal("12.00"))
    assert metrics.trade_count == 3
    assert metrics.win_count == 2
    assert metrics.loss_count == 1
    assert metrics.win_rate_pct == Decimal("66.67")
    assert metrics.avg_pnl == Money(Decimal("4.00"))


def test_trades_outside_period_are_excluded() -> None:
    trades = (
        _trade("t-1", "20.00", closed_at=_IN_PERIOD),
        _trade("t-2", "-50.00", closed_at=_OUT_OF_PERIOD),
    )

    metrics = compute(trades, (), (), (), _PERIOD)

    assert metrics.trade_count == 1
    assert metrics.period_pnl == Money(Decimal("20.00"))


def test_zero_trades_does_not_divide_by_zero() -> None:
    metrics = compute((), (), (), (), _PERIOD)

    assert metrics.trade_count == 0
    assert metrics.win_rate_pct == Decimal("0.00")
    assert metrics.avg_pnl == Money(Decimal("0.00"))
    assert metrics.period_pnl == Money(Decimal("0.00"))


def test_max_drawdown_from_equity_snapshot_sequence() -> None:
    snapshots = (
        _snapshot("2026-01-01", "500"),
        _snapshot("2026-01-02", "520"),
        _snapshot("2026-01-03", "470"),
    )

    metrics = compute((), (), snapshots, (), _PERIOD)

    assert metrics.max_drawdown_pct == Decimal("9.62")


def test_max_drawdown_is_zero_with_no_snapshots() -> None:
    metrics = compute((), (), (), (), _PERIOD)

    assert metrics.max_drawdown_pct == Decimal("0.00")


def test_exit_reason_counts_reflect_period_trades() -> None:
    trades = (
        _trade("t-1", "10.00", exit_reason=ExitReason.stop_loss),
        _trade("t-2", "10.00", exit_reason=ExitReason.stop_loss),
        _trade("t-3", "10.00", exit_reason=ExitReason.trailing_stop),
    )

    metrics = compute(trades, (), (), (), _PERIOD)

    assert metrics.exit_reason_counts == {
        ExitReason.stop_loss: 2,
        ExitReason.trailing_stop: 1,
    }


def test_decision_counts_split_passed_rejected_skip_and_hold() -> None:
    decisions = (
        _decision("d-1", action=Action.buy, rule_check=RuleCheck.passed),
        _decision("d-2", action=Action.sell, rule_check=RuleCheck.passed),
        _decision("d-3", action=Action.buy, rule_check=RuleCheck.rejected),
        _decision("d-4", action=Action.skip, rule_check=RuleCheck.passed),
        _decision("d-5", action=Action.hold, rule_check=RuleCheck.passed),
    )

    metrics = compute((), decisions, (), (), _PERIOD)

    assert metrics.decision_counts == {"passed": 2, "rejected": 1, "skip": 1, "hold": 1}


def test_decisions_outside_period_are_excluded_from_decision_counts() -> None:
    decisions = (
        _decision("d-1", action=Action.buy, rule_check=RuleCheck.passed, decided_at=_IN_PERIOD),
        _decision("d-2", action=Action.buy, rule_check=RuleCheck.passed, decided_at=_OUT_OF_PERIOD),
    )

    metrics = compute((), decisions, (), (), _PERIOD)

    assert metrics.decision_counts == {"passed": 1}


def test_open_positions_pass_through_unfiltered() -> None:
    position = Position(
        ticker="ABCD",
        qty=Quantity(7),
        avg_cost=Price(Decimal("10")),
        opened_at=_IN_PERIOD,
        high_watermark=Price(Decimal("12")),
        partial_tp_done=False,
    )

    metrics = compute((), (), (), (position,), _PERIOD)

    assert metrics.open_positions == (position,)


def test_unbounded_period_includes_everything() -> None:
    trades = (
        _trade("t-1", "20.00", closed_at=_IN_PERIOD),
        _trade("t-2", "-50.00", closed_at=_OUT_OF_PERIOD),
    )

    metrics = compute(trades, (), (), (), Period(start=None, end=None))

    assert metrics.trade_count == 2
    assert isinstance(metrics, Metrics)
