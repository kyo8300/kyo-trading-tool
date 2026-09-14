"""T-4: equity_snapshots upsert (design "ドローダウン判定の計算") and
engine_state set/get/delete (R-19 halted persistence)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.domain.models import Action, Decision, ExitReason, Origin, Position, RuleCheck, Trade
from trader.domain.money import Money, Price, Quantity
from trader.ledger import portfolio_repository as repo
from trader.ledger import repository as decision_repo
from trader.ledger.db import open_db, transaction

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "trader.sqlite3").value
    yield connection
    connection.close()


# --- positions / trades ----------------------------------------------------


def test_upsert_position_insert_then_update(conn) -> None:
    position = Position(
        ticker="ABCD",
        qty=Quantity(7),
        avg_cost=Price(Decimal("10")),
        opened_at=_NOW,
        high_watermark=Price(Decimal("10")),
        partial_tp_done=False,
    )
    with transaction(conn):
        repo.upsert_position(conn, position)

    assert repo.get_position(conn, "ABCD") == position

    updated = Position(
        ticker="ABCD",
        qty=Quantity(7),
        avg_cost=Price(Decimal("10")),
        opened_at=_NOW,
        high_watermark=Price(Decimal("15")),
        partial_tp_done=True,
    )
    with transaction(conn):
        repo.upsert_position(conn, updated)

    stored = repo.get_position(conn, "ABCD")
    assert stored.high_watermark == Price(Decimal("15"))
    assert stored.partial_tp_done is True
    assert len(repo.list_positions(conn)) == 1


def test_delete_position_removes_row(conn) -> None:
    position = Position(
        ticker="ABCD",
        qty=Quantity(7),
        avg_cost=Price(Decimal("10")),
        opened_at=_NOW,
        high_watermark=Price(Decimal("10")),
        partial_tp_done=False,
    )
    with transaction(conn):
        repo.upsert_position(conn, position)
    with transaction(conn):
        repo.delete_position(conn, "ABCD")

    assert repo.get_position(conn, "ABCD") is None
    assert repo.list_positions(conn) == ()


def test_insert_trade_and_list_trades_round_trip(conn) -> None:
    with transaction(conn):
        repo.insert_rule_set(
            conn,
            sha256="ruleset-sha",
            approved_by="kyo",
            approved_at="2026-01-01T00:00:00+00:00",
            capital_usd=Money(Decimal("500")),
            content_yaml="capital_usd: '500'\n",
        )
        repo.insert_cycle(conn, cycle_id="cycle-1", started_at=_NOW, mode="paper")

    entry_decision = Decision(
        id="dec-entry",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=("m-1",),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="notional 70.00 <= limit 75.00 (15% of 500)",
        proposed_notional=Money(Decimal("70.00")),
        reference_price=Price(Decimal("10")),
    )
    with transaction(conn):
        decision_repo.insert_decision(conn, entry_decision)

    trade = Trade(
        id="trade-1",
        ticker="ABCD",
        opened_at=_NOW,
        closed_at=_NOW,
        entry_decision_id="dec-entry",
        exit_decision_ids=("dec-exit",),
        exit_reason=ExitReason.stop_loss,
        realized_pnl=Money(Decimal("-14.00")),
        fees=Money(Decimal("0")),
        holding_days=3,
    )

    with transaction(conn):
        repo.insert_trade(conn, trade)

    stored = repo.list_trades(conn)
    assert len(stored) == 1
    assert stored[0] == trade


# --- equity_snapshots ------------------------------------------------------


def test_upsert_equity_snapshot_same_day_twice_stays_one_row(conn) -> None:
    with transaction(conn):
        repo.upsert_equity_snapshot(
            conn,
            snapshot_date="2026-01-05",
            mode="paper",
            cash=Money(Decimal("400")),
            positions_value=Money(Decimal("100")),
            peak_equity=Money(Decimal("500")),
            drawdown_pct=Decimal("0"),
            taken_at=_NOW,
        )
    with transaction(conn):
        repo.upsert_equity_snapshot(
            conn,
            snapshot_date="2026-01-05",
            mode="paper",
            cash=Money(Decimal("380")),
            positions_value=Money(Decimal("140")),
            peak_equity=Money(Decimal("520")),
            drawdown_pct=Decimal("0"),
            taken_at=_NOW,
        )

    rows = conn.execute("SELECT * FROM equity_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["equity"] == "520.00"
    assert rows[0]["peak_equity"] == "520.00"


def test_upsert_equity_snapshot_peak_equity_is_max(conn) -> None:
    with transaction(conn):
        repo.upsert_equity_snapshot(
            conn,
            snapshot_date="2026-01-05",
            mode="paper",
            cash=Money(Decimal("400")),
            positions_value=Money(Decimal("120")),
            peak_equity=Money(Decimal("520")),
            drawdown_pct=Decimal("0"),
            taken_at=_NOW,
        )
    with transaction(conn):
        # equity drops back to 470, but peak_equity (caller-computed max) stays 520
        repo.upsert_equity_snapshot(
            conn,
            snapshot_date="2026-01-05",
            mode="paper",
            cash=Money(Decimal("400")),
            positions_value=Money(Decimal("70")),
            peak_equity=Money(Decimal("520")),
            drawdown_pct=Decimal("9.62"),
            taken_at=_NOW,
        )

    row = conn.execute("SELECT * FROM equity_snapshots").fetchone()
    assert row["equity"] == "470.00"
    assert row["peak_equity"] == "520.00"
    assert row["drawdown_pct"] == "9.62"


def test_upsert_equity_snapshot_new_day_is_a_new_row(conn) -> None:
    with transaction(conn):
        repo.upsert_equity_snapshot(
            conn,
            snapshot_date="2026-01-05",
            mode="paper",
            cash=Money(Decimal("400")),
            positions_value=Money(Decimal("100")),
            peak_equity=Money(Decimal("500")),
            drawdown_pct=Decimal("0"),
            taken_at=_NOW,
        )
    with transaction(conn):
        repo.upsert_equity_snapshot(
            conn,
            snapshot_date="2026-01-06",
            mode="paper",
            cash=Money(Decimal("410")),
            positions_value=Money(Decimal("110")),
            peak_equity=Money(Decimal("520")),
            drawdown_pct=Decimal("0"),
            taken_at=_NOW,
        )

    rows = conn.execute(
        "SELECT snapshot_date FROM equity_snapshots ORDER BY snapshot_date"
    ).fetchall()
    assert [r["snapshot_date"] for r in rows] == ["2026-01-05", "2026-01-06"]


# --- engine_state ------------------------------------------------------


def test_engine_state_set_get_delete_round_trip(conn) -> None:
    assert repo.get_engine_state(conn, "halted") is None

    with transaction(conn):
        repo.set_engine_state(conn, "halted", "true")

    assert repo.get_engine_state(conn, "halted") == "true"

    with transaction(conn):
        repo.delete_engine_state(conn, "halted")

    assert repo.get_engine_state(conn, "halted") is None


def test_engine_state_set_overwrites_existing_value(conn) -> None:
    with transaction(conn):
        repo.set_engine_state(conn, "halted_reason", "daily loss limit")
    with transaction(conn):
        repo.set_engine_state(conn, "halted_reason", "drawdown limit")

    assert repo.get_engine_state(conn, "halted_reason") == "drawdown limit"
