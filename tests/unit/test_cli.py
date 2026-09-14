"""CLI smoke tests (AC-29): every subcommand's --help exits 0."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trader.cli import app
from trader.domain.clock import FixedClock
from trader.domain.models import Action, Decision, Origin, RuleCheck
from trader.domain.money import Money, Price
from trader.engine import kill_switch
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger import repository as repo
from trader.ledger.db import open_db, transaction

runner = CliRunner()

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)

COMMANDS: list[list[str]] = [
    ["ingest", "--help"],
    ["run-cycle", "--help"],
    ["approve", "--help"],
    ["report", "--help"],
    ["rules", "approve", "--help"],
    ["resume", "--help"],
    ["status", "--help"],
]


@pytest.mark.parametrize("args", COMMANDS, ids=[" ".join(c[:-1]) for c in COMMANDS])
def test_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output


def test_ingest_without_a_data_dir_reports_a_human_readable_error() -> None:
    # T-7 replaced the stub body with a real implementation (see
    # tests/unit/sources/test_serenity_adapter.py for full coverage). With
    # no --data-dir and no data/sources/serenity/ present, it should fail
    # with a human-readable message rather than a stack trace.
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 1
    assert "not implemented" not in result.output
    assert "trader ingest:" in result.output


def _decision(decision_id: str, db_path: Path) -> Decision:
    decision = Decision(
        id=decision_id,
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="live",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=(),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="notional 75.00 <= limit 75.00 (15% of 500)",
        proposed_notional=Money(Decimal("75.00")),
        reference_price=Price(Decimal("10.0000")),
    )
    conn = open_db(db_path).value
    with transaction(conn):
        portfolio_repo.insert_rule_set(
            conn,
            sha256="ruleset-sha",
            approved_by="kyo",
            approved_at="2026-01-01T00:00:00+00:00",
            capital_usd=Money(Decimal("500")),
            content_yaml="capital_usd: '500'\n",
        )
        portfolio_repo.insert_cycle(conn, cycle_id="cycle-1", started_at=_NOW, mode="live")
        repo.insert_decision(conn, decision)
    conn.close()
    return decision


def test_approve_without_expires_at_exits_1(tmp_path: Path) -> None:
    db_path = tmp_path / "trader.sqlite3"
    decision = _decision("dec-1", db_path)

    result = runner.invoke(app, ["approve", decision.id, "--db", str(db_path)])

    assert result.exit_code == 1
    assert "--expires-at is required" in result.output


def test_approve_with_expires_at_records_a_kyo_approval(tmp_path: Path) -> None:
    db_path = tmp_path / "trader.sqlite3"
    decision = _decision("dec-2", db_path)

    result = runner.invoke(
        app,
        [
            "approve",
            decision.id,
            "--expires-at",
            "2026-01-05T21:00:00+00:00",
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    conn = open_db(db_path).value
    approval = repo.find_valid_approval(conn, decision.id, _NOW)
    assert approval is not None
    assert approval.approver.value == "kyo"
    conn.close()


def test_status_reports_halted_positions_and_last_cycle(tmp_path: Path) -> None:
    from trader.domain.models import Position
    from trader.domain.money import Quantity

    db_path = tmp_path / "trader.sqlite3"
    conn = open_db(db_path).value
    with transaction(conn):
        portfolio_repo.insert_cycle(conn, cycle_id="cycle-1", started_at=_NOW, mode="paper")
        portfolio_repo.finish_cycle(conn, cycle_id="cycle-1", finished_at=_NOW, outcome="ok")
        portfolio_repo.upsert_position(
            conn,
            Position(
                ticker="ABCD",
                qty=Quantity(7),
                avg_cost=Money(Decimal("10.0000")),
                opened_at=_NOW,
                high_watermark=Money(Decimal("10.0000")),
                partial_tp_done=False,
            ),
        )
    from trader.rules.loss_limits import LossLimitBreach

    breach = LossLimitBreach(
        kind="daily",
        observed=Money(Decimal("-20.00")),
        limit=Money(Decimal("15.00")),
        reason="daily P&L -20.00 <= -15.00",
    )
    from trader.broker.fake_broker import FakeBroker

    kill_switch.trigger(breach, FakeBroker(), conn, FixedClock(_NOW))
    conn.close()

    result = runner.invoke(app, ["status", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "HALTED" in result.output
    assert "ABCD" in result.output
    assert "cycle-1" in result.output


def test_resume_when_halted_clears_state(tmp_path: Path) -> None:
    from trader.broker.fake_broker import FakeBroker
    from trader.rules.loss_limits import LossLimitBreach

    db_path = tmp_path / "trader.sqlite3"
    conn = open_db(db_path).value
    breach = LossLimitBreach(
        kind="daily",
        observed=Money(Decimal("-20.00")),
        limit=Money(Decimal("15.00")),
        reason="daily P&L -20.00 <= -15.00",
    )
    kill_switch.trigger(breach, FakeBroker(), conn, FixedClock(_NOW))
    conn.close()

    result = runner.invoke(app, ["resume", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "resumed" in result.output
    conn = open_db(db_path).value
    assert not kill_switch.is_halted(conn)
    conn.close()


def test_report_shows_a_closed_trade(tmp_path: Path) -> None:
    from trader.domain.models import ExitReason, Trade

    db_path = tmp_path / "trader.sqlite3"
    rules_path = Path(__file__).parent.parent / "fixtures" / "rules" / "valid.yaml"
    decision = _decision("dec-report", db_path)

    conn = open_db(db_path).value
    with transaction(conn):
        portfolio_repo.insert_trade(
            conn,
            Trade(
                id="dec-report:dec-report-exit",
                ticker="ABCD",
                opened_at=_NOW,
                closed_at=_NOW,
                entry_decision_id=decision.id,
                exit_decision_ids=("dec-report-exit",),
                exit_reason=ExitReason.stop_loss,
                realized_pnl=Money(Decimal("-14.00")),
                fees=Money(Decimal("0")),
                holding_days=3,
            ),
        )
    conn.close()

    result = runner.invoke(
        app,
        ["report", "--db", str(db_path), "--rules", str(rules_path)],
    )

    assert result.exit_code == 0, result.output
    assert "ABCD" in result.output
    assert "-14.00" in result.output
    assert "stop_loss" in result.output


def test_resume_when_not_halted_reports_not_halted_without_erroring(tmp_path: Path) -> None:
    db_path = tmp_path / "trader.sqlite3"
    open_db(db_path).value.close()

    result = runner.invoke(app, ["resume", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert "not halted" in result.output
