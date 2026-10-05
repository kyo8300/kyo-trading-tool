"""T-1 (live-readiness): AC-6 migration 0002 applies without disturbing 0001 data."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trader.ledger.db import _migration_files, apply_migrations, open_db

_NEW_TABLES = {"benchmark_prices", "position_marks"}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _apply_only_0001(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    version, sql = _migration_files()[0]
    assert version.startswith("0001")
    conn.executescript(sql)
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
        (version,),
    )
    conn.commit()
    return conn


def test_ac6_0002_exists_after_0001() -> None:
    names = [n for n, _ in _migration_files()]
    assert names[0].startswith("0001")
    assert any(n.startswith("0002") for n in names)


def test_ac6_fresh_db_has_new_tables_and_records_version(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "t.sqlite3").value

    assert _NEW_TABLES <= _tables(conn)
    versions = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert any(v.startswith("0002") for v in versions)
    conn.close()


def test_ac6_applies_to_db_with_only_0001_without_losing_rows(tmp_path: Path) -> None:
    conn = _apply_only_0001(tmp_path / "t.sqlite3")
    assert not (_NEW_TABLES & _tables(conn))
    conn.execute(
        "INSERT INTO cycles (id, started_at, mode) VALUES ('c1', '2026-01-05T15:00:00', 'paper')"
    )
    conn.commit()

    result = apply_migrations(conn)

    assert result.is_ok()
    assert _NEW_TABLES <= _tables(conn)
    assert conn.execute("SELECT id, mode FROM cycles").fetchall()[0][:] == ("c1", "paper")
    versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert sorted(versions) == [n for n, _ in _migration_files()]
    conn.close()


def test_ac6_reapplying_is_a_noop(tmp_path: Path) -> None:
    conn = _apply_only_0001(tmp_path / "t.sqlite3")
    apply_migrations(conn)
    before = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

    again = apply_migrations(conn)

    assert again.is_ok()
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == before
    conn.close()


def test_ac6_primary_keys_reject_duplicates(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "t.sqlite3").value
    conn.execute("INSERT INTO benchmark_prices VALUES ('SPY', '2026-01-05', '1', 't')")
    try:
        conn.execute("INSERT INTO benchmark_prices VALUES ('SPY', '2026-01-05', '2', 't')")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate benchmark_prices key accepted")
    conn.close()
