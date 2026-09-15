"""Pre-buy rule checks (R-12): the only path from a candidate ticker to an
`EntryPlan`. LLM proposals never bypass this module (R-6).

Checked in order: excluded ticker -> already held -> concurrent position
count -> market hours -> loss limits already breached -> liquidity (price,
volume) -> per-ticker notional limit -> share count. `RuleRejection.reason`
always carries the derived absolute amount and the underlying ratio so a
human can recompute it (spec "rule_check_reason").
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok, Result
from trader.rules.loss_limits import LossState
from trader.rules.schema import DerivedLimits, RuleSet


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """A rule-approved buy: integer shares within the per-ticker limit."""

    ticker: str
    qty: Quantity
    notional: Money
    limit: Money
    reference_price: Price


@dataclass(frozen=True, slots=True)
class RuleRejection:
    """Why a candidate did not become an `EntryPlan`."""

    ticker: str
    code: str
    reason: str


def check_entry(
    ticker: str,
    rules: RuleSet,
    limits: DerivedLimits,
    holdings: Collection[str],
    market_open: bool,
    latest_price: Price,
    avg_volume: int,
    loss_state: LossState | None,
) -> Result[EntryPlan, RuleRejection]:
    """Validate a buy candidate against every pre-buy rule (R-12).

    `holdings` is the set of currently held tickers. `loss_state` is the
    already-reached loss/drawdown breach for this cycle (if any), computed
    once by `loss_limits.evaluate` and passed in rather than recomputed
    here.
    """
    if ticker in rules.entry.excluded_tickers:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="excluded_ticker",
                reason=f"{ticker} is in entry.excluded_tickers",
            )
        )

    if ticker in holdings:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="already_held",
                reason=f"{ticker} is already held",
            )
        )

    if len(holdings) >= rules.position.max_concurrent_positions:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="max_concurrent_positions",
                reason=(
                    f"holdings {len(holdings)} >= max_concurrent_positions "
                    f"{rules.position.max_concurrent_positions}"
                ),
            )
        )

    if not market_open:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="market_closed",
                reason="market is not open",
            )
        )

    if loss_state is not None:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="loss_limit_reached",
                reason=loss_state.reason,
            )
        )

    if latest_price.amount < rules.entry.min_price_usd:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="min_price",
                reason=(f"price {latest_price.amount} < min_price_usd {rules.entry.min_price_usd}"),
            )
        )

    if avg_volume < rules.entry.min_avg_daily_volume:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="min_avg_volume",
                reason=(
                    f"avg_volume {avg_volume} < min_avg_daily_volume "
                    f"{rules.entry.min_avg_daily_volume}"
                ),
            )
        )

    if latest_price.amount > limits.max_notional_per_ticker.amount:
        return Err(
            RuleRejection(
                ticker=ticker,
                code="price_over_limit",
                reason=(
                    f"price {latest_price.amount} > per-ticker limit "
                    f"{limits.max_notional_per_ticker.amount} "
                    f"({rules.position.max_notional_per_ticker_pct}% of {rules.capital_usd})"
                ),
            )
        )

    shares = int(limits.max_notional_per_ticker.amount // latest_price.amount)
    qty = Quantity(shares)
    notional = Money(latest_price.amount * shares)
    return Ok(
        EntryPlan(
            ticker=ticker,
            qty=qty,
            notional=notional,
            limit=limits.max_notional_per_ticker,
            reference_price=latest_price,
        )
    )
