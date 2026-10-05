"""T-1 (live-readiness): AC-5 manual origin / exit reason round-trip the ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.domain.models import Action, Decision, ExitReason, Origin, RuleCheck, Trade
from trader.domain.money import Money, Price
from trader.ledger import portfolio_repository as repo
from trader.ledger import repository as decision_repo
from trader.ledger.db import open_db, transaction

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "trader.sqlite3").value
    yield connection
    connection.close()


def _decision(decision_id: str, origin: Origin, action: Action) -> Decision:
    return Decision(
        id=decision_id,
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=action,
        origin=origin,
        confidence=None,
        rationale="manual close",
        evidence_mention_ids=(),
        llm_model=None,
        prompt_sha256=None,
        response_sha256=None,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="manual",
        proposed_notional=Money(Decimal("70.00")),
        reference_price=Price(Decimal("10")),
    )


def _seed(conn) -> None:
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


def test_ac5_enum_values_exist() -> None:
    assert Origin.manual.value == "manual"
    assert ExitReason.manual.value == "manual"
    assert Origin("manual") is Origin.manual
    assert ExitReason("manual") is ExitReason.manual


def test_ac5_existing_enum_members_are_preserved() -> None:
    assert {o.value for o in Origin} >= {"llm", "rule_exit", "manual"}
    assert {r.value for r in ExitReason} >= {
        "stop_loss",
        "trailing_stop",
        "max_holding_days",
        "llm",
        "manual",
    }


def test_ac5_manual_decision_round_trips_through_decisions_table(conn) -> None:
    _seed(conn)
    decision = _decision("dec-manual", Origin.manual, Action.sell)

    with transaction(conn):
        decision_repo.insert_decision(conn, decision)

    stored = decision_repo.get_decision(conn, "dec-manual")
    assert stored == decision
    assert stored.origin is Origin.manual


def test_ac5_manual_trade_round_trips_through_trades_table(conn) -> None:
    _seed(conn)
    with transaction(conn):
        decision_repo.insert_decision(conn, _decision("dec-entry", Origin.llm, Action.buy))
        decision_repo.insert_decision(conn, _decision("dec-exit", Origin.manual, Action.sell))
    trade = Trade(
        id="trade-1",
        ticker="ABCD",
        opened_at=_NOW,
        closed_at=_NOW,
        entry_decision_id="dec-entry",
        exit_decision_ids=("dec-exit",),
        exit_reason=ExitReason.manual,
        realized_pnl=Money(Decimal("3.50")),
        fees=Money(Decimal("0")),
        holding_days=2,
    )

    with transaction(conn):
        repo.insert_trade(conn, trade)

    stored = repo.list_trades(conn)
    assert stored == (trade,)
    assert stored[0].exit_reason is ExitReason.manual


def test_ac5_unknown_origin_is_rejected() -> None:
    with pytest.raises(ValueError):
        Origin("bogus")
    with pytest.raises(ValueError):
        ExitReason("bogus")
