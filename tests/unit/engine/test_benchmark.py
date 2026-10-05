"""T-4 / AC-9: SPY daily closes are upserted into `benchmark_prices` each cycle
from the start date; non-positive closes are skipped; a market-data failure
never stops the cycle; a halted cycle records nothing."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.broker.fake_broker import FakeBroker
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.money import Money, Price
from trader.domain.result import Err, Ok
from trader.engine import kill_switch
from trader.engine.benchmark import BenchmarkOutcome, benchmark_start, record_benchmark
from trader.engine.cycle import CycleDeps, run_cycle
from trader.ledger.benchmark_repository import list_benchmark_prices
from trader.ledger.db import open_db
from trader.ledger.portfolio_repository import insert_rule_set, upsert_equity_snapshot
from trader.market.data_provider import Bar, MarketClock
from trader.market.fake_data import FakeMarketData
from trader.rules.lock import sha256_of_file
from trader.rules.loss_limits import LossLimitBreach
from trader.rules.schema import derive_limits, load_rules

_RULES = Path(__file__).parent.parent.parent / "fixtures" / "rules" / "valid.yaml"
_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_OPEN = MarketClock(is_open=True, next_open=_NOW, next_close=_NOW + timedelta(hours=1))
_START = date(2026, 9, 10)


def _bar(day: date, close: str) -> Bar:
    return Bar(
        ticker="SPY",
        date=day,
        open=Price(Decimal("500")),
        high=Price(Decimal("501")),
        low=Price(Decimal("499")),
        close=Price(Decimal(close)),
        volume=1_000_000,
    )


def _spy_bars() -> tuple[Bar, ...]:
    # 09-08 is before the start; 09-10 / 09-11 / 09-14 are on or after it;
    # 09-12 has a zero close.
    return (
        _bar(date(2026, 9, 8), "490"),
        _bar(date(2026, 9, 10), "500"),
        _bar(date(2026, 9, 11), "502"),
        _bar(date(2026, 9, 12), "0"),
        _bar(date(2026, 9, 14), "505"),
    )


def _market() -> FakeMarketData:
    return FakeMarketData(prices={}, bars={"SPY": _spy_bars()}, clock=_OPEN)


def _setup(tmp_path: Path):
    conn = open_db(tmp_path / "trader.sqlite3").value
    sha256 = sha256_of_file(_RULES).value
    rule_set = load_rules(_RULES).value
    inserted = insert_rule_set(
        conn,
        sha256=sha256,
        approved_by=rule_set.approved_by,
        approved_at=rule_set.approved_at,
        capital_usd=Money(rule_set.capital_usd),
        content_yaml=_RULES.read_text(encoding="utf-8"),
    )
    assert isinstance(inserted, Ok)
    return conn, rule_set, derive_limits(rule_set), sha256


def _deps(conn: sqlite3.Connection, rule_set, limits, sha256, market, broker=None) -> CycleDeps:
    class _NoLlm:
        model = "none"

        def complete(self, prompt: object):
            raise AssertionError("LLM must not be called without candidates")

    return CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker or FakeBroker(),
        market=market,
        llm=_NoLlm(),
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )


def _snapshot(conn: sqlite3.Connection, snapshot_date: str) -> None:
    zero = Money(Decimal("0"))
    cash = zero  # matches FakeBroker's zero account so no drawdown halt fires
    assert isinstance(
        upsert_equity_snapshot(conn, snapshot_date, "paper", cash, zero, cash, Decimal("0"), _NOW),
        Ok,
    )


def test_ac9_benchmark_start_is_today_when_no_snapshots(tmp_path: Path) -> None:
    conn, *_ = _setup(tmp_path)
    assert benchmark_start(conn, _NOW) == date(2026, 9, 14)
    conn.close()


def test_ac9_benchmark_start_is_oldest_snapshot_date(tmp_path: Path) -> None:
    conn, *_ = _setup(tmp_path)
    _snapshot(conn, "2026-09-12")
    _snapshot(conn, "2026-09-10")
    _snapshot(conn, "2026-09-13")
    assert benchmark_start(conn, _NOW) == date(2026, 9, 10)
    conn.close()


def test_ac9_record_benchmark_backfills_from_start_and_skips_nonpositive(tmp_path: Path) -> None:
    conn, *_ = _setup(tmp_path)

    result = record_benchmark(conn, _market(), _START, _NOW)

    assert result == Ok(BenchmarkOutcome(recorded=3, skipped_nonpositive=1))
    rows = list_benchmark_prices(conn, "SPY")
    assert [(r.price_date, r.close.amount) for r in rows] == [
        ("2026-09-10", Decimal("500")),
        ("2026-09-11", Decimal("502")),
        ("2026-09-14", Decimal("505")),
    ]
    conn.close()


def test_ac9_record_benchmark_is_idempotent_and_overwrites_provisional_close(
    tmp_path: Path,
) -> None:
    conn, *_ = _setup(tmp_path)
    assert isinstance(record_benchmark(conn, _market(), _START, _NOW), Ok)
    revised = _market().with_bars(
        "SPY", (_bar(date(2026, 9, 14), "507"), _bar(date(2026, 9, 11), "502"))
    )

    assert isinstance(record_benchmark(conn, revised, _START, _NOW), Ok)

    rows = {r.price_date: r.close.amount for r in list_benchmark_prices(conn, "SPY")}
    assert rows == {
        "2026-09-10": Decimal("500"),
        "2026-09-11": Decimal("502"),
        "2026-09-14": Decimal("507"),
    }
    conn.close()


def test_ac9_record_benchmark_returns_err_when_market_data_fails(tmp_path: Path) -> None:
    conn, *_ = _setup(tmp_path)

    result = record_benchmark(conn, _market().failing(["daily_bars_since"]), _START, _NOW)

    assert isinstance(result, Err)
    assert result.error.message
    assert list_benchmark_prices(conn, "SPY") == ()
    conn.close()


def test_ac9_record_benchmark_with_no_bars_for_spy_is_err(tmp_path: Path) -> None:
    conn, *_ = _setup(tmp_path)
    market = FakeMarketData(prices={}, bars={}, clock=_OPEN)
    assert isinstance(record_benchmark(conn, market, _START, _NOW), Err)
    conn.close()


def test_ac9_run_cycle_records_spy_from_oldest_snapshot_and_skips_nonpositive(
    tmp_path: Path,
) -> None:
    conn, rule_set, limits, sha256 = _setup(tmp_path)
    _snapshot(conn, "2026-09-10")

    result = run_cycle(_deps(conn, rule_set, limits, sha256, _market()))

    assert isinstance(result, Ok)
    assert result.value.benchmark_error is None
    dates = [r.price_date for r in list_benchmark_prices(conn, "SPY")]
    assert dates == ["2026-09-10", "2026-09-11", "2026-09-14"]
    conn.close()


def test_ac9_run_cycle_without_snapshots_starts_from_today(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _setup(tmp_path)

    result = run_cycle(_deps(conn, rule_set, limits, sha256, _market()))

    assert isinstance(result, Ok)
    dates = [r.price_date for r in list_benchmark_prices(conn, "SPY")]
    assert dates == ["2026-09-14"]
    conn.close()


def test_ac9_run_cycle_twice_does_not_duplicate_rows(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _setup(tmp_path)
    _snapshot(conn, "2026-09-10")
    deps = _deps(conn, rule_set, limits, sha256, _market())

    assert isinstance(run_cycle(deps), Ok)
    first = list_benchmark_prices(conn, "SPY")
    assert isinstance(run_cycle(deps), Ok)

    assert len(list_benchmark_prices(conn, "SPY")) == len(first) == 3
    conn.close()


def test_ac9_run_cycle_continues_and_reports_benchmark_error_when_spy_fetch_fails(
    tmp_path: Path,
) -> None:
    conn, rule_set, limits, sha256 = _setup(tmp_path)
    _snapshot(conn, "2026-09-10")
    market = _market().failing(["daily_bars_since"])
    broker = FakeBroker()

    result = run_cycle(_deps(conn, rule_set, limits, sha256, market, broker))

    assert isinstance(result, Ok)
    assert result.value.outcome == "ok"
    assert result.value.benchmark_error
    assert broker.submit_call_count == 0
    assert list_benchmark_prices(conn, "SPY") == ()
    conn.close()


def test_ac9_halted_cycle_records_no_benchmark(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _setup(tmp_path)
    breach = LossLimitBreach(
        kind="daily",
        observed=Money(Decimal("-20.00")),
        limit=Money(Decimal("15.00")),
        reason="daily P&L -20.00 <= -15.00",
    )
    assert isinstance(kill_switch.trigger(breach, FakeBroker(), conn, FixedClock(_NOW)), Ok)

    result = run_cycle(_deps(conn, rule_set, limits, sha256, _market()))

    assert isinstance(result, Ok)
    assert result.value.outcome == "halted"
    assert result.value.benchmark_error is None
    assert list_benchmark_prices(conn, "SPY") == ()
    conn.close()
