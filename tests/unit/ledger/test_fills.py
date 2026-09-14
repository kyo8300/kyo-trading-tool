"""T-4 / AC-17: fill polling records partial fills, terminal fills, and
non-fill terminal states (canceled/rejected) on `orders.status`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.domain.models import (
    Action,
    Approval,
    Approver,
    Decision,
    Fill,
    Order,
    OrderStatus,
    Origin,
    RuleCheck,
    Side,
    with_status,
)
from trader.domain.money import Money, Price, Quantity
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
        repo.insert_decision(
            connection,
            Decision(
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
                rule_check_reason="notional 70.00 <= limit 75.00 (15% of 500)",
                proposed_notional=Money(Decimal("70.00")),
                reference_price=Price(Decimal("10")),
            ),
        )
        repo.insert_approval(
            connection,
            Approval(
                id="app-1",
                decision_id="dec-1",
                approver=Approver.system,
                approved_at=_NOW,
                expires_at=_NOW.replace(hour=21),
            ),
        )
    yield connection
    connection.close()


def _order(order_id: str, client_order_id: str) -> Order:
    return Order(
        id=order_id,
        decision_id="dec-1",
        approval_id="app-1",
        client_order_id=client_order_id,
        broker_order_id=None,
        mode="paper",
        side=Side.buy,
        qty=Quantity(7),
        order_type="market",
        status=OrderStatus.recorded,
        submitted_at=None,
        last_error=None,
    )


def test_partial_fills_progress_order_to_filled(conn) -> None:
    order = _order("order-1", "order_dec-1")
    with transaction(conn):
        repo.insert_order(conn, order)
        repo.update_order_status(conn, "order-1", OrderStatus.submitted)

    with transaction(conn):
        repo.insert_fill(
            conn,
            Fill(
                id="fill-1",
                order_id="order-1",
                filled_at=_NOW,
                qty=Quantity(4),
                price=Price(Decimal("10")),
                fee=Money(Decimal("0")),
            ),
        )
        repo.update_order_status(conn, "order-1", OrderStatus.partially_filled)

    assert repo.get_order(conn, "order-1").status is OrderStatus.partially_filled
    assert len(repo.list_fills(conn, "order-1")) == 1

    with transaction(conn):
        repo.insert_fill(
            conn,
            Fill(
                id="fill-2",
                order_id="order-1",
                filled_at=_NOW,
                qty=Quantity(3),
                price=Price(Decimal("10")),
                fee=Money(Decimal("0")),
            ),
        )
        repo.update_order_status(conn, "order-1", OrderStatus.filled)

    final_order = repo.get_order(conn, "order-1")
    fills = repo.list_fills(conn, "order-1")
    assert final_order.status is OrderStatus.filled
    assert len(fills) == 2
    assert sum(f.qty.shares for f in fills) == 7


def test_canceled_order_has_no_fills(conn) -> None:
    order = _order("order-2", "order_dec-1-cancel")
    with transaction(conn):
        repo.insert_order(conn, order)
        repo.update_order_status(conn, "order-2", OrderStatus.submitted)
        repo.update_order_status(conn, "order-2", OrderStatus.canceled)

    stored = repo.get_order(conn, "order-2")
    assert stored.status is OrderStatus.canceled
    assert repo.list_fills(conn, "order-2") == ()


def test_rejected_order_records_last_error(conn) -> None:
    order = _order("order-3", "order_dec-1-reject")
    with transaction(conn):
        repo.insert_order(conn, order)
        repo.update_order_status(
            conn, "order-3", OrderStatus.rejected, last_error="insufficient buying power"
        )

    stored = repo.get_order(conn, "order-3")
    assert stored.status is OrderStatus.rejected
    assert stored.last_error == "insufficient buying power"


def test_with_status_helper_does_not_mutate_original() -> None:
    order = _order("order-4", "order_dec-1-helper")

    updated = with_status(order, OrderStatus.failed)

    assert order.status is OrderStatus.recorded
    assert updated.status is OrderStatus.failed
