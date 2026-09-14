"""Evaluate held positions each cycle (spec データフロー step 3, R-13).

For every open position: mark it to the latest price, raise its high
watermark (never lower it), and ask `rules.exit_checks` whether a sell rule
fires. The LLM is never consulted here -- sells are decided by the rule
engine alone. Any ticker whose price cannot be fetched is skipped (its
value/unrealized P&L cannot be computed this cycle; it is re-evaluated next
cycle) and recorded in `market_errors` for the caller to see.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal

from trader.domain.clock import Clock
from trader.domain.models import Action, Decision, ExitReason, Origin, Position, RuleCheck
from trader.domain.money import Money, Quantity
from trader.domain.result import Err, Ok, Result
from trader.ledger.portfolio_repository import upsert_position
from trader.ledger.repository import insert_decision
from trader.market.data_provider import MarketDataProvider
from trader.rules.exit_checks import check_exit
from trader.rules.schema import RuleSet


@dataclass(frozen=True, slots=True)
class HoldingsError:
    """A human-readable holdings-evaluation error (N-6)."""

    message: str


@dataclass(frozen=True, slots=True)
class HoldingsOutcome:
    """The result of marking every held position to market for one cycle."""

    positions_value: Money
    unrealized_pnl: Money
    decisions: tuple[Decision, ...]
    exit_reasons: dict[str, ExitReason]
    market_errors: tuple[str, ...]


def _sell_qty(position: Position, fraction: Decimal) -> Quantity:
    """Share count to sell for `fraction` of `position`.

    `rules.exit_checks` documents that a fraction which floors to zero
    (e.g. a 1-share position with a 50% partial take-profit) must still be
    treated as "sell something now" -- `max(1, floor(qty * fraction))`.
    """
    floored = (Decimal(position.qty.shares) * fraction).to_integral_value(rounding=ROUND_FLOOR)
    return Quantity(max(1, int(floored)))


def evaluate_holdings(
    conn: sqlite3.Connection,
    positions: tuple[Position, ...],
    market: MarketDataProvider,
    rules: RuleSet,
    clock: Clock,
    cycle_id: str,
    rule_set_sha256: str,
    mode: str,
) -> Result[HoldingsOutcome, HoldingsError]:
    """Mark every held position to market and record `rule_exit` sell
    decisions for any that trip an exit rule (spec データフロー 3, R-13)."""
    now = clock.now()
    positions_value = Money(Decimal(0))
    unrealized_pnl = Money(Decimal(0))
    decisions: list[Decision] = []
    exit_reasons: dict[str, ExitReason] = {}
    market_errors: list[str] = []

    for position in positions:
        price_result = market.latest_price(position.ticker)
        if isinstance(price_result, Err):
            market_errors.append(position.ticker)
            continue
        price = price_result.value

        positions_value = positions_value + Money(price.amount * position.qty.shares)
        unrealized_pnl = unrealized_pnl + Money(
            (price.amount - position.avg_cost.amount) * position.qty.shares
        )

        new_high_watermark = (
            price if price.amount > position.high_watermark.amount else position.high_watermark
        )
        updated_position = replace(position, high_watermark=new_high_watermark)
        upsert_result = upsert_position(conn, updated_position)
        if isinstance(upsert_result, Err):
            return Err(HoldingsError(upsert_result.error.message))

        signal = check_exit(updated_position, price, rules, now)
        if signal is None:
            continue

        qty = _sell_qty(updated_position, signal.fraction)
        notional = Money(price.amount * qty.shares)
        decision = Decision(
            id=f"dec_{uuid.uuid4().hex}",
            cycle_id=cycle_id,
            decided_at=now,
            mode=mode,
            ticker=position.ticker,
            action=Action.sell,
            origin=Origin.rule_exit,
            confidence=None,
            rationale=(
                f"{signal.reason.value} triggered at {signal.trigger_price.amount} "
                f"(sell {qty.shares} of {updated_position.qty.shares} shares)"
            ),
            evidence_mention_ids=(),
            llm_model=None,
            prompt_sha256=None,
            response_sha256=None,
            rule_set_sha256=rule_set_sha256,
            rule_check=RuleCheck.passed,
            rule_check_reason=f"rule exit: {signal.reason.value} at {signal.trigger_price.amount}",
            proposed_notional=notional,
            reference_price=price,
        )
        insert_result = insert_decision(conn, decision)
        if isinstance(insert_result, Err):
            return Err(HoldingsError(insert_result.error.message))
        decisions.append(decision)
        exit_reasons[decision.id] = signal.reason

    return Ok(
        HoldingsOutcome(
            positions_value=positions_value,
            unrealized_pnl=unrealized_pnl,
            decisions=tuple(decisions),
            exit_reasons=exit_reasons,
            market_errors=tuple(market_errors),
        )
    )
