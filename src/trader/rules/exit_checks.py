"""Sell-side rule checks for held positions (R-13). Sells never wait on the
LLM: this module decides on its own from `Position` + current `price`.

Checked in priority order: stop loss -> max holding days -> partial take
profit (only if not already done) -> trailing stop (only once partial take
profit is done). The first condition met wins; at most one `ExitSignal` is
returned per call.

`ExitSignal.fraction` is the fraction of the *current* position to sell
(1 = full exit). Converting a fraction to an actual share count happens
downstream (order construction / `trade_closer`), not here, because this
module only sees the position's rule state, not order-sizing concerns. When
`position.qty` is 1 share, `floor(1 * partial_take_profit_fraction)` is 0;
callers that turn `fraction` into a share count must treat that case as a
full sell (`max(1, floor(qty * fraction))`) rather than silently selling
nothing, since a partial-take-profit signal always means "sell something
now".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trader.domain.models import ExitReason, ExitSignal, Position
from trader.domain.money import Price
from trader.rules.schema import RuleSet

_HUNDRED = Decimal(100)


def check_exit(
    position: Position, price: Price, rules: RuleSet, now: datetime
) -> ExitSignal | None:
    """Return the rule-driven sell signal for `position` at `price`, if any."""
    stop_loss_price = position.avg_cost * (Decimal(1) + rules.exit.stop_loss_pct / _HUNDRED)
    if price <= stop_loss_price:
        return ExitSignal(
            ticker=position.ticker,
            reason=ExitReason.stop_loss,
            fraction=Decimal(1),
            trigger_price=price,
        )

    holding_days = (now - position.opened_at).days
    if holding_days >= rules.position.max_holding_days:
        return ExitSignal(
            ticker=position.ticker,
            reason=ExitReason.max_holding_days,
            fraction=Decimal(1),
            trigger_price=price,
        )

    if not position.partial_tp_done:
        take_profit_price = position.avg_cost * (
            Decimal(1) + rules.exit.partial_take_profit_pct / _HUNDRED
        )
        if price >= take_profit_price:
            return ExitSignal(
                ticker=position.ticker,
                reason=ExitReason.partial_take_profit,
                fraction=rules.exit.partial_take_profit_fraction,
                trigger_price=price,
            )
    else:
        trailing_stop_price = position.high_watermark * (
            Decimal(1) - rules.exit.trailing_stop_pct / _HUNDRED
        )
        if price <= trailing_stop_price:
            return ExitSignal(
                ticker=position.ticker,
                reason=ExitReason.trailing_stop,
                fraction=Decimal(1),
                trigger_price=price,
            )

    return None
