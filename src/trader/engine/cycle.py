"""Cycle orchestration (R-8, N-10): the single entry point tying holdings,
candidates, approval, execution, and fills together for one `run-cycle`.

Order: acquire the lock file -> open a `cycles` row -> if halted, evaluate
holdings only (decisions are kept, nothing is submitted) -> fetch the market
clock (a failure here means no new buys and no equity snapshot this cycle)
-> evaluate holdings -> compute equity and upsert today's snapshot -> check
daily/weekly/drawdown loss limits (a breach fires the kill switch and ends
the cycle) -> evaluate candidates -> if the market is closed, stop (decisions
are kept, nothing is submitted) -> for every passed buy/sell decision,
resolve approval, execute, and poll for fills -> close the `cycles` row.
"""

from __future__ import annotations

import fcntl
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Literal

from trader.analysis.analyst import LlmClient
from trader.broker.broker import Broker
from trader.config.mode import TradingMode
from trader.domain.clock import Clock
from trader.domain.models import Action, Decision, ExitReason, RuleCheck, Trade
from trader.domain.money import Money
from trader.domain.result import Err, Ok, Result
from trader.engine.approval import resolve_approval
from trader.engine.candidates import evaluate_candidates
from trader.engine.fills import poll_and_settle
from trader.engine.holdings import HoldingsOutcome, evaluate_holdings
from trader.engine.kill_switch import is_halted
from trader.engine.kill_switch import trigger as trigger_kill_switch
from trader.engine.order_executor import execute
from trader.ledger.portfolio_repository import (
    finish_cycle,
    insert_cycle,
    list_equity_snapshots,
    list_positions,
    list_trades,
    set_engine_state,
    upsert_equity_snapshot,
)
from trader.ledger.source_repository import list_mentions
from trader.market.data_provider import MarketClock, MarketDataProvider
from trader.rules import loss_limits
from trader.rules.schema import DerivedLimits, RuleSet

_SERENITY_SOURCE_ID = "serenity"
_LAST_CYCLE_ID_KEY = "last_cycle_id"

CycleStatus = Literal["ok", "halted", "market_unavailable", "error"]


@dataclass(frozen=True, slots=True)
class CycleError:
    """A human-readable cycle error (N-6)."""

    message: str


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    """What happened during one `run_cycle` call."""

    cycle_id: str
    outcome: CycleStatus
    decisions: int
    orders: int
    fills: int
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class CycleDeps:
    """Everything one `run_cycle` call needs, injected for testability (N-9)."""

    mode: TradingMode
    rules: RuleSet
    limits: DerivedLimits
    rule_set_sha256: str
    conn: sqlite3.Connection
    broker: Broker
    market: MarketDataProvider
    llm: LlmClient | None
    clock: Clock
    capital: Money
    poll_timeout_s: int = 60
    poll_interval_s: int = 2
    sleep: Callable[[int], None] = time.sleep
    lock_path: Path | None = None


def _acquire_lock(lock_path: Path) -> Result[IO[str], CycleError]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return Err(CycleError("別のサイクルが実行中です"))
    return Ok(handle)


def _release_lock(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _prior_peak_equity(conn: sqlite3.Connection, capital: Money, today: str) -> Money:
    prior = [s for s in list_equity_snapshots(conn) if s.snapshot_date < today]
    if not prior:
        return capital
    return prior[-1].peak_equity


def _realized_totals(trades: tuple[Trade, ...], now: datetime) -> tuple[Money, Money]:
    today = loss_limits.trading_day(now)
    week_start = loss_limits.trading_week_start(now)
    realized_today = Money(Decimal(0))
    realized_week = Money(Decimal(0))
    for trade in trades:
        trade_day = loss_limits.trading_day(trade.closed_at)
        if trade_day == today:
            realized_today = realized_today + trade.realized_pnl
        if trade_day >= week_start:
            realized_week = realized_week + trade.realized_pnl
    return realized_today, realized_week


def _finish(
    conn: sqlite3.Connection,
    cycle_id: str,
    outcome: CycleStatus,
    error_summary: str | None,
    decisions: int,
    orders: int,
    fills: int,
    clock: Clock,
) -> Result[CycleOutcome, CycleError]:
    finish_result = finish_cycle(conn, cycle_id, clock.now(), outcome, error_summary)
    if isinstance(finish_result, Err):
        # Persist whatever writes (decisions, orders, fills, positions, ...)
        # already succeeded this cycle even though the `cycles` row itself
        # could not be closed out -- callers other than this connection
        # (e.g. `trader report`/`trader status` in a later process) must see
        # them, not silently lose them because this final write failed.
        conn.commit()
        return Err(CycleError(finish_result.error.message))
    if outcome != "error":
        set_engine_state(conn, _LAST_CYCLE_ID_KEY, cycle_id)
    # Every write this cycle (decisions/approvals/orders/fills/positions/
    # trades/equity_snapshots/engine_state/cycles) shares this connection's
    # single implicit transaction; only the `execute()` order-recording step
    # calls `ledger.db.transaction()` (R-16) explicitly. Commit here so a
    # fresh connection opened by another `trader` invocation (or the next
    # `run-cycle`) actually observes this cycle's results (spec "SQLite の
    # 同時実行": WAL readers only see committed data).
    conn.commit()
    return Ok(
        CycleOutcome(
            cycle_id=cycle_id,
            outcome=outcome,
            decisions=decisions,
            orders=orders,
            fills=fills,
            error_summary=error_summary,
        )
    )


def run_cycle(deps: CycleDeps) -> Result[CycleOutcome, CycleError]:
    """Run exactly one decision/order cycle (R-8)."""
    if deps.lock_path is None:
        return _run_locked(deps)

    lock_result = _acquire_lock(deps.lock_path)
    if isinstance(lock_result, Err):
        return lock_result
    handle = lock_result.value
    try:
        return _run_locked(deps)
    finally:
        _release_lock(handle)


def _evaluate_risk(
    deps: CycleDeps,
    conn: sqlite3.Connection,
    cycle_id: str,
    holdings_outcome: HoldingsOutcome,
    decisions_count: int,
) -> Result[CycleOutcome, CycleError] | None:
    """Fetch account equity, upsert today's `equity_snapshots`, and check
    daily/weekly/drawdown loss limits (a breach fires the kill switch).

    Returns a finished `Result` if the cycle must stop here (account/
    snapshot error, or a loss-limit breach), or `None` to keep going.
    """
    account_result = deps.broker.account()
    if isinstance(account_result, Err):
        return _finish(
            conn,
            cycle_id,
            "market_unavailable",
            account_result.error.message,
            decisions_count,
            0,
            0,
            deps.clock,
        )
    cash = account_result.value.cash
    equity = cash + holdings_outcome.positions_value

    now = deps.clock.now()
    today = loss_limits.trading_day(now).isoformat()
    prior_peak = _prior_peak_equity(conn, deps.capital, today)
    peak_equity = Money(max(prior_peak.amount, equity.amount))
    drawdown_pct = (
        (peak_equity.amount - equity.amount) / peak_equity.amount * Decimal(100)
        if peak_equity.amount > 0
        else Decimal(0)
    )
    snapshot_result = upsert_equity_snapshot(
        conn,
        today,
        deps.mode.value,
        cash,
        holdings_outcome.positions_value,
        peak_equity,
        drawdown_pct,
        now,
    )
    if isinstance(snapshot_result, Err):
        return _finish(
            conn,
            cycle_id,
            "error",
            snapshot_result.error.message,
            decisions_count,
            0,
            0,
            deps.clock,
        )

    realized_today, realized_week = _realized_totals(list_trades(conn), now)
    breach = loss_limits.evaluate(
        deps.limits,
        realized_today,
        holdings_outcome.unrealized_pnl,
        realized_week,
        equity,
        peak_equity,
    )
    if breach is not None:
        kill_result = trigger_kill_switch(breach, deps.broker, conn, deps.clock)
        if isinstance(kill_result, Err):
            return _finish(
                conn,
                cycle_id,
                "error",
                kill_result.error.message,
                decisions_count,
                0,
                0,
                deps.clock,
            )
        return _finish(conn, cycle_id, "halted", breach.reason, decisions_count, 0, 0, deps.clock)

    return None


def _run_locked(deps: CycleDeps) -> Result[CycleOutcome, CycleError]:
    conn = deps.conn
    started_at = deps.clock.now()
    cycle_id = f"cycle_{uuid.uuid4().hex}"

    insert_result = insert_cycle(conn, cycle_id, started_at, deps.mode.value)
    if isinstance(insert_result, Err):
        return Err(CycleError(insert_result.error.message))

    if is_halted(conn):
        holdings_result = evaluate_holdings(
            conn,
            list_positions(conn),
            deps.market,
            deps.rules,
            deps.clock,
            cycle_id,
            deps.rule_set_sha256,
            deps.mode.value,
        )
        decisions_count = (
            len(holdings_result.value.decisions) if isinstance(holdings_result, Ok) else 0
        )
        return _finish(
            conn, cycle_id, "halted", "engine is halted", decisions_count, 0, 0, deps.clock
        )

    market_clock_result = deps.market.market_clock()
    if isinstance(market_clock_result, Err):
        return _finish(
            conn,
            cycle_id,
            "market_unavailable",
            market_clock_result.error.message,
            0,
            0,
            0,
            deps.clock,
        )
    market_clock = market_clock_result.value

    positions = list_positions(conn)
    holdings_result = evaluate_holdings(
        conn,
        positions,
        deps.market,
        deps.rules,
        deps.clock,
        cycle_id,
        deps.rule_set_sha256,
        deps.mode.value,
    )
    if isinstance(holdings_result, Err):
        return _finish(conn, cycle_id, "error", holdings_result.error.message, 0, 0, 0, deps.clock)
    holdings_outcome = holdings_result.value
    decisions_count = len(holdings_outcome.decisions)

    if holdings_outcome.market_errors:
        # spec エラー処理: a held position whose price could not be fetched
        # means the cycle cannot judge sells or value the portfolio, so no
        # equity_snapshots write, no loss-limit/kill-switch evaluation
        # against incomplete equity, and no new buys -- the whole cycle
        # stops here as `market_unavailable` (R-19 review finding). But
        # R-13 says sells are rule-driven and never wait for the LLM: a
        # rule-exit sell decision that *was* successfully priced on a
        # different, priced holding must still be submitted and filled (as
        # long as the market is open) even though a sibling holding's price
        # is missing this cycle (R-13 review finding, cycle.py:252).
        orders_count = 0
        fills_count = 0
        if market_clock.is_open:
            orders_count, fills_count = _execute_passed_decisions(
                deps, conn, holdings_outcome.decisions, holdings_outcome.exit_reasons, market_clock
            )
        return _finish(
            conn,
            cycle_id,
            "market_unavailable",
            "no price for held ticker(s): " + ", ".join(holdings_outcome.market_errors),
            decisions_count,
            orders_count,
            fills_count,
            deps.clock,
        )

    risk_outcome = _evaluate_risk(deps, conn, cycle_id, holdings_outcome, decisions_count)
    if risk_outcome is not None:
        return risk_outcome

    held_tickers = {p.ticker for p in positions}
    mentions = list_mentions(conn, _SERENITY_SOURCE_ID)
    candidates_result = evaluate_candidates(
        conn,
        mentions,
        held_tickers,
        deps.market,
        deps.llm,
        deps.rules,
        deps.limits,
        deps.clock,
        cycle_id,
        deps.rule_set_sha256,
        deps.mode.value,
        None,
        market_clock.is_open,
    )
    if isinstance(candidates_result, Err):
        return _finish(
            conn,
            cycle_id,
            "error",
            candidates_result.error.message,
            decisions_count,
            0,
            0,
            deps.clock,
        )
    candidates_outcome = candidates_result.value
    decisions_count += len(candidates_outcome.decisions)

    if not market_clock.is_open:
        return _finish(conn, cycle_id, "ok", None, decisions_count, 0, 0, deps.clock)

    all_decisions = (*holdings_outcome.decisions, *candidates_outcome.decisions)
    orders_count, fills_count = _execute_passed_decisions(
        deps, conn, all_decisions, holdings_outcome.exit_reasons, market_clock
    )

    return _finish(
        conn, cycle_id, "ok", None, decisions_count, orders_count, fills_count, deps.clock
    )


def _execute_passed_decisions(
    deps: CycleDeps,
    conn: sqlite3.Connection,
    decisions: tuple[Decision, ...],
    exit_reasons: dict[str, ExitReason],
    market_clock: MarketClock,
) -> tuple[int, int]:
    orders_count = 0
    fills_count = 0
    for decision in decisions:
        if decision.rule_check is not RuleCheck.passed or decision.action not in (
            Action.buy,
            Action.sell,
        ):
            continue

        approval_result = resolve_approval(decision, deps.mode, conn, deps.clock, market_clock)
        if isinstance(approval_result, Err):
            continue

        exec_result = execute(approval_result.value, conn, deps.broker, deps.clock, deps.mode)
        if isinstance(exec_result, Err):
            continue
        order = exec_result.value
        orders_count += 1

        exit_reason = exit_reasons.get(decision.id)
        poll_result = poll_and_settle(
            conn,
            deps.broker,
            deps.clock,
            deps.sleep,
            order.client_order_id,
            order.id,
            decision,
            order.side,
            exit_reason,
            deps.poll_timeout_s,
            deps.poll_interval_s,
        )
        if isinstance(poll_result, Ok):
            fills_count += poll_result.value.fills_recorded

    return orders_count, fills_count
