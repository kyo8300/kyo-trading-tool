"""Positions / trades / equity snapshots / engine state / cycles / rule sets.

Split out of `repository.py` to stay within the ~400-line file guideline
(N-8); same conventions apply (parameterized SQL only, no auto-commit --
callers use `trader.ledger.db.transaction()`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import cast

from trader.domain.models import ExitReason, Position, Trade
from trader.domain.money import Money, Price, Quantity, from_canonical, to_canonical
from trader.domain.result import Err, Ok, Result
from trader.ledger.db import LedgerError
from trader.ledger.repository import LedgerCorruptionError


def _dt_to_text(value: datetime) -> str:
    return value.isoformat()


def _dt_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value)


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


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        ticker=row["ticker"],
        qty=_quantity_or_raise(row["qty"]),
        avg_cost=Price(_decimal_or_raise(row["avg_cost"])),
        opened_at=_dt_from_text(row["opened_at"]),
        high_watermark=Price(_decimal_or_raise(row["high_watermark"])),
        partial_tp_done=bool(row["partial_tp_done"]),
    )


def _row_to_trade(row: sqlite3.Row) -> Trade:
    return Trade(
        id=row["id"],
        ticker=row["ticker"],
        opened_at=_dt_from_text(row["opened_at"]),
        closed_at=_dt_from_text(row["closed_at"]),
        entry_decision_id=row["entry_decision_id"],
        exit_decision_ids=tuple(json.loads(row["exit_decision_ids"])),
        exit_reason=ExitReason(row["exit_reason"]),
        realized_pnl=Money(_decimal_or_raise(row["realized_pnl"])),
        fees=Money(_decimal_or_raise(row["fees"])),
        holding_days=row["holding_days"],
    )


# --- positions ---------------------------------------------------------


def upsert_position(conn: sqlite3.Connection, position: Position) -> Result[Position, LedgerError]:
    """Insert or replace the position row for `position.ticker`."""
    try:
        conn.execute(
            """
            INSERT INTO positions (
                ticker, qty, avg_cost, opened_at, high_watermark, partial_tp_done
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker) DO UPDATE SET
                qty = excluded.qty,
                avg_cost = excluded.avg_cost,
                opened_at = excluded.opened_at,
                high_watermark = excluded.high_watermark,
                partial_tp_done = excluded.partial_tp_done
            """,
            (
                position.ticker,
                to_canonical(Decimal(position.qty.shares)),
                to_canonical(position.avg_cost.amount),
                _dt_to_text(position.opened_at),
                to_canonical(position.high_watermark.amount),
                int(position.partial_tp_done),
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not upsert position: {exc}"))
    return Ok(position)


def get_position(conn: sqlite3.Connection, ticker: str) -> Position | None:
    """Return the current position for `ticker`, or `None` if flat."""
    row = conn.execute("SELECT * FROM positions WHERE ticker = ?", (ticker,)).fetchone()
    return _row_to_position(row) if row is not None else None


def list_positions(conn: sqlite3.Connection) -> tuple[Position, ...]:
    """Return every open position, ordered by ticker."""
    rows = conn.execute("SELECT * FROM positions ORDER BY ticker ASC").fetchall()
    return tuple(_row_to_position(row) for row in rows)


def delete_position(conn: sqlite3.Connection, ticker: str) -> None:
    """Remove the position row for `ticker` (called when a position closes)."""
    conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))


# --- trades --------------------------------------------------------------


def insert_trade(conn: sqlite3.Connection, trade: Trade) -> Result[Trade, LedgerError]:
    """Persist one closed trade row (R-21)."""
    try:
        conn.execute(
            """
            INSERT INTO trades (
                id, ticker, opened_at, closed_at, entry_decision_id,
                exit_decision_ids, exit_reason, realized_pnl, fees, holding_days
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id,
                trade.ticker,
                _dt_to_text(trade.opened_at),
                _dt_to_text(trade.closed_at),
                trade.entry_decision_id,
                json.dumps(list(trade.exit_decision_ids)),
                trade.exit_reason.value,
                to_canonical(trade.realized_pnl.amount),
                to_canonical(trade.fees.amount),
                trade.holding_days,
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert trade: {exc}"))
    return Ok(trade)


def list_trades(conn: sqlite3.Connection) -> tuple[Trade, ...]:
    """Return every closed trade, oldest first."""
    rows = conn.execute("SELECT * FROM trades ORDER BY closed_at ASC").fetchall()
    return tuple(_row_to_trade(row) for row in rows)


# --- equity snapshots ------------------------------------------------------


def upsert_equity_snapshot(
    conn: sqlite3.Connection,
    snapshot_date: str,
    mode: str,
    cash: Money,
    positions_value: Money,
    peak_equity: Money,
    drawdown_pct: Decimal,
    taken_at: datetime,
) -> Result[None, LedgerError]:
    """Insert or replace today's equity snapshot.

    `equity = cash + positions_value` is computed here; `peak_equity` and
    `drawdown_pct` are the caller's responsibility (design "ドローダウン判定の計算").
    """
    equity = cash + positions_value
    try:
        conn.execute(
            """
            INSERT INTO equity_snapshots (
                snapshot_date, mode, cash, positions_value, equity, peak_equity,
                drawdown_pct, taken_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (snapshot_date) DO UPDATE SET
                mode = excluded.mode,
                cash = excluded.cash,
                positions_value = excluded.positions_value,
                equity = excluded.equity,
                peak_equity = excluded.peak_equity,
                drawdown_pct = excluded.drawdown_pct,
                taken_at = excluded.taken_at
            """,
            (
                snapshot_date,
                mode,
                to_canonical(cash.amount),
                to_canonical(positions_value.amount),
                to_canonical(equity.amount),
                to_canonical(peak_equity.amount),
                to_canonical(drawdown_pct),
                _dt_to_text(taken_at),
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not upsert equity snapshot: {exc}"))
    return Ok(None)


# --- engine state ----------------------------------------------------------


def get_engine_state(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the raw stored value for `key`, or `None` if unset."""
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_engine_state(conn: sqlite3.Connection, key: str, value: str) -> Result[None, LedgerError]:
    """Insert or replace the stored value for `key`."""
    try:
        conn.execute(
            """
            INSERT INTO engine_state (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not set engine state: {exc}"))
    return Ok(None)


def delete_engine_state(conn: sqlite3.Connection, key: str) -> None:
    """Remove the stored value for `key` (e.g. clearing `halted` on resume)."""
    conn.execute("DELETE FROM engine_state WHERE key = ?", (key,))


# --- cycles ------------------------------------------------------------


def insert_cycle(
    conn: sqlite3.Connection, cycle_id: str, started_at: datetime, mode: str
) -> Result[None, LedgerError]:
    """Record the start of a cycle (R-8)."""
    try:
        conn.execute(
            "INSERT INTO cycles (id, started_at, mode) VALUES (?, ?, ?)",
            (cycle_id, _dt_to_text(started_at), mode),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert cycle: {exc}"))
    return Ok(None)


def finish_cycle(
    conn: sqlite3.Connection,
    cycle_id: str,
    finished_at: datetime,
    outcome: str,
    error_summary: str | None = None,
) -> Result[None, LedgerError]:
    """Record the end of a cycle."""
    try:
        cursor = conn.execute(
            "UPDATE cycles SET finished_at = ?, outcome = ?, error_summary = ? WHERE id = ?",
            (_dt_to_text(finished_at), outcome, error_summary, cycle_id),
        )
        if cursor.rowcount == 0:
            return Err(LedgerError(f"no such cycle: {cycle_id}"))
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not finish cycle: {exc}"))
    return Ok(None)


# --- rule sets ---------------------------------------------------------


def insert_rule_set(
    conn: sqlite3.Connection,
    sha256: str,
    approved_by: str,
    approved_at: str,
    capital_usd: Money,
    content_yaml: str,
) -> Result[None, LedgerError]:
    """Record an approved rule set (`trader rules approve`, R-10)."""
    try:
        conn.execute(
            """
            INSERT INTO rule_sets (sha256, approved_by, approved_at, capital_usd, content_yaml)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sha256, approved_by, approved_at, to_canonical(capital_usd.amount), content_yaml),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert rule set: {exc}"))
    return Ok(None)


def get_rule_set(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    """Return the raw row for the rule set with the given hash, if recorded."""
    row = conn.execute("SELECT * FROM rule_sets WHERE sha256 = ?", (sha256,)).fetchone()
    return cast("sqlite3.Row | None", row)
