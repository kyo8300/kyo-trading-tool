"""T-4: `sources` / `mentions` ingest is idempotent (R-2). Re-inserting the
same mentions must not duplicate rows -- `trader ingest` relies on this to
be safe to re-run on an unchanged file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trader.domain.models import Mention
from trader.ledger import source_repository as repo
from trader.ledger.db import open_db, transaction

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "trader.sqlite3").value
    with transaction(connection):
        repo.upsert_source(
            connection,
            source_id="serenity",
            name="Serenity",
            imported_at=_NOW,
            file_sha256="abc123",
            record_count=3,
        )
    yield connection
    connection.close()


def _mentions() -> tuple[Mention, ...]:
    return (
        Mention(
            id="m-1",
            source_id="serenity",
            external_id="tweet-1",
            ticker="ABCD",
            posted_at=_NOW,
            text_excerpt="excerpt one",
            url=None,
            raw_sha256="h1",
        ),
        Mention(
            id="m-2",
            source_id="serenity",
            external_id="tweet-2",
            ticker="ABCD",
            posted_at=_NOW,
            text_excerpt="excerpt two",
            url=None,
            raw_sha256="h2",
        ),
        Mention(
            id="m-3",
            source_id="serenity",
            external_id="tweet-2",
            ticker="WXYZ",
            posted_at=_NOW,
            text_excerpt="excerpt two, second ticker",
            url=None,
            raw_sha256="h2",
        ),
    )


def test_insert_mentions_many_is_idempotent_on_reingest(conn) -> None:
    mentions = _mentions()

    with transaction(conn):
        first_inserted = repo.insert_mentions_many(conn, mentions)
    with transaction(conn):
        second_inserted = repo.insert_mentions_many(conn, mentions)

    assert first_inserted.is_ok()
    assert first_inserted.value == 3
    assert second_inserted.is_ok()
    assert second_inserted.value == 0

    stored = repo.list_mentions(conn, "serenity")
    assert len(stored) == 3
    assert {m.id for m in stored} == {"m-1", "m-2", "m-3"}


def test_insert_mentions_many_dedupes_by_source_external_id_ticker(conn) -> None:
    duplicate = Mention(
        id="m-1-dup",
        source_id="serenity",
        external_id="tweet-1",
        ticker="ABCD",
        posted_at=_NOW,
        text_excerpt="a different excerpt, same identity",
        url=None,
        raw_sha256="h1-different",
    )

    with transaction(conn):
        repo.insert_mentions_many(conn, _mentions())
    with transaction(conn):
        result = repo.insert_mentions_many(conn, (duplicate,))

    assert result.is_ok()
    assert result.value == 0
    stored = repo.list_mentions(conn, "serenity")
    assert len(stored) == 3


def test_upsert_source_updates_existing_row(conn) -> None:
    with transaction(conn):
        repo.upsert_source(
            conn,
            source_id="serenity",
            name="Serenity",
            imported_at=_NOW,
            file_sha256="new-sha",
            record_count=5,
        )

    row = repo.get_source(conn, "serenity")
    assert row is not None
    assert row["file_sha256"] == "new-sha"
    assert row["record_count"] == 5


def test_get_source_returns_none_when_never_ingested(conn) -> None:
    assert repo.get_source(conn, "no-such-source") is None
