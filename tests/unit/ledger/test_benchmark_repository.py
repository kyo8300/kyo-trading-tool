"""T-1 (live-readiness): AC-7 benchmark_prices / position_marks repository."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.domain.models import PositionMark
from trader.domain.money import Money, Price, Quantity
from trader.ledger import benchmark_repository as repo
from trader.ledger import portfolio_repository as portfolio
from trader.ledger.db import open_db, transaction

_T1 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_T2 = datetime(2026, 1, 5, 16, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "trader.sqlite3").value
    yield connection
    connection.close()


def _mark(ticker: str = "ABCD", date: str = "2026-01-05", price: str = "12.5") -> PositionMark:
    return PositionMark(
        mark_date=date,
        ticker=ticker,
        qty=Quantity(7),
        avg_cost=Price(Decimal("10")),
        mark_price=Price(Decimal(price)),
        unrealized_pnl=Money(Decimal("17.5")),
        taken_at=_T1,
    )


def test_ac7_benchmark_upsert_round_trips_decimal_exactly(conn) -> None:
    close = Price(Decimal("512.3456"))
    with transaction(conn):
        result = repo.upsert_benchmark_price(conn, "SPY", "2026-01-05", close, _T1)

    assert result.is_ok()
    stored = repo.list_benchmark_prices(conn, "SPY")
    assert len(stored) == 1
    assert stored[0].close == close
    assert stored[0].taken_at == _T1
    assert stored[0].price_date == "2026-01-05"


def test_ac7_benchmark_upsert_same_key_overwrites(conn) -> None:
    with transaction(conn):
        repo.upsert_benchmark_price(conn, "SPY", "2026-01-05", Price(Decimal("500")), _T1)
        repo.upsert_benchmark_price(conn, "SPY", "2026-01-05", Price(Decimal("501.25")), _T2)
        repo.upsert_benchmark_price(conn, "SPY", "2026-01-05", Price(Decimal("501.25")), _T2)

    stored = repo.list_benchmark_prices(conn, "SPY")
    assert len(stored) == 1
    assert stored[0].close == Price(Decimal("501.25"))
    assert stored[0].taken_at == _T2


def test_ac7_benchmark_list_is_ordered_and_filtered_by_ticker(conn) -> None:
    with transaction(conn):
        repo.upsert_benchmark_price(conn, "SPY", "2026-01-06", Price(Decimal("2")), _T1)
        repo.upsert_benchmark_price(conn, "SPY", "2026-01-05", Price(Decimal("1")), _T1)
        repo.upsert_benchmark_price(conn, "QQQ", "2026-01-05", Price(Decimal("9")), _T1)

    assert [b.price_date for b in repo.list_benchmark_prices(conn, "SPY")] == [
        "2026-01-05",
        "2026-01-06",
    ]
    assert repo.list_benchmark_prices(conn, "NONE") == ()


def test_ac7_position_mark_upsert_round_trips(conn) -> None:
    mark = _mark()
    with transaction(conn):
        assert repo.upsert_position_mark(conn, mark).is_ok()

    assert repo.list_position_marks(conn, "2026-01-05") == (mark,)


def test_ac7_position_mark_upsert_same_key_overwrites(conn) -> None:
    with transaction(conn):
        repo.upsert_position_mark(conn, _mark(price="12.5"))
        repo.upsert_position_mark(conn, replace(_mark(price="13"), taken_at=_T2))

    stored = repo.list_position_marks(conn, "2026-01-05")
    assert len(stored) == 1
    assert stored[0].mark_price == Price(Decimal("13"))
    assert stored[0].taken_at == _T2


def test_ac7_position_marks_are_scoped_by_date_and_sorted_by_ticker(conn) -> None:
    with transaction(conn):
        repo.upsert_position_mark(conn, _mark("ZZZZ"))
        repo.upsert_position_mark(conn, _mark("AAAA"))
        repo.upsert_position_mark(conn, _mark("AAAA", date="2026-01-06"))

    assert [m.ticker for m in repo.list_position_marks(conn, "2026-01-05")] == ["AAAA", "ZZZZ"]
    assert repo.list_position_marks(conn, "2030-01-01") == ()


def test_ac7_delete_position_mark_removes_only_target(conn) -> None:
    with transaction(conn):
        repo.upsert_position_mark(conn, _mark("AAAA"))
        repo.upsert_position_mark(conn, _mark("BBBB"))
        repo.upsert_position_mark(conn, _mark("AAAA", date="2026-01-06"))

    with transaction(conn):
        assert repo.delete_position_mark(conn, "2026-01-05", "AAAA").is_ok()

    assert [m.ticker for m in repo.list_position_marks(conn, "2026-01-05")] == ["BBBB"]
    assert len(repo.list_position_marks(conn, "2026-01-06")) == 1


def test_ac7_delete_missing_mark_is_noop_ok(conn) -> None:
    with transaction(conn):
        assert repo.delete_position_mark(conn, "2026-01-05", "NOPE").is_ok()


def test_ac7_hostile_ticker_is_parameterized_not_executed(conn) -> None:
    evil = "X'; DROP TABLE position_marks; --"
    with transaction(conn):
        repo.upsert_position_mark(conn, _mark(evil))
        repo.upsert_benchmark_price(conn, evil, "2026-01-05", Price(Decimal("1")), _T1)

    assert [m.ticker for m in repo.list_position_marks(conn, "2026-01-05")] == [evil]
    with transaction(conn):
        repo.delete_position_mark(conn, "2026-01-05", evil)
    assert repo.list_position_marks(conn, "2026-01-05") == ()
    assert len(repo.list_benchmark_prices(conn, evil)) == 1


def test_ac7_corrupt_stored_decimal_raises(conn) -> None:
    from trader.ledger.repository import LedgerCorruptionError

    conn.execute("INSERT INTO benchmark_prices VALUES ('SPY', '2026-01-05', 'abc', 't')")
    conn.execute("UPDATE benchmark_prices SET taken_at = '2026-01-05T15:00:00+00:00'")

    with pytest.raises(LedgerCorruptionError):
        repo.list_benchmark_prices(conn, "SPY")


def test_list_halted_cycles_excludes_already_halted_skips(conn) -> None:
    with transaction(conn):
        for cid, hour, outcome, err in [
            ("c-halt", 10, "halted", "daily loss limit"),
            ("c-skip", 11, "halted", "engine is halted"),
            ("c-ok", 12, "completed", None),
            ("c-halt-null", 13, "halted", None),
        ]:
            portfolio.insert_cycle(conn, cid, datetime(2026, 1, 5, hour, 0, tzinfo=UTC), "paper")
            portfolio.finish_cycle(
                conn, cid, datetime(2026, 1, 5, hour, 1, tzinfo=UTC), outcome, err
            )

    halted = portfolio.list_halted_cycles(conn)

    assert [h.id for h in halted] == ["c-halt", "c-halt-null"]
    assert halted[0].error_summary == "daily loss limit"
    assert halted[0].started_at == datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def test_list_halted_cycles_empty(conn) -> None:
    assert portfolio.list_halted_cycles(conn) == ()
