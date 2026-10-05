"""Manual close (LR-15..LR-22): sell a held position in full, through the same
approval -> order -> settle path as `run_cycle` (`engine/execution.py`).

Preconditions are verified before anything is written. This module never calls
the approval / order / settle functions directly (LR-17); it only builds a
decision and hands it to `execute_decisions`.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from trader.broker.broker import Broker
from trader.config.mode import TradingMode
from trader.domain.clock import Clock
from trader.domain.models import (
    Action,
    Decision,
    ExitReason,
    Order,
    OrderStatus,
    Origin,
    Position,
    RuleCheck,
    Trade,
)
from trader.domain.money import Money, Price
from trader.domain.result import Err, Ok, Result
from trader.engine.cycle import acquire_lock, release_lock
from trader.engine.execution import DecisionExecution, ExecutionDeps, execute_decisions
from trader.engine.kill_switch import is_halted
from trader.ledger.portfolio_repository import (
    finish_cycle,
    get_position,
    insert_cycle,
    list_trades,
)
from trader.ledger.repository import get_order, insert_decision, list_decisions
from trader.market.data_provider import MarketClock, MarketDataProvider
from trader.rules import loss_limits
from trader.rules.schema import RuleSet

_RULE_CHECK_REASON = "manual close (sell of held position; entry rules do not apply)"
_RATIONALE = "manual close by kyo"

CloseErrorKind = Literal["precondition", "approval_required", "order_failed", "ledger"]


@dataclass(frozen=True, slots=True)
class CloseError:
    """A human-readable close failure (N-6)."""

    kind: CloseErrorKind
    message: str


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """What one `close_position` call did."""

    cycle_id: str
    decision_id: str
    order_id: str
    order_status: OrderStatus
    filled_qty: int
    avg_fill_price: Price | None
    trade: Trade | None
    slot_freed: bool


@dataclass(frozen=True, slots=True)
class CloseDeps:
    """Everything `close_position` needs, injected for testability (N-9)."""

    mode: TradingMode
    rules: RuleSet
    rule_set_sha256: str
    conn: sqlite3.Connection
    broker: Broker
    market: MarketDataProvider
    clock: Clock
    poll_timeout_s: int = 60
    poll_interval_s: int = 2
    sleep: Callable[[int], None] = time.sleep
    lock_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _Checked:
    position: Position
    price: Price
    market_clock: MarketClock


def close_position(
    deps: CloseDeps, ticker: str, reason: str | None
) -> Result[CloseOutcome, CloseError]:
    """Sell the whole held position in `ticker` via the shared execution path."""
    if deps.lock_path is None:
        return _close_locked(deps, ticker, reason)
    lock_result = acquire_lock(deps.lock_path)
    if isinstance(lock_result, Err):
        return Err(CloseError("precondition", lock_result.error.message))
    try:
        return _close_locked(deps, ticker, reason)
    finally:
        release_lock(lock_result.value)


def _check_preconditions(deps: CloseDeps, ticker: str) -> Result[_Checked, CloseError]:
    position = get_position(deps.conn, ticker)
    if position is None:
        return _precondition(f"{ticker} を保有していません")
    if is_halted(deps.conn):
        return _precondition("エンジンが halted です(trader resume を先に実行してください)")
    clock_result = deps.market.market_clock()
    if isinstance(clock_result, Err):
        return _precondition("市場時間外です(市場の状態を取得できません)")
    if not clock_result.value.is_open:
        return _precondition(f"市場時間外です(次の open: {clock_result.value.next_open})")
    price_result = deps.market.latest_price(ticker)
    if isinstance(price_result, Err):
        return _precondition(f"{ticker} の価格を取得できません")
    return Ok(_Checked(position, price_result.value, clock_result.value))


def _precondition(message: str) -> Err[CloseError]:
    return Err(CloseError("precondition", message))


def _close_locked(
    deps: CloseDeps, ticker: str, reason: str | None
) -> Result[CloseOutcome, CloseError]:
    checked_result = _check_preconditions(deps, ticker)
    if isinstance(checked_result, Err):
        return checked_result
    checked = checked_result.value

    now = deps.clock.now()
    cycle_id = f"close_{uuid.uuid4().hex}"
    cycle_insert = insert_cycle(deps.conn, cycle_id, now, deps.mode.value)
    if isinstance(cycle_insert, Err):
        return Err(CloseError("ledger", cycle_insert.error.message))

    decision_result = _find_or_create_decision(deps, cycle_id, checked, reason, now)
    if isinstance(decision_result, Err):
        return _finish(deps, cycle_id, decision_result)
    decision = decision_result.value

    summary = execute_decisions(
        ExecutionDeps(
            mode=deps.mode,
            broker=deps.broker,
            clock=deps.clock,
            sleep=deps.sleep,
            poll_timeout_s=deps.poll_timeout_s,
            poll_interval_s=deps.poll_interval_s,
        ),
        deps.conn,
        (decision,),
        {decision.id: ExitReason.manual},
        checked.market_clock,
    )
    if not summary.results:
        return _finish(deps, cycle_id, Err(CloseError("order_failed", "decision was not executed")))
    return _finish(
        deps, cycle_id, _read_result(deps, ticker, decision, summary.results[0], cycle_id)
    )


def _finish(
    deps: CloseDeps, cycle_id: str, result: Result[CloseOutcome, CloseError]
) -> Result[CloseOutcome, CloseError]:
    if isinstance(result, Ok):
        outcome, summary = "ok", None
    elif result.error.kind == "approval_required":
        outcome, summary = "ok", "approval required"
    else:
        outcome, summary = "error", result.error.message
    finish_result = finish_cycle(deps.conn, cycle_id, deps.clock.now(), outcome, summary)
    deps.conn.commit()
    if isinstance(finish_result, Err):
        return Err(CloseError("ledger", finish_result.error.message))
    return result


def _find_or_create_decision(
    deps: CloseDeps, cycle_id: str, checked: _Checked, reason: str | None, now: datetime
) -> Result[Decision, CloseError]:
    existing = _reusable_decision(deps.conn, checked.position.ticker, now)
    if existing is not None:
        return Ok(existing)
    shares = checked.position.qty.shares
    decision = Decision(
        id=f"decision_{uuid.uuid4().hex}",
        cycle_id=cycle_id,
        decided_at=now,
        mode=deps.mode.value,
        ticker=checked.position.ticker,
        action=Action.sell,
        origin=Origin.manual,
        confidence=None,
        rationale=f"{_RATIONALE}: {reason}" if reason else _RATIONALE,
        evidence_mention_ids=(),
        llm_model=None,
        prompt_sha256=None,
        response_sha256=None,
        rule_set_sha256=deps.rule_set_sha256,
        rule_check=RuleCheck.passed,
        rule_check_reason=_RULE_CHECK_REASON,
        proposed_notional=Money(checked.price.amount * Decimal(shares)),
        reference_price=checked.price,
    )
    insert_result = insert_decision(deps.conn, decision)
    if isinstance(insert_result, Err):
        return Err(CloseError("ledger", insert_result.error.message))
    return Ok(decision)


def _reusable_decision(conn: sqlite3.Connection, ticker: str, now: datetime) -> Decision | None:
    """Newest same-ET-day, unordered manual sell decision (LR-20)."""
    today = loss_limits.trading_day(now)
    for decision in list_decisions(conn):  # newest first
        if (
            decision.origin is Origin.manual
            and decision.action is Action.sell
            and decision.ticker == ticker
            and loss_limits.trading_day(decision.decided_at) == today
            and get_order(conn, f"order_{decision.id}") is None
        ):
            return decision
    return None


def _read_result(
    deps: CloseDeps,
    ticker: str,
    decision: Decision,
    execution: DecisionExecution,
    cycle_id: str,
) -> Result[CloseOutcome, CloseError]:
    order = execution.order
    if order is None:
        if deps.mode is TradingMode.live:
            return Err(CloseError("approval_required", _approval_message(decision, ticker)))
        return Err(CloseError("order_failed", execution.error or "order was not placed"))
    if order.status is OrderStatus.failed:
        return Err(
            CloseError("order_failed", order.last_error or execution.error or "order failed")
        )
    return Ok(_build_outcome(deps, decision, order, execution, cycle_id))


def _approval_message(decision: Decision, ticker: str) -> str:
    return (
        f"live モードでは kyo の承認が必要です(decision: {decision.id})。"
        f"trader approve {decision.id} --expires-at <ISO> && trader close {ticker}"
    )


def _build_outcome(
    deps: CloseDeps, decision: Decision, order: Order, execution: DecisionExecution, cycle_id: str
) -> CloseOutcome:
    status = execution.poll.final_status if execution.poll is not None else order.status
    trade = next(
        (t for t in list_trades(deps.conn) if decision.id in t.exit_decision_ids),
        None,
    )
    return CloseOutcome(
        cycle_id=cycle_id,
        decision_id=decision.id,
        order_id=order.id,
        order_status=status,
        filled_qty=order.qty.shares if status is OrderStatus.filled else 0,
        avg_fill_price=None,
        trade=trade,
        slot_freed=trade is not None,
    )
