"""T-4: migrations are idempotent, transactions roll back, FKs are enforced."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trader.ledger.db import apply_migrations, open_db, run_in_transaction, transaction


def test_open_db_creates_parent_dir_and_applies_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "trader.sqlite3"

    result = open_db(db_path)

    assert result.is_ok()
    conn = result.value
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    expected = {
        "sources",
        "mentions",
        "rule_sets",
        "decisions",
        "approvals",
        "orders",
        "fills",
        "positions",
        "trades",
        "equity_snapshots",
        "engine_state",
        "cycles",
        "schema_migrations",
    }
    assert expected.issubset(tables)
    conn.close()


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "trader.sqlite3"
    conn = open_db(db_path).value

    second = apply_migrations(conn)
    third = apply_migrations(conn)

    assert second.is_ok()
    assert third.is_ok()
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert len(versions) == len(set(versions))
    assert len(versions) == 1
    conn.close()


def test_foreign_keys_pragma_is_on(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value

    row = conn.execute("PRAGMA foreign_keys").fetchone()

    assert row[0] == 1
    conn.close()


def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value

    class _BoomError(Exception):
        pass

    with pytest.raises(_BoomError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO sources (source_id, name, imported_at, file_sha256, record_count) "
                "VALUES ('serenity', 'Serenity', '2026-01-01T00:00:00+00:00', 'abc', 1)"
            )
            raise _BoomError

    remaining = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    assert remaining == 0
    conn.close()


def test_transaction_commits_on_success(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value

    with transaction(conn):
        conn.execute(
            "INSERT INTO sources (source_id, name, imported_at, file_sha256, record_count) "
            "VALUES ('serenity', 'Serenity', '2026-01-01T00:00:00+00:00', 'abc', 1)"
        )

    remaining = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    assert remaining == 1
    conn.close()


def test_foreign_keys_reject_orphan_approval(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value

    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO approvals (id, decision_id, approver, approved_at, expires_at) "
                "VALUES ('app-orphan', 'no-such-decision', 'system', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T21:00:00+00:00')"
            )

    remaining = conn.execute("SELECT COUNT(*) AS n FROM approvals").fetchone()["n"]
    assert remaining == 0
    conn.close()


def test_run_in_transaction_returns_err_on_exception(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value

    def _boom(c: object) -> None:
        conn.execute(
            "INSERT INTO sources (source_id, name, imported_at, file_sha256, record_count) "
            "VALUES ('serenity', 'Serenity', '2026-01-01T00:00:00+00:00', 'abc', 1)"
        )
        raise sqlite3.OperationalError("simulated failure")

    result = run_in_transaction(conn, _boom)

    assert result.is_err()
    remaining = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    assert remaining == 0
    conn.close()


def test_run_in_transaction_returns_ok_value_on_success(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value

    def _insert(c: object) -> str:
        conn.execute(
            "INSERT INTO sources (source_id, name, imported_at, file_sha256, record_count) "
            "VALUES ('serenity', 'Serenity', '2026-01-01T00:00:00+00:00', 'abc', 1)"
        )
        return "done"

    result = run_in_transaction(conn, _insert)

    assert result.is_ok()
    assert result.value == "done"
    conn.close()


def test_open_db_rejects_unwritable_path(tmp_path: Path) -> None:
    unwritable_parent = tmp_path / "not_a_dir"
    unwritable_parent.write_text("i am a file, not a directory")

    result = open_db(unwritable_parent / "trader.sqlite3")

    assert result.is_err()
    assert "trader.sqlite3" not in result.error.message
