"""Ledger-vs-broker position reconciliation.

Every rule decision (loss limits, drawdown, per-ticker limits) is driven by
the ledger, not by the broker's account, so the ledger must be provably in
sync with what the broker actually holds. This module only compares the two
books; `engine.cycle` decides what to do about a difference (refuse new buys
until a human looks, keep rule-driven sells).
"""

from __future__ import annotations

from collections.abc import Sequence

from trader.broker.broker import BrokerPosition
from trader.domain.models import Position

POSITION_MISMATCH_KEY = "position_mismatch"


def reconcile_positions(
    ledger: Sequence[Position], broker: Sequence[BrokerPosition]
) -> tuple[str, ...]:
    """Return one human-readable line per ticker whose share count differs.

    A ticker present on only one side counts as `0 shares` on the other.
    Lines are sorted by ticker so the result is stable across cycles. An
    empty tuple means the books agree.
    """
    ledger_qty = {p.ticker: p.qty.shares for p in ledger}
    broker_qty = {p.ticker: p.qty.shares for p in broker}
    mismatches: list[str] = []
    for ticker in sorted(set(ledger_qty) | set(broker_qty)):
        held = ledger_qty.get(ticker, 0)
        at_broker = broker_qty.get(ticker, 0)
        if held != at_broker:
            mismatches.append(f"{ticker}: ledger {held} shares, broker {at_broker} shares")
    return tuple(mismatches)
