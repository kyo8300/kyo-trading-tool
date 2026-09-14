"""T-12 / AC-16: paper auto-approval, live approval lookup and expiry (R-17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.models import Action, Approval, Approver, Decision, Origin, RuleCheck
from trader.domain.money import Money, Price
from trader.domain.result import Err, Ok
from trader.engine.approval import ApprovedOrderRequest, record_human_approval, resolve_approval
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger import repository as repo
from trader.ledger.db import open_db, transaction
from trader.market.data_provider import MarketClock

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_NEXT_CLOSE = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


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
        evidence_mention_ids=("m-1",),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="notional 75.00 <= limit 75.00 (15% of 500)",
        proposed_notional=Money(Decimal("75.00")),
        reference_price=Price(Decimal("10.0000")),
    )
    base.update(overrides)
    return Decision(**base)  # type: ignore[arg-type]


_MARKET_CLOCK = MarketClock(is_open=True, next_open=_NOW, next_close=_NEXT_CLOSE)


def test_rejected_decision_never_resolves_to_an_approval(conn) -> None:
    decision = _decision(rule_check=RuleCheck.rejected, rule_check_reason="notional too high")
    result = resolve_approval(decision, TradingMode.paper, conn, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Err)


def test_paper_mode_auto_approves_with_system_approver(conn) -> None:
    decision = _decision()
    result = resolve_approval(decision, TradingMode.paper, conn, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Ok)
    req = result.value
    assert isinstance(req, ApprovedOrderRequest)
    assert req.approval.approver is Approver.system
    assert req.approval.expires_at == _NEXT_CLOSE
    assert req.qty.shares == 7
    assert req.client_order_id == "order_dec-1"


def test_live_mode_without_any_approval_is_rejected(conn) -> None:
    decision = _decision(mode="live")
    result = resolve_approval(decision, TradingMode.live, conn, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Err)


def test_live_mode_with_expired_approval_is_rejected(conn) -> None:
    decision = _decision(mode="live")
    with transaction(conn):
        repo.insert_decision(conn, decision)
        repo.insert_approval(
            conn,
            Approval(
                id="appr-expired",
                decision_id=decision.id,
                approver=Approver.kyo,
                approved_at=_NOW - timedelta(days=2),
                expires_at=_NOW - timedelta(days=1),
            ),
        )
    result = resolve_approval(decision, TradingMode.live, conn, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Err)


def test_live_mode_with_valid_kyo_approval_succeeds(conn) -> None:
    decision = _decision(mode="live")
    with transaction(conn):
        repo.insert_decision(conn, decision)
        repo.insert_approval(
            conn,
            Approval(
                id="appr-valid",
                decision_id=decision.id,
                approver=Approver.kyo,
                approved_at=_NOW,
                expires_at=_NEXT_CLOSE,
            ),
        )
    result = resolve_approval(decision, TradingMode.live, conn, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Ok)
    assert result.value.approval.approver is Approver.kyo


def test_record_human_approval_inserts_a_kyo_approval(conn) -> None:
    decision = _decision(mode="live")
    with transaction(conn):
        repo.insert_decision(conn, decision)

    result = record_human_approval(conn, decision.id, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Ok)
    approval = result.value
    assert approval.approver is Approver.kyo
    assert approval.expires_at == _NEXT_CLOSE

    stored = repo.find_valid_approval(conn, decision.id, _NOW)
    assert stored is not None
    assert stored.approver is Approver.kyo


def test_record_human_approval_rejects_unknown_decision(conn) -> None:
    result = record_human_approval(conn, "no-such-decision", FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Err)


def test_record_human_approval_rejects_a_failed_rule_check(conn) -> None:
    decision = _decision(id="dec-2", rule_check=RuleCheck.rejected, rule_check_reason="too big")
    with transaction(conn):
        repo.insert_decision(conn, decision)

    result = record_human_approval(conn, decision.id, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Err)
