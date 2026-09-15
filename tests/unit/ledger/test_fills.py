"""T-4 / AC-17: fill polling records partial fills, terminal fills, and
non-fill terminal states (canceled/rejected) on `orders.status`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.broker.broker import BrokerOrder
from trader.domain.clock import FixedClock
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
from trader.domain.result import Ok
from trader.engine.fills import poll_and_settle
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


def test_duplicate_client_order_id_is_rejected(conn) -> None:
    order_a = _order("order-dup-a", "order_dec-1-dup")
    order_b = _order("order-dup-b", "order_dec-1-dup")

    with transaction(conn):
        repo.insert_order(conn, order_a)

    with transaction(conn):
        result = repo.insert_order(conn, order_b)

    assert result.is_err()
    stored_ids = {
        row["id"] for row in conn.execute("SELECT id FROM orders WHERE decision_id = 'dec-1'")
    }
    assert stored_ids == {"order-dup-a"}


def test_duplicate_client_order_id_raises_integrity_error_when_uncaught(conn) -> None:
    order_a = _order("order-dup-c", "order_dec-1-dup-2")
    order_b = _order("order-dup-d", "order_dec-1-dup-2")
    with transaction(conn):
        repo.insert_order(conn, order_a)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO orders ("
            "id, decision_id, approval_id, client_order_id, broker_order_id,"
            " mode, side, qty, order_type, status, submitted_at, last_error"
            ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                order_b.id,
                order_b.decision_id,
                order_b.approval_id,
                order_b.client_order_id,
                order_b.mode,
                order_b.side.value,
                "7",
                order_b.order_type,
                order_b.status.value,
            ),
        )


def test_update_order_status_records_broker_order_id(conn) -> None:
    order = _order("order-5", "order_dec-1-broker-id")
    with transaction(conn):
        repo.insert_order(conn, order)

    with transaction(conn):
        repo.update_order_status(
            conn, "order-5", OrderStatus.submitted, broker_order_id="alpaca-xyz"
        )

    stored = repo.get_order(conn, "order-5")
    assert stored.status is OrderStatus.submitted
    assert stored.broker_order_id == "alpaca-xyz"


def test_fill_fee_round_trips_as_canonical_decimal(conn) -> None:
    order = _order("order-6", "order_dec-1-fee")
    with transaction(conn):
        repo.insert_order(conn, order)
        repo.insert_fill(
            conn,
            Fill(
                id="fill-fee",
                order_id="order-6",
                filled_at=_NOW,
                qty=Quantity(7),
                price=Price(Decimal("10")),
                fee=Money(Decimal("0.35")),
            ),
        )

    fills = repo.list_fills(conn, "order-6")
    assert len(fills) == 1
    assert fills[0].fee == Money(Decimal("0.35"))


def test_with_status_helper_does_not_mutate_original() -> None:
    order = _order("order-4", "order_dec-1-helper")

    updated = with_status(order, OrderStatus.failed)

    assert order.status is OrderStatus.recorded
    assert updated.status is OrderStatus.failed


def test_list_orders_round_trips_every_order(conn) -> None:
    order_a = _order("order-list-a", "order_dec-1-list-a")
    order_b = _order("order-list-b", "order_dec-1-list-b")
    with transaction(conn):
        repo.insert_order(conn, order_a)
        repo.insert_order(conn, order_b)

    stored = repo.list_orders(conn)

    assert {o.id for o in stored} >= {"order-list-a", "order-list-b"}
    by_id = {o.id: o for o in stored}
    assert by_id["order-list-a"] == order_a
    assert by_id["order-list-b"] == order_b


def test_list_all_fills_round_trips_fills_across_orders(conn) -> None:
    order_a = _order("order-list-c", "order_dec-1-list-c")
    order_b = _order("order-list-d", "order_dec-1-list-d")
    fill_a = Fill(
        id="fill-list-a",
        order_id="order-list-c",
        filled_at=_NOW,
        qty=Quantity(3),
        price=Price(Decimal("10")),
        fee=Money(Decimal("0")),
    )
    fill_b = Fill(
        id="fill-list-b",
        order_id="order-list-d",
        filled_at=_NOW,
        qty=Quantity(4),
        price=Price(Decimal("12")),
        fee=Money(Decimal("0.10")),
    )
    with transaction(conn):
        repo.insert_order(conn, order_a)
        repo.insert_order(conn, order_b)
        repo.insert_fill(conn, fill_a)
        repo.insert_fill(conn, fill_b)

    stored = repo.list_all_fills(conn)

    by_id = {f.id: f for f in stored}
    assert by_id["fill-list-a"] == fill_a
    assert by_id["fill-list-b"] == fill_b


def test_list_all_fills_is_empty_with_no_fills(conn) -> None:
    assert repo.list_all_fills(conn) == ()


def test_partial_fill_delta_records_incremental_price_not_brokers_cumulative_avg(
    conn,
) -> None:
    """R-18 review finding (fills.py:200): `poll_and_settle` records every
    new (cumulative-delta) fill at `broker_order.filled_avg_price` --
    but that field is the *broker's running average price across the whole
    order so far*, not the price of just the newly-filled shares. For a
    partial fill followed by a further fill at a different price, this
    double-counts the first fill's contribution into the second fill's
    price.

    1st poll: filled_qty=3, filled_avg_price=10.00 (order average so far).
    2nd poll: filled_qty=7 (delta +4), filled_avg_price=11.00 (order
    average across all 7 shares). The true price of the 4 incremental
    shares is `(7*11.00 - 3*10.00) / 4 = 11.75`, not the broker's
    already-blended 11.00 -- so `fills` row 2 must have `qty=4`,
    `price=11.75`, and the resulting `positions.avg_cost` (blending
    3 @ 10.00 and 4 @ 11.75) must be exactly `11.0000`.
    """

    class _TwoStepBroker:
        """A minimal `Broker` double: `get_order` returns a fixed sequence
        of (cumulative) broker-reported fill states, one per call."""

        def __init__(self) -> None:
            self._responses = [
                BrokerOrder(
                    broker_order_id="b-1",
                    client_order_id="order_dec-1",
                    status=OrderStatus.partially_filled,
                    filled_qty=Quantity(3),
                    filled_avg_price=Price(Decimal("10.00")),
                    updated_at=_NOW,
                ),
                BrokerOrder(
                    broker_order_id="b-1",
                    client_order_id="order_dec-1",
                    status=OrderStatus.filled,
                    filled_qty=Quantity(7),
                    filled_avg_price=Price(Decimal("11.00")),
                    updated_at=_NOW,
                ),
            ]

        def get_order(self, client_order_id: str):
            return Ok(self._responses.pop(0))

    order = _order("order-partial-avg", "order_dec-1")
    with transaction(conn):
        repo.insert_order(conn, order)
        repo.update_order_status(conn, "order-partial-avg", OrderStatus.submitted)

    decision = repo.get_decision(conn, "dec-1")

    poll_result = poll_and_settle(
        conn,
        _TwoStepBroker(),
        FixedClock(_NOW),
        lambda _s: None,
        "order_dec-1",
        "order-partial-avg",
        decision,
        Side.buy,
        None,
        poll_timeout_s=10,
        poll_interval_s=1,
    )

    assert isinstance(poll_result, Ok)
    assert poll_result.value.fills_recorded == 2

    fills = repo.list_fills(conn, "order-partial-avg")
    assert len(fills) == 2
    assert fills[0].qty.shares == 3
    assert fills[0].price.amount == Decimal("10.00")
    assert fills[1].qty.shares == 4
    assert fills[1].price.amount == Decimal("11.75")

    position = portfolio_repo.get_position(conn, "ABCD")
    assert position is not None
    assert position.avg_cost.amount == Decimal("11.0000")


def test_stale_poll_with_no_notional_progress_records_no_fill_and_still_advances(
    conn,
) -> None:
    """R-18 review fix (fills.py `delta_notional.amount > 0` guard): a
    poll that reports the exact same cumulative `filled_qty`/
    `filled_avg_price` as the previous poll (a stale/duplicate broker read
    with zero incremental notional) must not fabricate a zero-or-negative
    `delta_price` fill. It must be skipped, `recorded_qty`/
    `recorded_notional` stay put, and the *next* poll picks up the real
    fill once the broker's numbers actually move. Polling must keep
    advancing (`sleep` is still called) rather than looping forever on the
    no-progress poll."""

    class _StaleThenAdvanceBroker:
        def __init__(self) -> None:
            self._responses = [
                BrokerOrder(
                    broker_order_id="b-3",
                    client_order_id="order_dec-1",
                    status=OrderStatus.partially_filled,
                    filled_qty=Quantity(3),
                    filled_avg_price=Price(Decimal("10.00")),
                    updated_at=_NOW,
                ),
                # Stale/duplicate read: identical cumulative qty and average
                # price as the previous poll -> delta_notional == 0.
                BrokerOrder(
                    broker_order_id="b-3",
                    client_order_id="order_dec-1",
                    status=OrderStatus.partially_filled,
                    filled_qty=Quantity(3),
                    filled_avg_price=Price(Decimal("10.00")),
                    updated_at=_NOW,
                ),
                BrokerOrder(
                    broker_order_id="b-3",
                    client_order_id="order_dec-1",
                    status=OrderStatus.filled,
                    filled_qty=Quantity(7),
                    filled_avg_price=Price(Decimal("11.00")),
                    updated_at=_NOW,
                ),
            ]

        def get_order(self, client_order_id: str):
            return Ok(self._responses.pop(0))

    order = _order("order-stale-poll", "order_dec-1")
    with transaction(conn):
        repo.insert_order(conn, order)
        repo.update_order_status(conn, "order-stale-poll", OrderStatus.submitted)

    decision = repo.get_decision(conn, "dec-1")
    sleep_calls: list[int] = []

    poll_result = poll_and_settle(
        conn,
        _StaleThenAdvanceBroker(),
        FixedClock(_NOW),
        sleep_calls.append,
        "order_dec-1",
        "order-stale-poll",
        decision,
        Side.buy,
        None,
        poll_timeout_s=10,
        poll_interval_s=1,
    )

    assert isinstance(poll_result, Ok)
    # Only 2 real fills recorded -- the stale second poll added nothing.
    assert poll_result.value.fills_recorded == 2
    assert poll_result.value.final_status is OrderStatus.filled

    fills = repo.list_fills(conn, "order-stale-poll")
    assert [f.qty.shares for f in fills] == [3, 4]
    assert [f.price.amount for f in fills] == [Decimal("10.00"), Decimal("11.75")]

    # Polling advanced past the no-progress poll instead of looping forever
    # on it: `sleep` was called once between poll 1 and poll 2, and once
    # between poll 2 (stale, no terminal status) and poll 3 (terminal).
    assert sleep_calls == [1, 1]


def test_three_poll_partial_fill_deltas_each_get_their_own_incremental_price(conn) -> None:
    """R-18: extends the two-poll case to three polls (3 -> 5 -> 7 shares,
    broker cumulative average 10.00 -> 10.40 -> 11.00). Each poll's
    incremental price must be derived from the delta against the *previous*
    poll, not re-blended from the broker's running average, so `fills`
    ends up with 3 rows priced 10.00 / 11.00 / 12.50 and the resulting
    `positions.avg_cost` is exactly `11.0000`
    ((3*10.00 + 2*11.00 + 2*12.50) / 7 = 77.00 / 7)."""

    class _ThreeStepBroker:
        def __init__(self) -> None:
            self._responses = [
                BrokerOrder(
                    broker_order_id="b-2",
                    client_order_id="order_dec-1",
                    status=OrderStatus.partially_filled,
                    filled_qty=Quantity(3),
                    filled_avg_price=Price(Decimal("10.00")),
                    updated_at=_NOW,
                ),
                BrokerOrder(
                    broker_order_id="b-2",
                    client_order_id="order_dec-1",
                    status=OrderStatus.partially_filled,
                    filled_qty=Quantity(5),
                    filled_avg_price=Price(Decimal("10.40")),
                    updated_at=_NOW,
                ),
                BrokerOrder(
                    broker_order_id="b-2",
                    client_order_id="order_dec-1",
                    status=OrderStatus.filled,
                    filled_qty=Quantity(7),
                    filled_avg_price=Price(Decimal("11.00")),
                    updated_at=_NOW,
                ),
            ]

        def get_order(self, client_order_id: str):
            return Ok(self._responses.pop(0))

    order = _order("order-three-poll", "order_dec-1")
    with transaction(conn):
        repo.insert_order(conn, order)
        repo.update_order_status(conn, "order-three-poll", OrderStatus.submitted)

    decision = repo.get_decision(conn, "dec-1")

    poll_result = poll_and_settle(
        conn,
        _ThreeStepBroker(),
        FixedClock(_NOW),
        lambda _s: None,
        "order_dec-1",
        "order-three-poll",
        decision,
        Side.buy,
        None,
        poll_timeout_s=10,
        poll_interval_s=1,
    )

    assert isinstance(poll_result, Ok)
    assert poll_result.value.fills_recorded == 3

    fills = repo.list_fills(conn, "order-three-poll")
    assert [f.qty.shares for f in fills] == [3, 2, 2]
    assert [f.price.amount for f in fills] == [
        Decimal("10.00"),
        Decimal("11.00"),
        Decimal("12.50"),
    ]

    position = portfolio_repo.get_position(conn, "ABCD")
    assert position is not None
    assert position.avg_cost.amount == Decimal("11.0000")
