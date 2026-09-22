"""Migration 0002 discards `equity_snapshots` rows written before equity was
derived from `capital_usd`: those rows carry the broker's account balance
(Alpaca paper: $100,000) as `cash`, so their `peak_equity` would make every
later capital-based snapshot look like a ~97% drawdown."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path

from trader.domain.money import Money
from trader.ledger.db import open_db
from trader.ledger.portfolio_repository import list_equity_snapshots, upsert_equity_snapshot

_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


def _open_at_version_0001(db_path: Path) -> sqlite3.Connection:
    sql = (resources.files("trader.ledger.migrations") / "0001_init.sql").read_text("utf-8")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(sql)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO schema_migrations VALUES ('0001_init.sql', ?)", (_NOW.isoformat(),))
    conn.commit()
    return conn


def test_upgrade_from_0001_drops_broker_balance_snapshots(tmp_path: Path) -> None:
    db_path = tmp_path / "trader.sqlite3"
    old = _open_at_version_0001(db_path)
    upsert_equity_snapshot(
        old,
        "2026-09-15",
        "paper",
        Money(Decimal("99000.00")),
        Money(Decimal("1000.00")),
        Money(Decimal("100000.00")),
        Decimal("0"),
        _NOW,
    )
    old.commit()
    old.close()

    conn = open_db(db_path).value
    try:
        assert list_equity_snapshots(conn) == ()
        versions = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        assert "0002_reset_equity_snapshots.sql" in versions
    finally:
        conn.close()


def test_fresh_database_applies_0002_without_error(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "fresh.sqlite3").value
    try:
        assert list_equity_snapshots(conn) == ()
    finally:
        conn.close()
