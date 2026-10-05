"""LR-2..LR-7: assemble `ReadinessInputs` from the ledger (read-only, no network)."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from trader.domain.models import ExitReason, Position, Trade
from trader.domain.money import Money
from trader.domain.result import Err, Ok, Result
from trader.ledger.benchmark_repository import list_benchmark_prices, list_position_marks
from trader.ledger.portfolio_repository import (
    list_equity_snapshots,
    list_halted_cycles,
    list_positions,
    list_trades,
)
from trader.ledger.repository import get_decision, list_all_fills, list_orders
from trader.report.readiness import BenchmarkPoint, ReadinessInputs
from trader.rules.loss_limits import trading_day
from trader.rules.schema import RuleSet

_BENCHMARK_TICKER = "SPY"
_SUBSTITUTE_DAYS = 5


@dataclass(frozen=True, slots=True)
class ReadinessInputsError:
    message: str


def _sum_by_ticker(trades: Iterable[Trade]) -> dict[str, Money]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for trade in trades:
        totals[trade.ticker] += trade.realized_pnl.amount
    return {ticker: Money(amount) for ticker, amount in totals.items()}


def _marks(conn: sqlite3.Connection, day: date) -> dict[str, Money]:
    return {m.ticker: m.unrealized_pnl for m in list_position_marks(conn, day.isoformat())}


def _benchmark_point(
    conn: sqlite3.Connection, target: date, *, forward: bool
) -> BenchmarkPoint | None:
    by_day = {
        date.fromisoformat(p.price_date): p.close.amount
        for p in list_benchmark_prices(conn, _BENCHMARK_TICKER)
    }
    if target in by_day:
        return BenchmarkPoint(target, by_day[target], False)
    offsets = range(1, _SUBSTITUTE_DAYS + 1)
    candidates = (
        [target + timedelta(days=n) for n in offsets]
        if forward
        else [target - timedelta(days=n) for n in offsets]
    )
    for day in candidates:
        if day in by_day:
            return BenchmarkPoint(day, by_day[day], True)
    return None


def _in_window(moment: datetime | None, start: date, end: date) -> bool:
    if moment is None:
        return False
    return start < trading_day(moment) <= end


def _held_on(trades: Iterable[Trade], positions: Iterable[Position], day: date) -> bool:
    """True when something was held at the close of `day` (LR-2)."""
    if any(trading_day(p.opened_at) <= day for p in positions):
        return True
    return any(trading_day(t.opened_at) <= day < trading_day(t.closed_at) for t in trades)


def _manual_close_count(conn: sqlite3.Connection, trades: Iterable[Trade]) -> int:
    count = 0
    for trade in trades:
        if trade.exit_reason != ExitReason.manual or not trade.exit_decision_ids:
            continue
        decision = get_decision(conn, trade.exit_decision_ids[-1])
        if decision is not None and decision.mode == "paper":
            count += 1
    return count


def load_readiness_inputs(
    conn: sqlite3.Connection, rule_set: RuleSet, since: date | None
) -> Result[ReadinessInputs, ReadinessInputsError]:
    """Build the inputs for `evaluate_readiness` (LR-2..LR-7)."""
    snapshots = [(date.fromisoformat(s.snapshot_date), s) for s in list_equity_snapshots(conn)]
    missing: list[str] = []
    first = snapshots[0][0] if snapshots else None
    if since is not None and first is not None and since < first:
        return Err(
            ReadinessInputsError("--since は paper 開始日 YYYY-MM-DD 以降を指定してください")
        )
    start = since if since is not None else first
    end = snapshots[-1][0] if snapshots else None
    if since is not None and end is not None and since > end:
        return Err(ReadinessInputsError("--since は評価日(最新 snapshot)以前を指定してください"))
    trades = list_trades(conn)
    kills = tuple(
        (c.id, c.error_summary or "")
        for c in list_halted_cycles(conn)
        if start is not None and trading_day(c.started_at) >= start
    )
    base = ReadinessInputs(
        start_date=start,
        end_date=end,
        since_requested=since,
        capital=Money(rule_set.capital_usd),
        realized_start_by_ticker={},
        realized_end_by_ticker={},
        unreal_start_by_ticker={},
        unreal_end_by_ticker={},
        benchmark_start=None,
        benchmark_end=None,
        window_fills=(),
        window_order_count=0,
        cost=rule_set.cost_assumptions,
        max_weekly_loss_pct=rule_set.loss_limits.max_weekly_loss_pct,
        equity_series=(),
        kill_switch_events=kills,
        manual_close_count=_manual_close_count(conn, trades),
        missing=("equity_snapshots",),
    )
    if start is None or end is None:
        return Ok(base)
    positions = list_positions(conn)
    unreal_end: dict[str, Money] | None = _marks(conn, end)
    if not unreal_end and positions:
        unreal_end = None
        missing.append("position_marks")
    elif unreal_end is not None:
        absent = sorted(p.ticker for p in positions if p.ticker not in unreal_end)
        if absent:
            missing.append(f"position_marks({', '.join(absent)})")
    unreal_start: dict[str, Money] | None = _marks(conn, start)
    if not unreal_start and start != first and _held_on(trades, positions, start):
        unreal_start = None
        missing.append("position_marks(S)")
    b_start = _benchmark_point(conn, start, forward=True)
    b_end = _benchmark_point(conn, end, forward=False)
    if b_start is None or b_end is None:
        missing.append("benchmark_prices")
    fills = tuple(f for f in list_all_fills(conn) if _in_window(f.filled_at, start, end))
    orders = sum(1 for o in list_orders(conn) if _in_window(o.submitted_at, start, end))
    return Ok(
        ReadinessInputs(
            start_date=start,
            end_date=end,
            since_requested=since,
            capital=base.capital,
            realized_start_by_ticker=_sum_by_ticker(
                t for t in trades if trading_day(t.closed_at) <= start
            ),
            realized_end_by_ticker=_sum_by_ticker(
                t for t in trades if trading_day(t.closed_at) <= end
            ),
            unreal_start_by_ticker=unreal_start,
            unreal_end_by_ticker=unreal_end,
            benchmark_start=b_start,
            benchmark_end=b_end,
            window_fills=fills,
            window_order_count=orders,
            cost=base.cost,
            max_weekly_loss_pct=base.max_weekly_loss_pct,
            equity_series=tuple((d, s.equity) for d, s in snapshots if d >= start),
            kill_switch_events=kills,
            manual_close_count=base.manual_close_count,
            missing=tuple(missing),
        )
    )
