"""Shared execution path: approval -> order -> poll/settle (LR-17).

`run_cycle` (and the manual close flow) turn passed decisions into orders
only through `execute_decisions`, so the approval gate and the fill
settlement exist in exactly one place.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from trader.broker.broker import Broker
from trader.config.mode import TradingMode
from trader.domain.clock import Clock
from trader.domain.models import Action, Decision, ExitReason, Order, RuleCheck
from trader.domain.result import Err, Ok
from trader.engine.approval import resolve_approval
from trader.engine.fills import PollOutcome, poll_and_settle
from trader.engine.order_executor import execute
from trader.market.data_provider import MarketClock


@dataclass(frozen=True, slots=True)
class ExecutionDeps:
    """What the shared execution path needs, injected for testability."""

    mode: TradingMode
    broker: Broker
    clock: Clock
    sleep: Callable[[int], None] = time.sleep
    poll_timeout_s: int = 60
    poll_interval_s: int = 2


@dataclass(frozen=True, slots=True)
class DecisionExecution:
    """The result of executing one passed decision."""

    decision_id: str
    order: Order | None
    poll: PollOutcome | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    """Counts plus per-decision results."""

    orders: int
    fills: int
    results: tuple[DecisionExecution, ...]


def execute_decisions(
    deps: ExecutionDeps,
    conn: sqlite3.Connection,
    decisions: tuple[Decision, ...],
    exit_reasons: dict[str, ExitReason],
    market_clock: MarketClock,
) -> ExecutionSummary:
    """Approve, submit, and settle every passed buy/sell decision."""
    orders_count = 0
    fills_count = 0
    results: list[DecisionExecution] = []
    for decision in decisions:
        if decision.rule_check is not RuleCheck.passed or decision.action not in (
            Action.buy,
            Action.sell,
        ):
            continue

        approval_result = resolve_approval(decision, deps.mode, conn, deps.clock, market_clock)
        if isinstance(approval_result, Err):
            results.append(DecisionExecution(decision.id, None, None, str(approval_result.error)))
            continue

        exec_result = execute(approval_result.value, conn, deps.broker, deps.clock, deps.mode)
        if isinstance(exec_result, Err):
            results.append(DecisionExecution(decision.id, None, None, str(exec_result.error)))
            continue
        order = exec_result.value
        orders_count += 1

        poll_result = poll_and_settle(
            conn,
            deps.broker,
            deps.clock,
            deps.sleep,
            order.client_order_id,
            order.id,
            decision,
            order.side,
            exit_reasons.get(decision.id),
            deps.poll_timeout_s,
            deps.poll_interval_s,
        )
        if isinstance(poll_result, Ok):
            fills_count += poll_result.value.fills_recorded
            results.append(DecisionExecution(decision.id, order, poll_result.value, None))
        else:
            results.append(DecisionExecution(decision.id, order, None, str(poll_result.error)))

    return ExecutionSummary(orders_count, fills_count, tuple(results))
