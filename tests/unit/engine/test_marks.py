"""T-5 / AC-10: `evaluate_holdings` upserts one `position_marks` row per
priced holding; `poll_and_settle` drops that day's mark on a full close and
keeps it on a partial sell."""

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
    ExitReason,
    Order,
    OrderStatus,
    Origin,
    Position,
    PositionMark,
    RuleCheck,
    Side,
)
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok
from trader.engine.fills import poll_and_settle
from trader.engine.holdings import evaluate_holdings
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger import repository as repo
from trader.ledger.benchmark_repository import list_position_marks, upsert_position_mark
from trader.ledger.db import open_db, transaction
from trader.market.data_provider import MarketClock
from trader.market.fake_data import FakeMarketData
from trader.rules.schema import load_rules

_RULES = Path(__file__).parent.parent.parent / "fixtures" / "rules" / "valid.yaml"
_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_NEXT_DAY = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
_DAY = "2026-09-14"
_OPEN = MarketClock(is_open=True, next_open=_NOW, next_close=_NOW)


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


def _position(ticker: str, qty: int, avg: str) -> Position:
    return Position(
        ticker=ticker,
        qty=Quantity(qty),
        avg_cost=Price(Decimal(avg)),
        opened_at=_NOW,
        high_watermark=Price(Decimal(avg)),
        partial_tp_done=False,
    )


def _market(prices: dict[str, str]) -> FakeMarketData:
    return FakeMarketData(
        prices={t: Price(Decimal(p)) for t, p in prices.items()}, bars={}, clock=_OPEN
    )


def _evaluate(conn, positions, market, now=_NOW):
    rules = load_rules(_RULES).value
    return evaluate_holdings(
        conn, positions, market, rules, FixedClock(now), "cycle-1", "ruleset-sha", "paper"
    )


def _marks(conn: sqlite3.Connection, day: str = _DAY) -> dict[str, PositionMark]:
    return {m.ticker: m for m in list_position_marks(conn, day)}


def test_ac10_only_priced_holdings_get_a_mark(conn) -> None:
    positions = (_position("AAAA", 7, "10"), _position("BBBB", 3, "20"))
    result = _evaluate(conn, positions, _market({"AAAA": "10.50"}))

    assert isinstance(result, Ok)
    marks = _marks(conn)
    assert set(marks) == {"AAAA"}
    assert result.value.market_errors == ("BBBB",)


def test_ac10_unrealized_pnl_is_mark_minus_cost_times_qty(conn) -> None:
    result = _evaluate(conn, (_position("AAAA", 7, "10.1234"),), _market({"AAAA": "10.5"}))

    assert isinstance(result, Ok)
    mark = _marks(conn)["AAAA"]
    assert mark.mark_price == Price(Decimal("10.5"))
    assert mark.qty == Quantity(7)
    assert mark.avg_cost == Price(Decimal("10.1234"))
    # (10.5 - 10.1234) * 7 = 2.6362 -> 2.64 (cent quantized)
    assert mark.unrealized_pnl == Money(Decimal("2.64"))
    assert mark.taken_at == _NOW


def test_ac10_negative_unrealized_pnl_is_recorded(conn) -> None:
    result = _evaluate(conn, (_position("AAAA", 4, "10"),), _market({"AAAA": "9.75"}))

    assert isinstance(result, Ok)
    assert _marks(conn)["AAAA"].unrealized_pnl == Money(Decimal("-1.00"))


def test_ac10_same_day_rerun_overwrites_instead_of_adding(conn) -> None:
    position = _position("AAAA", 7, "10")
    assert isinstance(_evaluate(conn, (position,), _market({"AAAA": "10.50"})), Ok)
    assert isinstance(_evaluate(conn, (position,), _market({"AAAA": "10.80"})), Ok)

    rows = list_position_marks(conn, _DAY)
    assert len(rows) == 1
    assert rows[0].mark_price == Price(Decimal("10.80"))


def test_ac10_next_day_adds_a_separate_row(conn) -> None:
    position = _position("AAAA", 7, "10")
    assert isinstance(_evaluate(conn, (position,), _market({"AAAA": "10.50"})), Ok)
    assert isinstance(_evaluate(conn, (position,), _market({"AAAA": "10.60"}), _NEXT_DAY), Ok)

    assert len(list_position_marks(conn, _DAY)) == 1
    assert len(list_position_marks(conn, "2026-09-15")) == 1


def test_ac10_mark_write_failure_is_holdings_error(conn) -> None:
    conn.execute("DROP TABLE position_marks")

    result = _evaluate(conn, (_position("AAAA", 7, "10"),), _market({"AAAA": "10.50"}))

    assert isinstance(result, Err)


# --- fills: delete on full close ------------------------------------------


def _seed_sell(conn, held_qty: int, sell_qty: int) -> Decision:
    decision = Decision(
        id="dec-sell",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="AAAA",
        action=Action.sell,
        origin=Origin.rule_exit,
        confidence=None,
        rationale="exit",
        evidence_mention_ids=(),
        llm_model=None,
        prompt_sha256=None,
        response_sha256=None,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="rule exit",
        proposed_notional=None,
        reference_price=Price(Decimal("11")),
    )
    with transaction(conn):
        portfolio_repo.upsert_position(conn, _position("AAAA", held_qty, "10"))
        repo.insert_decision(conn, decision)
        repo.insert_approval(
            conn,
            Approval(
                id="app-sell",
                decision_id="dec-sell",
                approver=Approver.system,
                approved_at=_NOW,
                expires_at=_NOW.replace(hour=21),
            ),
        )
        repo.insert_order(
            conn,
            Order(
                id="order-sell",
                decision_id="dec-sell",
                approval_id="app-sell",
                client_order_id="order_dec-sell",
                broker_order_id=None,
                mode="paper",
                side=Side.sell,
                qty=Quantity(sell_qty),
                order_type="market",
                status=OrderStatus.submitted,
                submitted_at=None,
                last_error=None,
            ),
        )
        for day in (_DAY, "2026-09-13"):
            upsert_position_mark(
                conn,
                PositionMark(
                    mark_date=day,
                    ticker="AAAA",
                    qty=Quantity(held_qty),
                    avg_cost=Price(Decimal("10")),
                    mark_price=Price(Decimal("11")),
                    unrealized_pnl=Money(Decimal(held_qty)),
                    taken_at=_NOW,
                ),
            )
    return decision


class _FilledBroker:
    def __init__(self, qty: int) -> None:
        self._qty = qty

    def get_order(self, client_order_id: str):
        return Ok(
            BrokerOrder(
                broker_order_id="b-1",
                client_order_id=client_order_id,
                status=OrderStatus.filled,
                filled_qty=Quantity(self._qty),
                filled_avg_price=Price(Decimal("11")),
                updated_at=_NOW,
            )
        )


def _settle(conn, decision: Decision, sold: int):
    return poll_and_settle(
        conn,
        _FilledBroker(sold),
        FixedClock(_NOW),
        lambda _s: None,
        "order_dec-sell",
        "order-sell",
        decision,
        Side.sell,
        ExitReason.stop_loss,
        poll_timeout_s=10,
        poll_interval_s=1,
    )


def test_ac10_full_close_deletes_that_days_mark_only(conn) -> None:
    decision = _seed_sell(conn, held_qty=7, sell_qty=7)

    assert isinstance(_settle(conn, decision, 7), Ok)

    assert portfolio_repo.get_position(conn, "AAAA") is None
    assert _marks(conn) == {}
    assert set(_marks(conn, "2026-09-13")) == {"AAAA"}


def test_ac10_partial_sell_keeps_the_mark(conn) -> None:
    decision = _seed_sell(conn, held_qty=7, sell_qty=3)

    assert isinstance(_settle(conn, decision, 3), Ok)

    position = portfolio_repo.get_position(conn, "AAAA")
    assert position is not None
    assert position.qty.shares == 4
    assert set(_marks(conn)) == {"AAAA"}
