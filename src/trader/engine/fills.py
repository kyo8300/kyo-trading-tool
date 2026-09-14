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
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from trader.broker.broker import Broker
from trader.domain.clock import Clock
from trader.domain.models import Decision, ExitReason, Fill, OrderStatus, Side
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok, Result
from trader.ledger.portfolio_repository import (
    delete_position,
    get_position,
    insert_trade,
    upsert_position,
)
from trader.ledger.repository import (
    FillContext,
    insert_fill,
    list_fill_context_for_ticker_since,
    list_fills,
    update_order_status,
)
from trader.ledger.trade_closer import OpenPosition, apply_fill

_TERMINAL_STATUSES = frozenset({OrderStatus.filled, OrderStatus.canceled, OrderStatus.rejected})
_ZERO_MONEY = Money(Decimal(0))


@dataclass(frozen=True, slots=True)
class FillsError:
    """A human-readable fill-settlement error (N-6)."""

    message: str


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """How many fills were recorded and the order's final observed status."""

    fills_recorded: int
    final_status: OrderStatus


@dataclass(frozen=True, slots=True)
class _ReplayState:
    """The running entry decision / accumulated exit decisions / realized
    P&L and fees for an open position, as rebuilt from its prior fills."""

    entry_decision_id: str | None
    exit_decision_ids: tuple[str, ...]
    realized_so_far: Money
    fees_so_far: Money
    qty: int
    avg_cost: Price | None


_EMPTY_REPLAY_STATE = _ReplayState(
    entry_decision_id=None,
    exit_decision_ids=(),
    realized_so_far=_ZERO_MONEY,
    fees_so_far=_ZERO_MONEY,
    qty=0,
    avg_cost=None,
)


def _replay_buy(state: _ReplayState, ctx: FillContext) -> _ReplayState:
    if state.avg_cost is None:
        return replace(
            state,
            entry_decision_id=ctx.decision_id,
            qty=ctx.fill.qty.shares,
            avg_cost=ctx.fill.price,
        )
    total_shares = state.qty + ctx.fill.qty.shares
    new_avg_cost = Price(
        (state.avg_cost.amount * state.qty + ctx.fill.price.amount * ctx.fill.qty.shares)
        / Decimal(total_shares)
    )
    return replace(state, qty=total_shares, avg_cost=new_avg_cost)


def _replay_sell(state: _ReplayState, ctx: FillContext) -> _ReplayState:
    avg_cost = state.avg_cost if state.avg_cost is not None else Price(Decimal(0))
    fill_pnl = Money((ctx.fill.price.amount - avg_cost.amount) * ctx.fill.qty.shares) - ctx.fill.fee
    return replace(
        state,
        exit_decision_ids=(*state.exit_decision_ids, ctx.decision_id),
        realized_so_far=state.realized_so_far + fill_pnl,
        fees_so_far=state.fees_so_far + ctx.fill.fee,
        qty=state.qty - ctx.fill.qty.shares,
    )


def _replay_prior_fills(
    conn: sqlite3.Connection, ticker: str, opened_at: datetime, exclude_fill_id: str
) -> _ReplayState:
    """Rebuild the entry decision, accumulated exit decisions, and
    accumulated realized P&L/fees for the currently open position on
    `ticker` by replaying, in order, every fill recorded since it opened
    (excluding `exclude_fill_id`, the fill about to be settled by the
    caller). `positions` has no columns for this bookkeeping (R-21, see
    `ledger.trade_closer` module docstring), so it must be rebuilt from the
    fill history on every settlement instead of carried across `run_cycle`
    calls in memory."""
    state = _EMPTY_REPLAY_STATE
    for ctx in list_fill_context_for_ticker_since(conn, ticker, opened_at):
        if ctx.fill.id == exclude_fill_id:
            continue
        state = _replay_buy(state, ctx) if ctx.side is Side.buy else _replay_sell(state, ctx)
    return state


def _settle_fill(
    conn: sqlite3.Connection,
    decision: Decision,
    fill: Fill,
    side: Side,
    exit_reason: ExitReason | None,
) -> Result[None, FillsError]:
    position = get_position(conn, decision.ticker)
    if side is Side.sell and position is not None:
        replay = _replay_prior_fills(conn, decision.ticker, position.opened_at, fill.id)
        entry_decision_id = replay.entry_decision_id
        exit_decision_ids = replay.exit_decision_ids
        realized_so_far = replay.realized_so_far
        fees_so_far = replay.fees_so_far
    else:
        entry_decision_id = None
        exit_decision_ids = ()
        realized_so_far = _ZERO_MONEY
        fees_so_far = _ZERO_MONEY
    result = apply_fill(
        position,
        fill,
        decision,
        side,
        fill.filled_at,
        exit_reason,
        entry_decision_id=entry_decision_id,
        exit_decision_ids=exit_decision_ids,
        realized_so_far=realized_so_far,
        fees_so_far=fees_so_far,
    )
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
    recording every fill and applying it to the ledger as it arrives.

    The already-recorded cumulative quantity/notional are seeded from
    `order_id`'s existing `fills` rows (not assumed to be zero) so a
    second `poll_and_settle` call for the same order -- rather than the
    normal single call that loops internally until a terminal status --
    still computes each new fill's incremental price correctly (R-18
    review finding).
    """
    existing_fills = list_fills(conn, order_id)
    recorded_qty = sum(f.qty.shares for f in existing_fills)
    recorded_notional = Money(
        sum((f.price.amount * f.qty.shares for f in existing_fills), start=Decimal(0))
    )
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
            # `broker_order.filled_avg_price` is the broker's running
            # average price across the *whole order so far*, not the price
            # of just the newly-filled `new_qty` shares (R-18 review
            # finding). Back out the incremental price from the delta
            # between this poll's cumulative notional and what was already
            # recorded, so a later fill at a different price does not get
            # double-blended with the earlier one.
            cumulative_notional = Money(
                broker_order.filled_avg_price.amount * broker_order.filled_qty.shares
            )
            delta_notional = cumulative_notional - recorded_notional
            delta_price = Price(delta_notional.amount / Decimal(new_qty))
            fill = Fill(
                id=f"fill_{uuid.uuid4().hex}",
                order_id=order_id,
                filled_at=clock.now(),
                qty=Quantity(new_qty),
                price=delta_price,
                fee=Money(Decimal(0)),
            )
            insert_result = insert_fill(conn, fill)
            if isinstance(insert_result, Err):
                return Err(FillsError(insert_result.error.message))
            settle_result = _settle_fill(conn, decision, fill, side, exit_reason)
            if isinstance(settle_result, Err):
                return settle_result
            recorded_qty = broker_order.filled_qty.shares
            recorded_notional = cumulative_notional
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
