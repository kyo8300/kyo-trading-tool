"""Derive `positions` / `trades` updates from a single `Fill` (R-21).

`apply_fill` is a pure function: given the position as it stood before the
fill (or `None` if this is the first buy for the ticker), the fill itself,
the decision that produced the order, and the side, it returns either an
updated (still open) `Position` or a closed `Trade`. It never touches the
database -- the caller (a future `engine/` module) is responsible for
reading the prior position, calling this function, and persisting whatever
`PositionUpdate` comes back via `ledger.portfolio_repository`.

`domain.models.Position` intentionally has no columns for the running
entry-decision id, the accumulated realized P&L, or the accumulated fees of
a position that has been partially sold (see the `positions` table in
spec.md "データモデル": only `ticker, qty, avg_cost, opened_at,
high_watermark, partial_tp_done`). Since this module may not invent new
persisted columns, that bookkeeping is threaded through explicit keyword
arguments instead:

- `entry_decision_id`: the id of the decision whose fill originally opened
  the position. `None` only for the very first buy (where it defaults to
  `decision.id`); every later call (additional buys, partial sells, the
  closing sell) must pass the value the caller has been carrying since.
- `exit_decision_ids`: the exit decision ids accumulated from earlier
  partial sells of this position (empty for the first sell).
- `realized_so_far` / `fees_so_far`: the realized P&L and fees accumulated
  from earlier partial sells of this position (zero for the first sell).

None of these are read for a buy fill or for a sell that does not fully
close the position; they only feed the `Trade` built when the position's
quantity reaches zero.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from trader.domain.models import Decision, ExitReason, Fill, Origin, Position, Side, Trade
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok, Result

_ZERO_MONEY = Money(Decimal(0))


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """The fill left `position` open (a buy, or a sell that only reduced it)."""

    position: Position


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """The fill closed the position out entirely; `trade` is the closed row."""

    trade: Trade


type PositionUpdate = OpenPosition | ClosedTrade


class TradeCloserError(ValueError):
    """A fill could not be applied to the given position (e.g. oversell)."""


def apply_fill(
    position: Position | None,
    fill: Fill,
    decision: Decision,
    side: Side,
    now: datetime,  # reserved for future validation (e.g. rejecting future-dated fills)
    exit_reason: ExitReason | None = None,
    *,
    entry_decision_id: str | None = None,
    exit_decision_ids: tuple[str, ...] = (),
    realized_so_far: Money = _ZERO_MONEY,
    fees_so_far: Money = _ZERO_MONEY,
) -> Result[PositionUpdate, TradeCloserError]:
    """Apply one fill to `position`, returning the resulting `PositionUpdate`.

    Buys: if `position` is `None`, a new `Position` is opened at
    `fill.price`. Otherwise the average cost is recalculated in `Decimal`
    (`(avg_cost*qty + price*fill_qty) / (qty+fill_qty)`, quantized via
    `Price`) and `opened_at` is left untouched (it stays the first fill's
    timestamp).

    Sells: `position` must exist and hold at least `fill.qty` shares, or
    this returns `Err`. Realized P&L for this fill is
    `(price - avg_cost) * qty - fee` (quantized via `Money`). If shares
    remain, an updated `Position` is returned (`partial_tp_done` becomes
    `True` when `exit_reason` is `partial_take_profit`, previously-`True`
    stays `True`). If the sell exhausts the position, a `Trade` is returned
    whose `exit_reason` is the caller-supplied `exit_reason` (a rule exit)
    or, if none was given, `ExitReason.llm` when `decision.origin` is
    `Origin.llm`; if neither applies this is an `Err` (a closing sell must
    be explained by a rule or by the LLM). `realized_pnl` / `fees` are the
    cumulative totals across every fill of this position, combining
    `realized_so_far` / `fees_so_far` with this fill. `holding_days` is the
    calendar-day difference between `position.opened_at` and
    `fill.filled_at`.
    """
    if side is Side.buy:
        return Ok(_apply_buy(position, fill, decision))
    return _apply_sell(
        position,
        fill,
        decision,
        exit_reason,
        entry_decision_id=entry_decision_id,
        exit_decision_ids=exit_decision_ids,
        realized_so_far=realized_so_far,
        fees_so_far=fees_so_far,
    )


def _apply_buy(position: Position | None, fill: Fill, decision: Decision) -> PositionUpdate:
    if position is None:
        return OpenPosition(
            Position(
                ticker=decision.ticker,
                qty=fill.qty,
                avg_cost=fill.price,
                opened_at=fill.filled_at,
                high_watermark=fill.price,
                partial_tp_done=False,
            )
        )

    total_shares = position.qty.shares + fill.qty.shares
    new_avg_cost = Price(
        (position.avg_cost.amount * position.qty.shares + fill.price.amount * fill.qty.shares)
        / Decimal(total_shares)
    )
    return OpenPosition(replace(position, qty=Quantity(total_shares), avg_cost=new_avg_cost))


def _apply_sell(
    position: Position | None,
    fill: Fill,
    decision: Decision,
    exit_reason: ExitReason | None,
    *,
    entry_decision_id: str | None,
    exit_decision_ids: tuple[str, ...],
    realized_so_far: Money,
    fees_so_far: Money,
) -> Result[PositionUpdate, TradeCloserError]:
    if position is None:
        return Err(TradeCloserError(f"cannot sell {decision.ticker}: no open position"))
    if fill.qty.shares > position.qty.shares:
        return Err(
            TradeCloserError(
                f"cannot sell {fill.qty.shares} shares of {decision.ticker}: "
                f"only {position.qty.shares} held"
            )
        )

    fill_pnl = Money((fill.price.amount - position.avg_cost.amount) * fill.qty.shares) - fill.fee
    cumulative_realized = realized_so_far + fill_pnl
    cumulative_fees = fees_so_far + fill.fee
    remaining_shares = position.qty.shares - fill.qty.shares

    resolved_reason = exit_reason
    if resolved_reason is None and decision.origin is Origin.llm:
        resolved_reason = ExitReason.llm

    if remaining_shares > 0:
        partial_tp_done = (
            position.partial_tp_done or resolved_reason == ExitReason.partial_take_profit
        )
        return Ok(
            OpenPosition(
                replace(position, qty=Quantity(remaining_shares), partial_tp_done=partial_tp_done)
            )
        )

    if resolved_reason is None:
        return Err(
            TradeCloserError("closing sell needs a rule exit_reason or an LLM-origin decision")
        )

    resolved_entry_decision_id = entry_decision_id if entry_decision_id is not None else decision.id
    trade = Trade(
        id=f"{resolved_entry_decision_id}:{decision.id}",
        ticker=position.ticker,
        opened_at=position.opened_at,
        closed_at=fill.filled_at,
        entry_decision_id=resolved_entry_decision_id,
        exit_decision_ids=(*exit_decision_ids, decision.id),
        exit_reason=resolved_reason,
        realized_pnl=cumulative_realized,
        fees=cumulative_fees,
        holding_days=(fill.filled_at.date() - position.opened_at.date()).days,
    )
    return Ok(ClosedTrade(trade))
