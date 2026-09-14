"""T-13 / AC-8, AC-18, AC-28 (precursor): `engine.cycle.run_cycle` end to end
with `FakeBroker`/`FakeMarketData`/a fake LLM/`FixedClock` and a real (tmp)
SQLite ledger.

Five scenarios (the remaining cases in plan.md's T-13 test plan -- LLM
failure -> skip decisions, and a `market_clock` failure leaving
`equity_snapshots` untouched -- are left for the tester to add):

1. A normal cycle: one mentioned, priced candidate + an LLM "buy" fills
   completely.
2. A halted engine evaluates holdings but never submits an order.
3. A closed market keeps every decision but submits no orders.
4. A second `run_cycle` cannot acquire an already-held lock file.
5. A held position that breaches its stop loss is sold and closes a `trade`
   with the expected realized P&L and exit reason.
"""

from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
from trader.domain.models import ExitReason, OrderStatus, Position
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok, Result
from trader.engine import kill_switch
from trader.engine.cycle import CycleDeps, run_cycle
from trader.ledger.db import open_db
from trader.ledger.portfolio_repository import (
    insert_rule_set,
    list_positions,
    list_trades,
    upsert_position,
)
from trader.ledger.repository import list_decisions
from trader.ledger.source_repository import insert_mentions_many, upsert_source
from trader.market.data_provider import Bar, MarketClock
from trader.market.fake_data import FakeMarketData
from trader.rules.lock import sha256_of_file
from trader.rules.loss_limits import LossLimitBreach
from trader.rules.schema import derive_limits, load_rules
from trader.sources.serenity.adapter import SerenityAdapter

_FIXTURES_RULES = Path(__file__).parent.parent.parent / "fixtures" / "rules" / "valid.yaml"
_FIXTURES_SERENITY = Path(__file__).parent.parent.parent / "fixtures" / "serenity"
_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_OPEN_CLOCK = MarketClock(is_open=True, next_open=_NOW, next_close=_NOW + timedelta(hours=1))
_CLOSED_CLOCK = MarketClock(is_open=False, next_open=_NOW + timedelta(hours=12), next_close=_NOW)


def _bars(ticker: str) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            ticker=ticker,
            date=date(2026, 9, 1) + timedelta(days=i),
            open=Price(Decimal("10")),
            high=Price(Decimal("10.5")),
            low=Price(Decimal("9.5")),
            close=Price(Decimal("10")),
            volume=300_000,
        )
        for i in range(20)
    )


def _db(tmp_path: Path):
    conn = open_db(tmp_path / "trader.sqlite3").value
    sha256 = sha256_of_file(_FIXTURES_RULES).value
    rule_set = load_rules(_FIXTURES_RULES).value
    insert_result = insert_rule_set(
        conn,
        sha256=sha256,
        approved_by=rule_set.approved_by,
        approved_at=rule_set.approved_at,
        capital_usd=Money(rule_set.capital_usd),
        content_yaml=_FIXTURES_RULES.read_text(encoding="utf-8"),
    )
    assert isinstance(insert_result, Ok)
    return conn, rule_set, derive_limits(rule_set), sha256


def _ingest_mentions(conn, tmp_path: Path) -> None:
    data_dir = tmp_path / "serenity"
    data_dir.mkdir()
    (data_dir / "tweets.json").write_text(
        (_FIXTURES_SERENITY / "tweets_valid.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (data_dir / "ticker_stats.txt").write_text(
        (_FIXTURES_SERENITY / "ticker_stats.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    mentions = SerenityAdapter().load(data_dir).value
    upsert_source(conn, "serenity", "serenity", _NOW, "sha", len(mentions))
    insert_mentions_many(conn, mentions)


@dataclass
class _FakeLlm:
    """Minimal `LlmClient`: always proposes buying `ticker`."""

    ticker: str
    model: str = "fake-model"

    def complete(self, prompt: object) -> Result[object, object]:
        from trader.analysis.llm_client import LlmRaw

        text = json.dumps(
            {
                "proposals": [
                    {
                        "ticker": self.ticker,
                        "action": "buy",
                        "confidence": "0.8",
                        "rationale": "rising mentions",
                        "evidence_mention_ids": [],
                    }
                ]
            }
        )
        return Ok(
            LlmRaw(text=text, model=self.model, prompt_sha256="p" * 64, response_sha256="r" * 64)
        )


@dataclass
class _ImmediateFillBroker:
    """A `Broker` test double that fully fills any submitted order at
    `fill_price` on the very first `get_order` poll -- avoids needing to
    predict `client_order_id` (which embeds a randomly generated decision id)
    ahead of time, unlike `FakeBroker.with_fill_plan`."""

    fill_price: Price
    cash: Money = field(default_factory=lambda: Money(Decimal("500.00")))
    submitted: list[OrderRequest] = field(default_factory=list)
    _orders: dict[str, OrderRequest] = field(default_factory=dict)

    def submit_market_order(self, req: OrderRequest) -> Result[BrokerOrder, BrokerError]:
        self.submitted.append(req)
        self._orders[req.client_order_id] = req
        return Ok(
            BrokerOrder(
                broker_order_id=f"b_{req.client_order_id}",
                client_order_id=req.client_order_id,
                status=OrderStatus.submitted,
                filled_qty=Quantity(0),
                filled_avg_price=None,
                updated_at=_NOW,
            )
        )

    def get_order(self, client_order_id: str) -> Result[BrokerOrder, BrokerError]:
        req = self._orders[client_order_id]
        return Ok(
            BrokerOrder(
                broker_order_id=f"b_{client_order_id}",
                client_order_id=client_order_id,
                status=OrderStatus.filled,
                filled_qty=req.qty,
                filled_avg_price=self.fill_price,
                updated_at=_NOW,
            )
        )

    def cancel_all_open(self) -> Result[int, BrokerError]:
        return Ok(0)

    def positions(self) -> Result[tuple[BrokerPosition, ...], BrokerError]:
        return Ok(())

    def account(self) -> Result[BrokerAccount, BrokerError]:
        return Ok(BrokerAccount(cash=self.cash, equity=self.cash))


def test_normal_cycle_buys_and_fills(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))}, bars={"AAPL": _bars("AAPL")}, clock=_OPEN_CLOCK
    )
    broker = _ImmediateFillBroker(fill_price=Price(Decimal("10")))
    llm = _FakeLlm(ticker="AAPL")

    deps = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker,
        market=market,
        llm=llm,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )

    result = run_cycle(deps)

    assert isinstance(result, Ok)
    outcome = result.value
    assert outcome.outcome == "ok"
    assert outcome.orders == 1
    assert outcome.fills == 1
    assert len(broker.submitted) == 1
    assert broker.submitted[0].qty.shares == 7

    positions = list_positions(conn)
    assert len(positions) == 1
    assert positions[0].ticker == "AAPL"
    assert positions[0].qty.shares == 7
    assert positions[0].avg_cost.amount == Decimal("10.0000")

    decisions = list_decisions(conn, outcome.cycle_id)
    buy_decisions = [d for d in decisions if d.ticker == "AAPL"]
    assert len(buy_decisions) == 1
    assert buy_decisions[0].rule_check.value == "passed"
    conn.close()


def test_halted_cycle_evaluates_holdings_but_never_submits(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _db(tmp_path)

    breach = LossLimitBreach(
        kind="daily",
        observed=Money(Decimal("-20.00")),
        limit=Money(Decimal("15.00")),
        reason="daily P&L -20.00 <= -15.00",
    )
    broker_for_halt = FakeBroker()
    trigger_result = kill_switch.trigger(breach, broker_for_halt, conn, FixedClock(_NOW))
    assert isinstance(trigger_result, Ok)

    market = FakeMarketData(prices={}, bars={}, clock=_OPEN_CLOCK)
    llm = _FakeLlm(ticker="AAPL")
    broker = FakeBroker()

    deps = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker,
        market=market,
        llm=llm,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )

    result = run_cycle(deps)

    assert isinstance(result, Ok)
    assert result.value.outcome == "halted"
    assert broker.submit_call_count == 0
    conn.close()


def test_market_closed_cycle_records_decisions_without_orders(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))}, bars={"AAPL": _bars("AAPL")}, clock=_CLOSED_CLOCK
    )
    broker = FakeBroker().with_account(
        BrokerAccount(cash=Money(Decimal("500.00")), equity=Money(Decimal("500.00")))
    )
    llm = _FakeLlm(ticker="AAPL")

    deps = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker,
        market=market,
        llm=llm,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )

    result = run_cycle(deps)

    assert isinstance(result, Ok)
    outcome = result.value
    assert outcome.outcome == "ok"
    assert outcome.orders == 0
    assert outcome.fills == 0
    assert broker.submit_call_count == 0

    decisions = list_decisions(conn, outcome.cycle_id)
    assert len(decisions) > 0
    conn.close()


def test_second_lock_acquisition_fails_while_first_is_held(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _db(tmp_path)
    lock_path = tmp_path / "run-cycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    market = FakeMarketData(prices={}, bars={}, clock=_OPEN_CLOCK)
    broker = FakeBroker()

    deps = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker,
        market=market,
        llm=None,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=lock_path,
    )

    try:
        result = run_cycle(deps)
        assert isinstance(result, Err)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        conn.close()


def test_stop_loss_sell_closes_trade_with_expected_pnl(tmp_path: Path) -> None:
    conn, rule_set, limits, sha256 = _db(tmp_path)

    opened_at = _NOW - timedelta(days=10)
    position = Position(
        ticker="ZZZZ",
        qty=Quantity(7),
        avg_cost=Price(Decimal("10.00")),
        opened_at=opened_at,
        high_watermark=Price(Decimal("10.00")),
        partial_tp_done=False,
    )
    upsert_result = upsert_position(conn, position)
    assert isinstance(upsert_result, Ok)

    market = FakeMarketData(prices={"ZZZZ": Price(Decimal("8.00"))}, bars={}, clock=_OPEN_CLOCK)
    broker = _ImmediateFillBroker(fill_price=Price(Decimal("8.00")))

    deps = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker,
        market=market,
        llm=None,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )

    result = run_cycle(deps)

    assert isinstance(result, Ok)
    outcome = result.value
    assert outcome.outcome == "ok"
    assert outcome.orders == 1
    assert outcome.fills == 1
    assert len(broker.submitted) == 1
    assert broker.submitted[0].qty.shares == 7

    positions = list_positions(conn)
    assert positions == ()

    trades = list_trades(conn)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.ticker == "ZZZZ"
    assert trade.realized_pnl.amount == Decimal("-14.00")
    assert trade.exit_reason == ExitReason.stop_loss
    conn.close()
