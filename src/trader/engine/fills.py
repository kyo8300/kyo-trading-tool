"""Fill polling and position/trade settlement for one approved order (R-18).

Split out of `cycle.py` to stay within the ~400-line file guideline (N-8).
`poll_and_settle` polls `broker.get_order` at `poll_interval_s` intervals for
up to `poll_timeout_s`, records every new (cumulative-delta) fill, and
applies it to the position via `ledger.trade_closer.apply_fill`. `clock` and
`sleep` are both injected so tests never actually wait (N-9).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from trader.broker.broker import Broker
from trader.domain.clock import Clock
from trader.domain.models import Decision, ExitReason, Fill, OrderStatus, Side
from trader.domain.money import Money, Quantity
from trader.domain.result import Err, Ok, Result
from trader.ledger.portfolio_repository import (
    delete_position,
    get_position,
    insert_trade,
    upsert_position,
)
from trader.ledger.repository import insert_fill, update_order_status
from trader.ledger.trade_closer import OpenPosition, apply_fill

_TERMINAL_STATUSES = frozenset({OrderStatus.filled, OrderStatus.canceled, OrderStatus.rejected})


@dataclass(frozen=True, slots=True)
class FillsError:
    """A human-readable fill-settlement error (N-6)."""

    message: str


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """How many fills were recorded and the order's final observed status."""

    fills_recorded: int
    final_status: OrderStatus


def _settle_fill(
    conn: sqlite3.Connection,
    decision: Decision,
    fill: Fill,
    side: Side,
    exit_reason: ExitReason | None,
) -> Result[None, FillsError]:
    position = get_position(conn, decision.ticker)
    result = apply_fill(position, fill, decision, side, fill.filled_at, exit_reason)
    if isinstance(result, Err):
        return Err(FillsError(str(result.error)))
    update = result.value
    if isinstance(update, OpenPosition):
        upsert_result = upsert_position(conn, update.position)
        if isinstance(upsert_result, Err):
            return Err(FillsError(upsert_result.error.message))
        return Ok(None)
    delete_position(conn, decision.ticker)
    insert_result = insert_trade(conn, update.trade)
    if isinstance(insert_result, Err):
        return Err(FillsError(insert_result.error.message))
    return Ok(None)


def poll_and_settle(
    conn: sqlite3.Connection,
    broker: Broker,
    clock: Clock,
    sleep: Callable[[int], None],
    client_order_id: str,
    order_id: str,
    decision: Decision,
    side: Side,
    exit_reason: ExitReason | None,
    poll_timeout_s: int,
    poll_interval_s: int,
) -> Result[PollOutcome, FillsError]:
    """Poll `client_order_id` until it reaches a terminal status or times out,
    recording every fill and applying it to the ledger as it arrives."""
    recorded_qty = 0
    fills_recorded = 0
    last_status = OrderStatus.submitted
    elapsed = 0

    while True:
        get_result = broker.get_order(client_order_id)
        if isinstance(get_result, Err):
            return Err(FillsError(get_result.error.message))
        broker_order = get_result.value

        new_qty = broker_order.filled_qty.shares - recorded_qty
        if new_qty > 0 and broker_order.filled_avg_price is not None:
            fill = Fill(
                id=f"fill_{uuid.uuid4().hex}",
                order_id=order_id,
                filled_at=clock.now(),
                qty=Quantity(new_qty),
                price=broker_order.filled_avg_price,
                fee=Money(Decimal(0)),
            )
            insert_result = insert_fill(conn, fill)
            if isinstance(insert_result, Err):
                return Err(FillsError(insert_result.error.message))
            settle_result = _settle_fill(conn, decision, fill, side, exit_reason)
            if isinstance(settle_result, Err):
                return settle_result
            recorded_qty = broker_order.filled_qty.shares
            fills_recorded += 1

        if broker_order.status != last_status:
            update_result = update_order_status(
                conn, order_id, broker_order.status, broker_order_id=broker_order.broker_order_id
            )
            if isinstance(update_result, Err):
                return Err(FillsError(update_result.error.message))
            last_status = broker_order.status

        if broker_order.status in _TERMINAL_STATUSES:
            return Ok(PollOutcome(fills_recorded=fills_recorded, final_status=broker_order.status))

        if elapsed >= poll_timeout_s:
            return Ok(PollOutcome(fills_recorded=fills_recorded, final_status=last_status))

        sleep(poll_interval_s)
        elapsed += poll_interval_s
