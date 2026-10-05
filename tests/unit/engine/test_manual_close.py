"""T-6 / AC-11, AC-12, AC-13: `engine.manual_close.close_position`.

Test names are limited to the `test_paper_happy_path_*`, `test_refuses_*` and
`test_live_*` prefixes because the AC verify commands select with `-k`
substring matches (plan risk 2).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.broker.broker import (
    BrokerAccount,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from trader.broker.fake_broker import FakeBroker
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.models import (
    Action,
    Approver,
    ExitReason,
    OrderStatus,
    Origin,
    Position,
    PositionMark,
    RuleCheck,
)
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok, Result
from trader.engine import kill_switch
from trader.engine.approval import record_human_approval
from trader.engine.manual_close import CloseDeps, close_position
from trader.ledger.benchmark_repository import list_position_marks, upsert_position_mark
from trader.ledger.db import open_db
from trader.ledger.portfolio_repository import (
    insert_rule_set,
    list_positions,
    list_trades,
    upsert_position,
)
from trader.ledger.repository import list_decisions, list_orders
from trader.market.data_provider import MarketClock
from trader.market.fake_data import FakeMarketData
from trader.rules.lock import sha256_of_file
from trader.rules.loss_limits import LossLimitBreach
from trader.rules.schema import load_rules

_RULES = Path(__file__).parent.parent.parent / "fixtures" / "rules" / "valid.yaml"
_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_OPEN = MarketClock(is_open=True, next_open=_NOW, next_close=_NOW + timedelta(hours=1))
_CLOSED = MarketClock(is_open=False, next_open=_NOW + timedelta(hours=12), next_close=_NOW)
_AVG_COST = Decimal("10.00")
_FILL = Decimal("12.00")
_QTY = 7


@dataclass
class _FillBroker:
    """Fills every submitted order fully on the first poll."""

    fill_price: Price
    submitted: list[OrderRequest] = field(default_factory=list)
    _orders: dict[str, OrderRequest] = field(default_factory=dict)

    def submit_market_order(self, req: OrderRequest) -> Result[BrokerOrder, BrokerError]:
        self.submitted.append(req)
        self._orders[req.client_order_id] = req
        return Ok(self._order(req, OrderStatus.submitted))

    def get_order(self, client_order_id: str) -> Result[BrokerOrder, BrokerError]:
        return Ok(self._order(self._orders[client_order_id], OrderStatus.filled))

    def _order(self, req: OrderRequest, status: OrderStatus) -> BrokerOrder:
        filled = status is OrderStatus.filled
        return BrokerOrder(
            broker_order_id=f"b_{req.client_order_id}",
            client_order_id=req.client_order_id,
            status=status,
            filled_qty=req.qty if filled else Quantity(0),
            filled_avg_price=self.fill_price if filled else None,
            updated_at=_NOW,
        )

    def cancel_all_open(self) -> Result[int, BrokerError]:
        return Ok(0)

    def positions(self) -> Result[tuple[BrokerPosition, ...], BrokerError]:
        return Ok(())

    def account(self) -> Result[BrokerAccount, BrokerError]:
        cash = Money(Decimal("500.00"))
        return Ok(BrokerAccount(cash=cash, equity=cash))


def _setup(tmp_path: Path, *, holding: bool = True) -> tuple[sqlite3.Connection, str, object]:
    conn = open_db(tmp_path / "t.sqlite3").value
    sha = sha256_of_file(_RULES).value
    rules = load_rules(_RULES).value
    assert isinstance(
        insert_rule_set(
            conn,
            sha256=sha,
            approved_by=rules.approved_by,
            approved_at=rules.approved_at,
            capital_usd=Money(rules.capital_usd),
            content_yaml=_RULES.read_text(encoding="utf-8"),
        ),
        Ok,
    )
    if holding:
        position = Position(
            ticker="ZZZZ",
            qty=Quantity(_QTY),
            avg_cost=Price(_AVG_COST),
            opened_at=_NOW - timedelta(days=3),
            high_watermark=Price(_AVG_COST),
            partial_tp_done=False,
        )
        assert isinstance(upsert_position(conn, position), Ok)
        mark = PositionMark(
            mark_date="2026-09-14",
            ticker="ZZZZ",
            qty=Quantity(_QTY),
            avg_cost=Price(_AVG_COST),
            mark_price=Price(_FILL),
            unrealized_pnl=Money(Decimal("14.00")),
            taken_at=_NOW,
        )
        assert isinstance(upsert_position_mark(conn, mark), Ok)
    return conn, sha, rules


def _deps(
    conn: sqlite3.Connection,
    sha: str,
    rules: object,
    broker: object,
    mode: TradingMode = TradingMode.paper,
    market: FakeMarketData | None = None,
) -> CloseDeps:
    market = market or FakeMarketData(prices={"ZZZZ": Price(_FILL)}, bars={}, clock=_OPEN)
    return CloseDeps(
        mode=mode,
        rules=rules,  # type: ignore[arg-type]
        rule_set_sha256=sha,
        conn=conn,
        broker=broker,  # type: ignore[arg-type]
        market=market,
        clock=FixedClock(_NOW),
        sleep=lambda _s: None,
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


def test_paper_happy_path_closes_position_through_full_chain(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))

    result = close_position(_deps(conn, sha, rules, broker), "ZZZZ", "take profit")

    assert isinstance(result, Ok)
    out = result.value
    assert out.order_status is OrderStatus.filled
    assert out.filled_qty == _QTY
    assert out.slot_freed is True
    assert out.cycle_id.startswith("close_")
    assert len(broker.submitted) == 1

    decisions = list_decisions(conn)
    assert len(decisions) == 1
    assert decisions[0].id == out.decision_id
    assert decisions[0].origin is Origin.manual
    assert decisions[0].action is Action.sell
    assert decisions[0].rule_check is RuleCheck.passed

    approvals = conn.execute("SELECT decision_id, approver FROM approvals").fetchall()
    assert [(r[0], r[1]) for r in approvals] == [(out.decision_id, Approver.system.value)]

    orders = list_orders(conn)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.filled

    trades = list_trades(conn)
    assert len(trades) == 1
    assert trades[0].exit_reason is ExitReason.manual
    assert trades[0].realized_pnl.amount == (_FILL - _AVG_COST) * _QTY
    assert out.trade is not None
    assert out.trade.exit_reason is ExitReason.manual

    assert list_positions(conn) == ()
    assert list_position_marks(conn, "2026-09-14") == ()

    cycles = conn.execute("SELECT id, outcome FROM cycles").fetchall()
    assert len(cycles) == 1
    assert cycles[0][0] == out.cycle_id
    assert cycles[0][1] is not None
    conn.close()


def _assert_nothing_recorded(conn: sqlite3.Connection) -> None:
    assert _count(conn, "decisions") == 0
    assert _count(conn, "cycles") == 0
    assert _count(conn, "orders") == 0
    assert _count(conn, "approvals") == 0


def _assert_clean_message(message: str) -> None:
    assert message
    assert not re.search(r"(^|\s)/\w", message)
    assert "sk-" not in message
    assert "PK" not in message


def test_refuses_when_ticker_not_held(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path, holding=False)
    broker = _FillBroker(Price(_FILL))

    result = close_position(_deps(conn, sha, rules, broker), "ZZZZ", None)

    assert isinstance(result, Err)
    assert result.error.kind == "precondition"
    _assert_clean_message(result.error.message)
    _assert_nothing_recorded(conn)
    assert broker.submitted == []
    conn.close()


def test_refuses_when_engine_halted(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    breach = LossLimitBreach(
        kind="daily",
        observed=Money(Decimal("-20.00")),
        limit=Money(Decimal("15.00")),
        reason="daily P&L -20.00 <= -15.00",
    )
    assert isinstance(kill_switch.trigger(breach, FakeBroker(), conn, FixedClock(_NOW)), Ok)
    broker = _FillBroker(Price(_FILL))

    result = close_position(_deps(conn, sha, rules, broker), "ZZZZ", None)

    assert isinstance(result, Err)
    assert result.error.kind == "precondition"
    _assert_clean_message(result.error.message)
    _assert_nothing_recorded(conn)
    assert broker.submitted == []
    conn.close()


def test_refuses_when_market_is_closed(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))
    market = FakeMarketData(prices={"ZZZZ": Price(_FILL)}, bars={}, clock=_CLOSED)

    result = close_position(_deps(conn, sha, rules, broker, market=market), "ZZZZ", None)

    assert isinstance(result, Err)
    assert result.error.kind == "precondition"
    _assert_clean_message(result.error.message)
    _assert_nothing_recorded(conn)
    assert broker.submitted == []
    conn.close()


def test_refuses_when_price_lookup_fails(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))
    market = FakeMarketData(prices={}, bars={}, clock=_OPEN).failing(["latest_price"])

    result = close_position(_deps(conn, sha, rules, broker, market=market), "ZZZZ", None)

    assert isinstance(result, Err)
    assert result.error.kind == "precondition"
    _assert_clean_message(result.error.message)
    _assert_nothing_recorded(conn)
    assert broker.submitted == []
    conn.close()


def test_live_without_approval_does_not_submit_and_names_decision(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))

    result = close_position(_deps(conn, sha, rules, broker, TradingMode.live), "ZZZZ", None)

    assert isinstance(result, Err)
    assert result.error.kind == "approval_required"
    decisions = list_decisions(conn)
    assert len(decisions) == 1
    assert decisions[0].id in result.error.message
    assert "trader approve" in result.error.message
    assert broker.submitted == []
    assert _count(conn, "orders") == 0
    assert len(list_positions(conn)) == 1
    conn.close()


def test_live_rerun_same_day_reuses_the_decision(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))
    deps = _deps(conn, sha, rules, broker, TradingMode.live)

    first = close_position(deps, "ZZZZ", None)
    second = close_position(deps, "ZZZZ", None)

    assert isinstance(first, Err)
    assert isinstance(second, Err)
    assert len(list_decisions(conn)) == 1
    decision_id = list_decisions(conn)[0].id
    assert decision_id in first.error.message
    assert decision_id in second.error.message
    assert broker.submitted == []
    conn.close()


def test_live_with_valid_kyo_approval_places_the_order(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))
    deps = _deps(conn, sha, rules, broker, TradingMode.live)
    assert isinstance(close_position(deps, "ZZZZ", None), Err)
    decision_id = list_decisions(conn)[0].id
    approved = record_human_approval(conn, decision_id, FixedClock(_NOW), _OPEN)
    assert isinstance(approved, Ok)
    conn.commit()

    result = close_position(deps, "ZZZZ", None)

    assert isinstance(result, Ok)
    assert result.value.decision_id == decision_id
    assert result.value.order_status is OrderStatus.filled
    assert len(broker.submitted) == 1
    assert len(list_decisions(conn)) == 1
    assert list_positions(conn) == ()
    trades = list_trades(conn)
    assert len(trades) == 1
    assert trades[0].exit_reason is ExitReason.manual
    conn.close()


def test_live_with_expired_approval_does_not_submit(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))
    deps = _deps(conn, sha, rules, broker, TradingMode.live)
    assert isinstance(close_position(deps, "ZZZZ", None), Err)
    decision_id = list_decisions(conn)[0].id
    expired = MarketClock(
        is_open=True, next_open=_NOW - timedelta(hours=3), next_close=_NOW - timedelta(hours=1)
    )
    assert isinstance(record_human_approval(conn, decision_id, FixedClock(_NOW), expired), Ok)
    conn.commit()

    result = close_position(deps, "ZZZZ", None)

    assert isinstance(result, Err)
    assert result.error.kind == "approval_required"
    assert broker.submitted == []
    assert _count(conn, "orders") == 0
    assert len(list_positions(conn)) == 1
    conn.close()


@dataclass
class _PartialBroker(_FillBroker):
    """Reports a partial fill of 3 shares forever."""

    def get_order(self, client_order_id: str) -> Result[BrokerOrder, BrokerError]:
        req = self._orders[client_order_id]
        return Ok(
            BrokerOrder(
                broker_order_id=f"b_{client_order_id}",
                client_order_id=client_order_id,
                status=OrderStatus.partially_filled,
                filled_qty=Quantity(3),
                filled_avg_price=self.fill_price,
                updated_at=_NOW,
            )
            if req.qty.shares > 3
            else self._order(req, OrderStatus.filled)
        )


def test_paper_happy_path_partial_fill_reports_summed_fill_qty(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _PartialBroker(Price(_FILL))

    result = close_position(_deps(conn, sha, rules, broker), "ZZZZ", None)

    assert isinstance(result, Ok)
    fills = conn.execute("SELECT qty FROM fills").fetchall()
    assert sum(int(r[0]) for r in fills) == result.value.filled_qty
    assert result.value.filled_qty == 3
    assert result.value.filled_qty != _QTY
    conn.close()


def test_live_rerun_with_changed_position_qty_creates_a_new_decision(tmp_path: Path) -> None:
    conn, sha, rules = _setup(tmp_path)
    broker = _FillBroker(Price(_FILL))
    deps = _deps(conn, sha, rules, broker, TradingMode.live)
    first = close_position(deps, "ZZZZ", None)
    assert isinstance(first, Err)
    smaller = Position(
        ticker="ZZZZ",
        qty=Quantity(_QTY - 2),
        avg_cost=Price(_AVG_COST),
        opened_at=_NOW - timedelta(days=3),
        high_watermark=Price(_AVG_COST),
        partial_tp_done=False,
    )
    assert isinstance(upsert_position(conn, smaller), Ok)

    second = close_position(deps, "ZZZZ", None)

    assert isinstance(second, Err)
    decisions = list_decisions(conn)
    assert len(decisions) == 2
    new = next(d for d in decisions if d.proposed_notional == Money(_FILL * (_QTY - 2)))
    assert new.id in second.error.message
    assert new.id not in first.error.message
    assert broker.submitted == []
    conn.close()


def _live_rerun_reuses_decision_at(tmp_path: Path, price: str, shares: int) -> None:
    conn, sha, rules = _setup(tmp_path)
    held = Position(
        ticker="ZZZZ",
        qty=Quantity(shares),
        avg_cost=Price(_AVG_COST),
        opened_at=_NOW - timedelta(days=3),
        high_watermark=Price(_AVG_COST),
        partial_tp_done=False,
    )
    assert isinstance(upsert_position(conn, held), Ok)
    market = FakeMarketData(prices={"ZZZZ": Price(Decimal(price))}, bars={}, clock=_OPEN)
    broker = _FillBroker(Price(Decimal(price)))
    deps = _deps(conn, sha, rules, broker, TradingMode.live, market)

    first = close_position(deps, "ZZZZ", None)
    assert isinstance(first, Err)
    decision_id = list_decisions(conn)[0].id
    approved = record_human_approval(conn, decision_id, FixedClock(_NOW), _OPEN)
    assert isinstance(approved, Ok)
    conn.commit()
    second = close_position(deps, "ZZZZ", None)

    assert isinstance(second, Ok)
    assert len(list_decisions(conn)) == 1
    assert len(broker.submitted) == 1
    assert broker.submitted[0].qty == Quantity(shares)
    conn.close()


def test_live_subcent_price_123_455_x3_reuses_approved_decision_and_places_order(
    tmp_path: Path,
) -> None:
    _live_rerun_reuses_decision_at(tmp_path, "123.455", 3)


def test_live_subcent_price_50_0001_x1_reuses_approved_decision_and_places_order(
    tmp_path: Path,
) -> None:
    _live_rerun_reuses_decision_at(tmp_path, "50.0001", 1)
