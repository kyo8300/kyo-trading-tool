"""Benchmark closes and per-day position marks (live-readiness LR-13 / LR-14).

Same conventions as `portfolio_repository.py`: parameterized SQL only,
idempotent upserts, no auto-commit (callers use `trader.ledger.db.transaction()`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from trader.domain.models import BenchmarkPrice, PositionMark
from trader.domain.money import Money, Price, Quantity, from_canonical, to_canonical
from trader.domain.result import Err, Ok, Result
from trader.ledger.db import LedgerError
from trader.ledger.repository import LedgerCorruptionError


def _decimal_or_raise(value: str) -> Decimal:
    result = from_canonical(value)
    if isinstance(result, Err):
        raise LedgerCorruptionError(f"stored decimal '{value}' is not canonical")
    return result.value


def _quantity_or_raise(value: str) -> Quantity:
    result = Quantity.parse(value)
    if isinstance(result, Err):
        raise LedgerCorruptionError(f"stored quantity '{value}' is not a whole share count")
    return result.value


def _row_to_benchmark(row: sqlite3.Row) -> BenchmarkPrice:
    return BenchmarkPrice(
        ticker=row["ticker"],
        price_date=row["price_date"],
        close=Price(_decimal_or_raise(row["close"])),
        taken_at=datetime.fromisoformat(row["taken_at"]),
    )


def _row_to_mark(row: sqlite3.Row) -> PositionMark:
    return PositionMark(
        mark_date=row["mark_date"],
        ticker=row["ticker"],
        qty=_quantity_or_raise(row["qty"]),
        avg_cost=Price(_decimal_or_raise(row["avg_cost"])),
        mark_price=Price(_decimal_or_raise(row["mark_price"])),
        unrealized_pnl=Money(_decimal_or_raise(row["unrealized_pnl"])),
        taken_at=datetime.fromisoformat(row["taken_at"]),
    )


def upsert_benchmark_price(
    conn: sqlite3.Connection,
    ticker: str,
    price_date: str,
    close: Price,
    taken_at: datetime,
) -> Result[BenchmarkPrice, LedgerError]:
    """Insert or overwrite the close for (`ticker`, `price_date`)."""
    try:
        conn.execute(
            """
            INSERT INTO benchmark_prices (ticker, price_date, close, taken_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (ticker, price_date) DO UPDATE SET
                close = excluded.close,
                taken_at = excluded.taken_at
            """,
            (ticker, price_date, to_canonical(close.amount), taken_at.isoformat()),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not upsert benchmark price: {exc}"))
    return Ok(BenchmarkPrice(ticker, price_date, close, taken_at))


def list_benchmark_prices(conn: sqlite3.Connection, ticker: str) -> tuple[BenchmarkPrice, ...]:
    """Return every stored close for `ticker`, oldest `price_date` first."""
    rows = conn.execute(
        "SELECT * FROM benchmark_prices WHERE ticker = ? ORDER BY price_date ASC",
        (ticker,),
    ).fetchall()
    return tuple(_row_to_benchmark(row) for row in rows)


def upsert_position_mark(
    conn: sqlite3.Connection, mark: PositionMark
) -> Result[PositionMark, LedgerError]:
    """Insert or overwrite the mark for (`mark.mark_date`, `mark.ticker`)."""
    try:
        conn.execute(
            """
            INSERT INTO position_marks (
                mark_date, ticker, qty, avg_cost, mark_price, unrealized_pnl, taken_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (mark_date, ticker) DO UPDATE SET
                qty = excluded.qty,
                avg_cost = excluded.avg_cost,
                mark_price = excluded.mark_price,
                unrealized_pnl = excluded.unrealized_pnl,
                taken_at = excluded.taken_at
            """,
            (
                mark.mark_date,
                mark.ticker,
                to_canonical(Decimal(mark.qty.shares)),
                to_canonical(mark.avg_cost.amount),
                to_canonical(mark.mark_price.amount),
                to_canonical(mark.unrealized_pnl.amount),
                mark.taken_at.isoformat(),
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not upsert position mark: {exc}"))
    return Ok(mark)


def list_position_marks(conn: sqlite3.Connection, mark_date: str) -> tuple[PositionMark, ...]:
    """Return every mark recorded for `mark_date`, ordered by ticker."""
    rows = conn.execute(
        "SELECT * FROM position_marks WHERE mark_date = ? ORDER BY ticker ASC",
        (mark_date,),
    ).fetchall()
    return tuple(_row_to_mark(row) for row in rows)


def delete_position_mark(
    conn: sqlite3.Connection, mark_date: str, ticker: str
) -> Result[None, LedgerError]:
    """Remove the mark for (`mark_date`, `ticker`); no-op when absent."""
    try:
        conn.execute(
            "DELETE FROM position_marks WHERE mark_date = ? AND ticker = ?",
            (mark_date, ticker),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not delete position mark: {exc}"))
    return Ok(None)
