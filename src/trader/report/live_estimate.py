"""R-23 / Q10: the concrete real-money cost estimate subtracted from paper
P&L, computed purely from `CostAssumptions` (read from the rules file) and
the ledger's recorded fills/order count. No network access, no `float`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trader.domain.models import Fill
from trader.domain.money import Money
from trader.rules.schema import CostAssumptions

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class LiveEstimate:
    """R-23's real-money P&L estimate, with each cost broken out."""

    paper_pnl: Money
    slippage_cost: Money
    commission_cost: Money
    fixed_commission_cost: Money
    fx_cost: Money
    estimated_live_pnl: Money


def _fill_notional(fill: Fill) -> Decimal:
    return fill.price.amount * fill.qty.shares


def _slippage_cost(fills: Sequence[Fill], cost: CostAssumptions) -> Money:
    total = sum(
        (_fill_notional(fill) * cost.slippage_pct_per_side / _HUNDRED for fill in fills), _ZERO
    )
    return Money(total)


def _commission_cost(fills: Sequence[Fill], cost: CostAssumptions) -> Money:
    total = _ZERO
    for fill in fills:
        fill_commission = _fill_notional(fill) * cost.commission_pct / _HUNDRED
        if cost.commission_cap_usd is not None:
            fill_commission = min(fill_commission, cost.commission_cap_usd)
        total += fill_commission
    return Money(total)


def estimate(
    paper_pnl: Money,
    fills: Sequence[Fill],
    order_count: int,
    capital: Money,
    cost: CostAssumptions,
) -> LiveEstimate:
    """Q10's formula: `paper_pnl - slippage - commission - fixed - fx*2`.

    `slippage = Σ(fill notional * slippage_pct_per_side / 100)`.
    `commission = Σ min(fill notional * commission_pct / 100, commission_cap_usd)`
    (no cap when `commission_cap_usd` is `None`).
    `fixed = order_count * commission_usd_per_order`.
    `fx = capital * fx_cost_pct_one_way / 100 * 2` (round-trip, in and out).
    """
    slippage_cost = _slippage_cost(fills, cost)
    commission_cost = _commission_cost(fills, cost)
    fixed_commission_cost = Money(Decimal(order_count) * cost.commission_usd_per_order)
    fx_cost = Money(capital.amount * cost.fx_cost_pct_one_way / _HUNDRED * Decimal(2))

    estimated_live_pnl = (
        paper_pnl - slippage_cost - commission_cost - fixed_commission_cost - fx_cost
    )

    return LiveEstimate(
        paper_pnl=paper_pnl,
        slippage_cost=slippage_cost,
        commission_cost=commission_cost,
        fixed_commission_cost=fixed_commission_cost,
        fx_cost=fx_cost,
        estimated_live_pnl=estimated_live_pnl,
    )
