"""SQLite connection, migrations, and transaction helpers (R-16 foundation).

"Cannot be recorded" must mean "cannot be executed": every ledger write goes
through a real SQLite transaction so that a partial failure leaves nothing
behind. All migrations are plain `.sql` files shipped as package data and
applied idempotently via a `schema_migrations` table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from trader.domain.result import Err, Ok, Result

_MIGRATIONS_PACKAGE = "trader.ledger.migrations"


@dataclass(frozen=True, slots=True)
class LedgerError:
    """A human-readable ledger error. Never includes filesystem paths (N-6)."""

    message: str


def open_db(path: Path) -> Result[sqlite3.Connection, LedgerError]:
    """Open the ledger DB at `path`, creating parent directories and applying
    any pending migrations. Returns a connection configured for concurrent,
    foreign-key-checked access (spec "SQLite の同時実行").
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    except OSError as exc:
        return Err(LedgerError(f"could not open database: {exc.strerror or exc}"))

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    migration_result = apply_migrations(conn)
    if isinstance(migration_result, Err):
        conn.close()
        return Err(migration_result.error)

    return Ok(conn)


def _migration_files() -> list[tuple[str, str]]:
    """Return (version, sql) pairs for every shipped migration, sorted by name."""
    migrations_dir = resources.files(_MIGRATIONS_PACKAGE)
    entries = sorted(
        (entry for entry in migrations_dir.iterdir() if entry.name.endswith(".sql")),
        key=lambda entry: entry.name,
    )
    return [(entry.name, entry.read_text(encoding="utf-8")) for entry in entries]


def apply_migrations(conn: sqlite3.Connection) -> Result[None, LedgerError]:
    """Apply every migration not yet recorded in `schema_migrations`.

    Idempotent: running this against an already-migrated database is a no-op.
    """
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in _migration_files():
            if version in applied:
                continue
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (?, datetime('now'))",
                    (version,),
                )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"migration failed: {exc}"))
    return Ok(None)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block of writes atomically.

    On success the block's writes are committed; on any exception they are
    rolled back and the exception re-raised (callers convert it to `Err`).
    """
    with conn:
        yield conn


def run_in_transaction[T](
    conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], T]
) -> Result[T, LedgerError]:
    """Run `fn(conn)` inside a transaction, converting any failure to `Err`.

    Convenience wrapper around `transaction()` for callers that would
    otherwise have to catch `sqlite3.Error` themselves at every call site.
    """
    try:
        with transaction(conn) as tx:
            return Ok(fn(tx))
    except sqlite3.Error as exc:
        return Err(LedgerError(f"transaction failed: {exc}"))
