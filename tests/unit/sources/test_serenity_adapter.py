"""AC-4: serenity ingest validates the whole file before returning anything
(R-1, R-2). A single invalid record rejects the entire load with a count
and a handful of example errors, and rejects nothing to the DB.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trader.cli import app
from trader.domain.result import Err, Ok
from trader.ledger.db import open_db
from trader.ledger.source_repository import list_mentions
from trader.sources.serenity.adapter import SerenityAdapter

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "serenity"

runner = CliRunner()


def _data_dir(tmp_path: Path, tweets_filename: str) -> Path:
    data_dir = tmp_path / "serenity"
    data_dir.mkdir()
    (data_dir / "tweets.json").write_text(
        (_FIXTURES / tweets_filename).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (data_dir / "ticker_stats.txt").write_text(
        (_FIXTURES / "ticker_stats.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return data_dir


def test_valid_file_produces_mentions_including_multi_ticker(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path, "tweets_valid.json")
    result = SerenityAdapter().load(data_dir)
    assert isinstance(result, Ok)
    mentions = result.value

    tickers_for_1002 = {m.ticker for m in mentions if m.external_id == "1002"}
    assert tickers_for_1002 == {"AAPL", "TSLA"}

    # $XYZ is not in the whitelist and must not produce a mention.
    assert all(m.ticker != "XYZ" for m in mentions)

    aapl_mentions = [m for m in mentions if m.ticker == "AAPL"]
    assert len(aapl_mentions) == 2


def test_invalid_records_reject_entire_file_with_count_and_examples(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path, "tweets_invalid.json")
    result = SerenityAdapter().load(data_dir)
    assert isinstance(result, Err)
    error = result.error
    assert error.invalid_count == 3
    assert 1 <= len(error.first_errors) <= 5
    assert all(e.startswith("index ") for e in error.first_errors)


def test_missing_tweets_file_is_err(tmp_path: Path) -> None:
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    (data_dir / "ticker_stats.txt").write_text("AAPL 1\n", encoding="utf-8")
    result = SerenityAdapter().load(data_dir)
    assert isinstance(result, Err)


def test_invalid_json_is_err(tmp_path: Path) -> None:
    data_dir = tmp_path / "bad-json"
    data_dir.mkdir()
    (data_dir / "tweets.json").write_text("not json", encoding="utf-8")
    (data_dir / "ticker_stats.txt").write_text("AAPL 1\n", encoding="utf-8")
    result = SerenityAdapter().load(data_dir)
    assert isinstance(result, Err)


def test_ingest_cli_rejects_invalid_file_and_writes_nothing(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path, "tweets_invalid.json")
    db_path = tmp_path / "trader.sqlite3"

    result = runner.invoke(
        app,
        ["ingest", "--source", "serenity", "--data-dir", str(data_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 1
    assert "3" in result.output

    conn = open_db(db_path).value
    try:
        assert list_mentions(conn, "serenity") == ()
    finally:
        conn.close()


def test_ingest_cli_accepts_valid_file_and_is_idempotent(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path, "tweets_valid.json")
    db_path = tmp_path / "trader.sqlite3"

    first = runner.invoke(
        app,
        ["ingest", "--source", "serenity", "--data-dir", str(data_dir), "--db", str(db_path)],
    )
    assert first.exit_code == 0

    conn = open_db(db_path).value
    try:
        mentions_after_first = list_mentions(conn, "serenity")
    finally:
        conn.close()
    assert len(mentions_after_first) > 0

    second = runner.invoke(
        app,
        ["ingest", "--source", "serenity", "--data-dir", str(data_dir), "--db", str(db_path)],
    )
    assert second.exit_code == 0
    assert "変更なし" in second.output

    conn = open_db(db_path).value
    try:
        mentions_after_second = list_mentions(conn, "serenity")
    finally:
        conn.close()
    assert mentions_after_second == mentions_after_first
