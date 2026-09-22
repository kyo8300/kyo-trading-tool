"""`engine.reconcile.reconcile_positions`: the ledger's `positions` and the
broker's open positions must agree ticker-by-ticker on share count. Any
difference is reported as a human-readable line so the cycle can refuse new
buys until a human looks (the ledger, not the broker balance, drives every
rule decision, so it must be provably in sync with what the broker holds)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trader.broker.broker import BrokerPosition
from trader.domain.models import Position
from trader.domain.money import Price, Quantity
from trader.engine.reconcile import reconcile_positions

_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


def _held(ticker: str, qty: int) -> Position:
    return Position(
        ticker=ticker,
        qty=Quantity(qty),
        avg_cost=Price(Decimal("10.00")),
        opened_at=_NOW,
        high_watermark=Price(Decimal("10.00")),
        partial_tp_done=False,
    )


def _broker(ticker: str, qty: int) -> BrokerPosition:
    return BrokerPosition(ticker=ticker, qty=Quantity(qty), avg_cost=Price(Decimal("10.00")))


def test_matching_books_report_nothing() -> None:
    ledger = (_held("AAA", 3), _held("BBB", 1))
    broker = (_broker("BBB", 1), _broker("AAA", 3))

    assert reconcile_positions(ledger, broker) == ()


def test_both_empty_report_nothing() -> None:
    assert reconcile_positions((), ()) == ()


def test_quantity_difference_is_reported_with_both_sides() -> None:
    mismatches = reconcile_positions((_held("AAA", 3),), (_broker("AAA", 2),))

    assert mismatches == ("AAA: ledger 3 shares, broker 2 shares",)


def test_ledger_only_and_broker_only_tickers_are_reported() -> None:
    mismatches = reconcile_positions((_held("AAA", 3),), (_broker("ZZZ", 5),))

    assert mismatches == (
        "AAA: ledger 3 shares, broker 0 shares",
        "ZZZ: ledger 0 shares, broker 5 shares",
    )
