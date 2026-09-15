"""T-4 / AC-19: every decision records its evidence, LLM identity, both
hashes, the rule set it was checked against, and the check result -- skips
and rejections included.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.domain.models import Action, Decision, Origin, RuleCheck
from trader.domain.money import Money, Price
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger import repository as repo
from trader.ledger.db import open_db, transaction

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "trader.sqlite3").value
    with transaction(connection):
        portfolio_repo.insert_rule_set(
            connection,
            sha256="ruleset-sha",
            approved_by="kyo",
            approved_at="2026-01-01T00:00:00+00:00",
            capital_usd=Money(Decimal("500")),
            content_yaml="capital_usd: '500'\n",
        )
        portfolio_repo.insert_cycle(connection, cycle_id="cycle-1", started_at=_NOW, mode="paper")
    yield connection
    connection.close()


def _decision(**overrides: object) -> Decision:
    base = dict(
        id="dec-1",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=("m-1", "m-2"),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="notional 75.00 <= limit 75.00 (15% of 500)",
        proposed_notional=Money(Decimal("75.00")),
        reference_price=Price(Decimal("10.7143")),
    )
    base.update(overrides)
    return Decision(**base)  # type: ignore[arg-type]


def test_insert_decision_round_trips_evidence_and_hashes(conn) -> None:
    decision = _decision()

    with transaction(conn):
        result = repo.insert_decision(conn, decision)

    assert result.is_ok()
    stored = repo.get_decision(conn, "dec-1")
    assert stored is not None
    assert stored.evidence_mention_ids == ("m-1", "m-2")
    assert stored.llm_model == "claude-sonnet-5"
    assert stored.prompt_sha256 == "p" * 64
    assert stored.response_sha256 == "r" * 64
    assert stored.rule_set_sha256 == "ruleset-sha"
    assert stored.rule_check is RuleCheck.passed
    assert stored.proposed_notional == Money(Decimal("75.00"))
    assert stored.reference_price == Price(Decimal("10.7143"))


def test_skipped_decision_is_recorded(conn) -> None:
    decision = _decision(
        id="dec-skip",
        action=Action.skip,
        origin=Origin.llm,
        confidence=None,
        rule_check=RuleCheck.rejected,
        rule_check_reason="llm output failed schema validation",
        proposed_notional=None,
        reference_price=None,
    )

    with transaction(conn):
        repo.insert_decision(conn, decision)

    stored = repo.get_decision(conn, "dec-skip")
    assert stored is not None
    assert stored.action is Action.skip
    assert stored.confidence is None
    assert stored.proposed_notional is None


def test_rejected_decision_is_recorded(conn) -> None:
    decision = _decision(
        id="dec-rejected",
        action=Action.buy,
        rule_check=RuleCheck.rejected,
        rule_check_reason="notional 80.00 > limit 75.00 (15% of 500)",
    )

    with transaction(conn):
        repo.insert_decision(conn, decision)

    stored = repo.get_decision(conn, "dec-rejected")
    assert stored is not None
    assert stored.rule_check is RuleCheck.rejected
    assert "limit 75.00" in stored.rule_check_reason


def test_list_decisions_filters_by_cycle(conn) -> None:
    with transaction(conn):
        portfolio_repo.insert_cycle(conn, cycle_id="cycle-2", started_at=_NOW, mode="paper")
        repo.insert_decision(conn, _decision(id="dec-a", cycle_id="cycle-1"))
        repo.insert_decision(conn, _decision(id="dec-b", cycle_id="cycle-2"))

    cycle_1_decisions = repo.list_decisions(conn, cycle_id="cycle-1")
    all_decisions = repo.list_decisions(conn)

    assert [d.id for d in cycle_1_decisions] == ["dec-a"]
    assert {d.id for d in all_decisions} == {"dec-a", "dec-b"}
