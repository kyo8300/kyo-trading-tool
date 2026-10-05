"""T-12/AC-21: buy -> SPY/mark recorded -> manual close -> report condition 5
is met -> the freed slot is used by the next buy.

Real CLI (`rules approve`, `ingest`, `report`) + a real tmp SQLite ledger;
`run_cycle` / `close_position` are called directly because the `run-cycle`
and `close` CLI commands refuse `TRADER_ENV=test` (N-9). Broker and market
data are fakes; nothing touches the network.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from test_paper_cycle import (  # type: ignore[import-not-found]
    _FakeLlm,
    _ImmediateFillBroker,
    _setup_workspace,
)
from typer.testing import CliRunner

from trader.cli import app as cli_app
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.money import Money, Price
from trader.domain.result import Ok
from trader.engine.cycle import CycleDeps, run_cycle
from trader.engine.manual_close import CloseDeps, close_position
from trader.ledger.db import open_db
from trader.market.data_provider import Bar, MarketClock
from trader.market.fake_data import FakeMarketData
from trader.rules.lock import verify_lock
from trader.rules.schema import derive_limits, load_rules

_DAY1 = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


def _open_clock(now: datetime) -> MarketClock:
    return MarketClock(is_open=True, next_open=now, next_close=now + timedelta(hours=1))


def _bars(ticker: str, close: str, days: int = 20) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            ticker=ticker,
            date=date(2026, 9, 1) + timedelta(days=i),
            open=Price(Decimal(close)),
            high=Price(Decimal(close)),
            low=Price(Decimal(close)),
            close=Price(Decimal(close)),
            volume=300_000,
        )
        for i in range(days)
    )


def _market(now: datetime, aapl: str) -> FakeMarketData:
    return FakeMarketData(
        prices={"AAPL": Price(Decimal(aapl)), "SPY": Price(Decimal("500"))},
        bars={"AAPL": _bars("AAPL", "10"), "SPY": _bars("SPY", "500")},
        clock=_open_clock(now),
    )


def _scalar(conn: sqlite3.Connection, sql: str) -> object:
    return conn.execute(sql).fetchone()[0]


def test_ac21_buy_record_manual_close_report_then_rebuy_in_freed_slot(tmp_path: Path) -> None:
    rules_path, db_path, data_dir = _setup_workspace(tmp_path)
    runner = CliRunner()

    # 1. rules approve + ingest through the CLI.
    approve = runner.invoke(
        cli_app, ["rules", "approve", "--rules", str(rules_path), "--db", str(db_path)]
    )
    assert approve.exit_code == 0, approve.output
    lock = verify_lock(rules_path, rules_path.parent / "trading-rules.lock")
    assert isinstance(lock, Ok), lock
    ingest = runner.invoke(
        cli_app,
        ["ingest", "--source", "serenity", "--data-dir", str(data_dir), "--db", str(db_path)],
    )
    assert ingest.exit_code == 0, ingest.output

    rule_set = load_rules(rules_path).value
    db_result = open_db(db_path)
    assert isinstance(db_result, Ok)
    conn = db_result.value

    clock1 = FixedClock(_DAY1)
    broker1 = _ImmediateFillBroker(fill_price=Price(Decimal("10")))
    deps1 = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=derive_limits(rule_set),
        rule_set_sha256=lock.value.sha256,
        conn=conn,
        broker=broker1,
        market=_market(_DAY1, "10"),
        llm=_FakeLlm(ticker="AAPL"),
        clock=clock1,
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )

    # 2. Day 1: buy 7 shares; SPY close and AAPL mark are recorded.
    result1 = run_cycle(deps1)
    assert isinstance(result1, Ok), result1
    assert result1.value.orders == 1
    assert broker1.submitted[0].qty.shares == 7
    assert _scalar(conn, "SELECT COUNT(*) FROM positions") == 1
    assert _scalar(conn, "SELECT COUNT(*) FROM benchmark_prices WHERE ticker = 'SPY'") != 0
    # Marks are taken for holdings at the start of a cycle (LR-14); nothing was held yet.
    assert _scalar(conn, "SELECT COUNT(*) FROM position_marks") == 0

    # 3. Day 2 ($11): a new mark date appears and SPY closes reach day 2.
    day2_time = _DAY1 + timedelta(days=1)
    clock2 = clock1.advance(timedelta(days=1))
    deps2 = replace(
        deps1,
        broker=_ImmediateFillBroker(fill_price=Price(Decimal("11"))),
        market=_market(day2_time, "11"),
        clock=clock2,
    )
    result2 = run_cycle(deps2)
    assert isinstance(result2, Ok), result2
    marks = conn.execute("SELECT mark_date, ticker, mark_price FROM position_marks").fetchall()
    assert [(r[1], Decimal(r[2])) for r in marks] == [("AAPL", Decimal("11"))]
    day2 = marks[0][0]
    assert day2 == "2026-09-15"
    max_spy = _scalar(conn, "SELECT MAX(price_date) FROM benchmark_prices WHERE ticker = 'SPY'")
    assert max_spy is not None and str(max_spy) >= "2026-09-14"
    assert _scalar(conn, "SELECT COUNT(DISTINCT snapshot_date) FROM equity_snapshots") == 2

    # 4. Manual close at $11: one `manual` trade, position gone, day-2 mark gone.
    close_broker = _ImmediateFillBroker(fill_price=Price(Decimal("11")))
    closed = close_position(
        CloseDeps(
            mode=TradingMode.paper,
            rules=rule_set,
            rule_set_sha256=lock.value.sha256,
            conn=conn,
            broker=close_broker,
            market=_market(day2_time, "11"),
            clock=clock2,
            sleep=lambda _s: None,
            lock_path=None,
        ),
        "AAPL",
        "integration test",
    )
    assert isinstance(closed, Ok), closed
    assert closed.value.slot_freed is True
    trades = conn.execute("SELECT ticker, exit_reason FROM trades").fetchall()
    assert [tuple(r) for r in trades] == [("AAPL", "manual")]
    assert _scalar(conn, "SELECT COUNT(*) FROM positions") == 0
    day2_marks = conn.execute(
        "SELECT COUNT(*) FROM position_marks WHERE mark_date = ?", (day2,)
    ).fetchone()[0]
    assert day2_marks == 0
    assert _scalar(conn, "SELECT COUNT(*) FROM cycles WHERE id LIKE 'close_%'") == 1
    conn.commit()

    # 5. `trader report`: condition 5 met, SPY / portfolio rates shown, manual: 1.
    report = runner.invoke(cli_app, ["report", "--db", str(db_path), "--rules", str(rules_path)])
    assert report.exit_code == 0, report.output
    out = report.output
    assert "移行条件" in out
    assert "条件 5: 満たした" in out
    assert "SPY リターン" in out
    assert "ポートフォリオ損益率" in out
    assert "manual: 1" in out
    assert "合格" not in out

    # 6. Day 3: the freed slot takes a new AAPL buy.
    day3_time = _DAY1 + timedelta(days=2)
    broker3 = _ImmediateFillBroker(fill_price=Price(Decimal("11")))
    deps3 = replace(
        deps1,
        broker=broker3,
        market=_market(day3_time, "11"),
        clock=clock2.advance(timedelta(days=1)),
    )
    result3 = run_cycle(deps3)
    assert isinstance(result3, Ok), result3
    assert result3.value.orders == 1
    assert len(broker3.submitted) == 1
    assert _scalar(conn, "SELECT COUNT(*) FROM positions") == 1
    conn.close()
