"""T-12 / AC-15: DB writes must succeed before `Broker.submit_market_order`
is ever called, and a submit failure is recorded (never retried) (R-16)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.broker.fake_broker import FakeBroker
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.models import (
    Action,
    Approval,
    Approver,
    Decision,
    OrderStatus,
    Origin,
    RuleCheck,
    Side,
)
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok
from trader.engine import order_executor
from trader.engine.approval import _make
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


def _decision(decision_id: str = "dec-1") -> Decision:
    return Decision(
        id=decision_id,
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


def _approved_request(decision: Decision):
    approval = Approval(
        id="appr-1",
        decision_id=decision.id,
        approver=Approver.system,
        approved_at=_NOW,
        expires_at=_NOW,
    )
    return _make(decision, approval, Side.buy, Quantity(7))


def test_successful_execute_records_then_submits_exactly_once(conn) -> None:
    decision = _decision()
    req = _approved_request(decision)
    broker = FakeBroker()

    result = order_executor.execute(req, conn, broker, FixedClock(_NOW), TradingMode.paper)

    assert isinstance(result, Ok)
    assert result.value.status is OrderStatus.submitted
    assert broker.submit_call_count == 1

    stored_order = repo.get_order(conn, f"order_{decision.id}")
    assert stored_order is not None
    assert stored_order.status is OrderStatus.submitted
    assert repo.get_decision(conn, decision.id) is not None


def test_decision_insert_failure_prevents_broker_submit(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision()
    req = _approved_request(decision)
    broker = FakeBroker()

    def _boom(*_args: object, **_kwargs: object):
        return Err(repo.LedgerError("simulated decision insert failure"))

    monkeypatch.setattr(order_executor, "insert_decision", _boom)

    result = order_executor.execute(req, conn, broker, FixedClock(_NOW), TradingMode.paper)

    assert isinstance(result, Err)
    assert broker.submit_call_count == 0
    assert repo.get_order(conn, f"order_{decision.id}") is None
    assert repo.get_decision(conn, decision.id) is None


def test_approval_insert_failure_prevents_broker_submit(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision()
    req = _approved_request(decision)
    broker = FakeBroker()

    def _boom(*_args: object, **_kwargs: object):
        return Err(repo.LedgerError("simulated approval insert failure"))

    monkeypatch.setattr(order_executor, "insert_approval", _boom)

    result = order_executor.execute(req, conn, broker, FixedClock(_NOW), TradingMode.paper)

    assert isinstance(result, Err)
    assert broker.submit_call_count == 0
    assert repo.get_order(conn, f"order_{decision.id}") is None
    assert repo.get_decision(conn, decision.id) is None


def test_order_insert_failure_prevents_broker_submit(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    decision = _decision()
    req = _approved_request(decision)
    broker = FakeBroker()

    def _boom(*_args: object, **_kwargs: object):
        return Err(repo.LedgerError("simulated order insert failure"))

    monkeypatch.setattr(order_executor, "insert_order", _boom)

    result = order_executor.execute(req, conn, broker, FixedClock(_NOW), TradingMode.paper)

    assert isinstance(result, Err)
    assert broker.submit_call_count == 0
    assert repo.get_order(conn, f"order_{decision.id}") is None
    assert repo.get_decision(conn, decision.id) is None


def test_broker_submit_failure_marks_order_failed_and_is_never_retried(conn) -> None:
    decision = _decision()
    req = _approved_request(decision)
    broker = FakeBroker().fail_next_submit()

    result = order_executor.execute(req, conn, broker, FixedClock(_NOW), TradingMode.paper)

    assert isinstance(result, Err)
    # FakeBroker's simulated failure is consumed by the one submit attempt
    # (it does not append to `.submitted`, so `submit_call_count` stays 0
    # here); the failure being recorded once, with no retry, is what matters.

    stored_order = repo.get_order(conn, f"order_{decision.id}")
    assert stored_order is not None
    assert stored_order.status is OrderStatus.failed
    assert stored_order.last_error is not None


def test_broker_raising_instead_of_returning_err_is_not_swallowed(conn) -> None:
    """Prove-It (AC-15): if the broker *raises* rather than returning
    `Err(...)`, `execute` does not catch it in a silent `except` -- the
    exception propagates so the caller (and its logs) see it, rather than
    being hidden. This locks in the current implementation's decision
    (no broad `except Exception` around `broker.submit_market_order`)."""
    decision = _decision()
    req = _approved_request(decision)

    class _RaisingBroker(FakeBroker):
        def submit_market_order(self, req):  # type: ignore[override]
            raise RuntimeError("simulated broker crash")

    broker = _RaisingBroker()

    with pytest.raises(RuntimeError, match="simulated broker crash"):
        order_executor.execute(req, conn, broker, FixedClock(_NOW), TradingMode.paper)

    # The record-before-submit write already committed (decision/approval/
    # order(recorded)) before the broker was ever called -- only the
    # subsequent status update to submitted/failed didn't happen.
    stored_order = repo.get_order(conn, f"order_{decision.id}")
    assert stored_order is not None
    assert stored_order.status is OrderStatus.recorded


def test_already_persisted_decision_is_not_re_inserted(conn) -> None:
    decision = _decision(decision_id="dec-2")
    with transaction(conn):
        repo.insert_decision(conn, decision)
    req = _approved_request(decision)
    broker = FakeBroker()

    result = order_executor.execute(req, conn, broker, FixedClock(_NOW), TradingMode.paper)

    assert isinstance(result, Ok)
    assert broker.submit_call_count == 1
