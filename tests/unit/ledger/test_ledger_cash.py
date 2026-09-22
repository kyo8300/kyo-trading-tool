"""`ledger_cash`: the engine's cash is derived from `capital_usd` and the
ledger's own fills (buys subtract, sells add, fees subtract), never from the
broker's account balance -- an Alpaca paper account starts at $100,000 and a
live account's balance is not the strategy's capital."""

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
)
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Ok
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger import repository as repo
from trader.ledger.db import open_db, transaction
from trader.ledger.portfolio_repository import ledger_cash

_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_CAPITAL = Money(Decimal("500.00"))


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "trader.sqlite3").value
    with transaction(connection):
        portfolio_repo.insert_rule_set(
            connection,
            sha256="ruleset-sha",
            approved_by="kyo",
            approved_at="2026-01-01T00:00:00+00:00",
            capital_usd=_CAPITAL,
            content_yaml="capital_usd: '500'\n",
        )
        portfolio_repo.insert_cycle(connection, cycle_id="cycle-1", started_at=_NOW, mode="paper")
        assert isinstance(
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
                    evidence_mention_ids=(),
                    llm_model="fake-model",
                    prompt_sha256="p" * 64,
                    response_sha256="r" * 64,
                    rule_set_sha256="ruleset-sha",
                    rule_check=RuleCheck.passed,
                    rule_check_reason="ok",
                    proposed_notional=Money(Decimal("70.00")),
                    reference_price=Price(Decimal("10")),
                ),
            ),
            Ok,
        )
        assert isinstance(
            repo.insert_approval(
                connection,
                Approval(
                    id="app-1",
                    decision_id="dec-1",
                    approver=Approver.system,
                    approved_at=_NOW,
                    expires_at=_NOW.replace(hour=21),
                ),
            ),
            Ok,
        )
    yield connection
    connection.close()


def _order(order_id: str, side: Side, qty: int) -> Order:
    return Order(
        id=order_id,
        decision_id="dec-1",
        approval_id="app-1",
        client_order_id=f"order_{order_id}",
        broker_order_id=None,
        mode="paper",
        side=side,
        qty=Quantity(qty),
        order_type="market",
        status=OrderStatus.filled,
        submitted_at=_NOW,
        last_error=None,
    )


def _fill(fill_id: str, order_id: str, qty: int, price: str, fee: str = "0") -> Fill:
    return Fill(
        id=fill_id,
        order_id=order_id,
        filled_at=_NOW,
        qty=Quantity(qty),
        price=Price(Decimal(price)),
        fee=Money(Decimal(fee)),
    )


def test_no_fills_means_cash_equals_capital(conn) -> None:
    result = ledger_cash(conn, _CAPITAL)
    assert isinstance(result, Ok)
    assert result.value == _CAPITAL


def test_buy_fills_subtract_and_sell_fills_add_net_of_fees(conn) -> None:
    with transaction(conn):
        assert isinstance(repo.insert_order(conn, _order("o1", Side.buy, 7)), Ok)
        assert isinstance(repo.insert_fill(conn, _fill("f1", "o1", 4, "10.00", "0.10")), Ok)
        assert isinstance(repo.insert_fill(conn, _fill("f2", "o1", 3, "10.50")), Ok)
        assert isinstance(repo.insert_order(conn, _order("o2", Side.sell, 2)), Ok)
        assert isinstance(repo.insert_fill(conn, _fill("f3", "o2", 2, "12.00", "0.05")), Ok)

    result = ledger_cash(conn, _CAPITAL)

    assert isinstance(result, Ok)
    # 500 - (4*10 + 0.10) - 3*10.50 + (2*12 - 0.05) = 500 - 40.10 - 31.50 + 23.95
    assert result.value == Money(Decimal("452.35"))


def test_orders_without_fills_do_not_move_cash(conn) -> None:
    with transaction(conn):
        assert isinstance(repo.insert_order(conn, _order("o1", Side.buy, 7)), Ok)

    result = ledger_cash(conn, _CAPITAL)

    assert isinstance(result, Ok)
    assert result.value == _CAPITAL
