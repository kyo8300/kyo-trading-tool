"""Sources / mentions: the record of what aggregated data was ingested and
when (R-1, R-2, R-4).

Ingest is idempotent: `insert_mentions_many` uses `INSERT OR IGNORE` against
the `(source_id, external_id, ticker)` uniqueness constraint, so re-running
`trader ingest` on the same file never duplicates rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import cast

from trader.domain.models import Mention
from trader.domain.result import Err, Ok, Result
from trader.ledger.db import LedgerError


def _dt_to_text(value: datetime) -> str:
    return value.isoformat()


def _dt_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_mention(row: sqlite3.Row) -> Mention:
    return Mention(
        id=row["id"],
        source_id=row["source_id"],
        external_id=row["external_id"],
        ticker=row["ticker"],
        posted_at=_dt_from_text(row["posted_at"]),
        text_excerpt=row["text_excerpt"],
        url=row["url"],
        raw_sha256=row["raw_sha256"],
    )


def upsert_source(
    conn: sqlite3.Connection,
    source_id: str,
    name: str,
    imported_at: datetime,
    file_sha256: str,
    record_count: int,
) -> Result[None, LedgerError]:
    """Record (or update) an ingest run's provenance for `source_id` (R-2)."""
    try:
        conn.execute(
            """
            INSERT INTO sources (source_id, name, imported_at, file_sha256, record_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                name = excluded.name,
                imported_at = excluded.imported_at,
                file_sha256 = excluded.file_sha256,
                record_count = excluded.record_count
            """,
            (source_id, name, _dt_to_text(imported_at), file_sha256, record_count),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not upsert source: {exc}"))
    return Ok(None)


def get_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    """Return the raw row for `source_id`, or `None` if it was never ingested."""
    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    return cast("sqlite3.Row | None", row)


def insert_mentions_many(
    conn: sqlite3.Connection, mentions: tuple[Mention, ...]
) -> Result[int, LedgerError]:
    """Insert `mentions`, skipping any that duplicate an existing
    `(source_id, external_id, ticker)`. Returns the number of rows actually
    inserted (0 on a fully idempotent re-ingest)."""
    try:
        inserted = 0
        for mention in mentions:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO mentions (
                    id, source_id, external_id, ticker, posted_at, text_excerpt, url, raw_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mention.id,
                    mention.source_id,
                    mention.external_id,
                    mention.ticker,
                    _dt_to_text(mention.posted_at),
                    mention.text_excerpt,
                    mention.url,
                    mention.raw_sha256,
                ),
            )
            inserted += cursor.rowcount
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert mentions: {exc}"))
    return Ok(inserted)


def list_mentions(conn: sqlite3.Connection, source_id: str) -> tuple[Mention, ...]:
    """Return every mention ingested from `source_id`, oldest first."""
    rows = conn.execute(
        "SELECT * FROM mentions WHERE source_id = ? ORDER BY posted_at ASC", (source_id,)
    ).fetchall()
    return tuple(_row_to_mention(row) for row in rows)
