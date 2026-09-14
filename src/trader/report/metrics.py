"""R-22: report metrics computed from the ledger's closed trades, decisions,
equity snapshots, and open positions.

`compute` is a pure function (N-7): it never touches the database itself --
the caller (the `report` CLI command) reads rows via `ledger.*` and passes
plain domain objects in. Amounts are `Money`/`Decimal` throughout (N-1).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from trader.domain.models import Action, Decision, EquitySnapshot, ExitReason, Position, Trade
from trader.domain.money import Money

_PCT_QUANTUM = Decimal("0.01")
_ZERO_MONEY = Money(Decimal(0))
_ZERO_PCT = Decimal("0.00")

_SKIP = "skip"
_HOLD = "hold"


@dataclass(frozen=True, slots=True)
class Period:
    """An inclusive date range used to filter trades/decisions for a report.

    Either bound may be `None`, meaning "unbounded" on that side.
    """

    start: date | None
    end: date | None


@dataclass(frozen=True, slots=True)
class Metrics:
    """R-22's report indicators for one `Period`."""

    period_pnl: Money
    trade_count: int
    win_count: int
    loss_count: int
    win_rate_pct: Decimal
    avg_pnl: Money
    max_drawdown_pct: Decimal
    exit_reason_counts: Mapping[ExitReason, int]
    decision_counts: Mapping[str, int]
    open_positions: tuple[Position, ...]


def _in_period(value: date, period: Period) -> bool:
    if period.start is not None and value < period.start:
        return False
    if period.end is not None and value > period.end:
        return False
    return True


def filter_trades_by_period(trades: Sequence[Trade], period: Period) -> tuple[Trade, ...]:
    """Return the subset of `trades` whose `closed_at` date falls in `period`.

    Exposed separately from `compute` so callers (e.g. the `report` CLI) can
    render the same period-filtered trade list that `Metrics` was computed
    from, without duplicating the filter predicate.
    """
    return tuple(t for t in trades if _in_period(_as_date(t.closed_at), period))


def _quantize_pct(value: Decimal) -> Decimal:
    return value.quantize(_PCT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _max_drawdown_pct(snapshots: Sequence[EquitySnapshot]) -> Decimal:
    """Max of `(running_peak - equity) / running_peak * 100` over `snapshots`.

    `snapshots` is expected oldest-first (as returned by
    `ledger.portfolio_repository.list_equity_snapshots`); each snapshot's own
    `peak_equity` is ignored in favor of a running peak recomputed here so
    the result only depends on the sequence of equities given.
    """
    if not snapshots:
        return _ZERO_PCT

    running_peak = snapshots[0].equity.amount
    worst = Decimal(0)
    for snapshot in snapshots:
        running_peak = max(running_peak, snapshot.equity.amount)
        if running_peak > 0:
            drawdown = (running_peak - snapshot.equity.amount) / running_peak * Decimal(100)
            worst = max(worst, drawdown)
    return _quantize_pct(worst)


def _decision_bucket(decision: Decision) -> str:
    if decision.action is Action.skip:
        return _SKIP
    if decision.action is Action.hold:
        return _HOLD
    return decision.rule_check.value


def compute(
    trades: Sequence[Trade],
    decisions: Sequence[Decision],
    snapshots: Sequence[EquitySnapshot],
    positions: Sequence[Position],
    period: Period,
) -> Metrics:
    """Compute R-22's report metrics for `period`.

    `trades` are filtered by `closed_at`'s date and `decisions` by
    `decided_at`'s date; `snapshots` and `positions` reflect the ledger's
    full/current state regardless of `period` (drawdown is a running measure,
    open positions are a point-in-time snapshot).
    """
    period_trades = filter_trades_by_period(trades, period)
    period_decisions = tuple(d for d in decisions if _in_period(_as_date(d.decided_at), period))

    trade_count = len(period_trades)
    period_pnl = sum((t.realized_pnl for t in period_trades), _ZERO_MONEY)
    win_count = sum(1 for t in period_trades if t.realized_pnl.amount > 0)
    loss_count = sum(1 for t in period_trades if t.realized_pnl.amount < 0)

    if trade_count:
        win_rate_pct = _quantize_pct(Decimal(win_count) / Decimal(trade_count) * Decimal(100))
        avg_pnl = Money(period_pnl.amount / Decimal(trade_count))
    else:
        win_rate_pct = _ZERO_PCT
        avg_pnl = _ZERO_MONEY

    exit_reason_counts = Counter(t.exit_reason for t in period_trades)
    decision_counts = Counter(_decision_bucket(d) for d in period_decisions)

    return Metrics(
        period_pnl=period_pnl,
        trade_count=trade_count,
        win_count=win_count,
        loss_count=loss_count,
        win_rate_pct=win_rate_pct,
        avg_pnl=avg_pnl,
        max_drawdown_pct=_max_drawdown_pct(snapshots),
        exit_reason_counts=dict(exit_reason_counts),
        decision_counts=dict(decision_counts),
        open_positions=tuple(positions),
    )


def _as_date(value: datetime) -> date:
    return value.date()
