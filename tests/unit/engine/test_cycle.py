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
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trader.analysis.llm_client import LlmError
from trader.broker.broker import (
    BrokerAccount,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from trader.broker.fake_broker import FakeBroker
from trader.cli import app as cli_app
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.models import ExitReason, OrderStatus, Position
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok, Result
from trader.engine import kill_switch
from trader.engine.cycle import CycleDeps, run_cycle
from trader.ledger.db import open_db
from trader.ledger.portfolio_repository import (
    get_engine_state,
    insert_rule_set,
    list_equity_snapshots,
    list_positions,
    list_trades,
    upsert_equity_snapshot,
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


def test_stop_loss_sell_at_a_four_decimal_price_sells_the_full_held_quantity(
    tmp_path: Path,
) -> None:
    """R-12/R-13 review finding (approval.py:104, `_order_qty`): a stop-loss
    `Decision`'s `proposed_notional` is `qty * price` quantized to the cent
    (`engine.holdings._sell_qty`/`notional`). For a 4-decimal price
    (7 shares @ 8.3333 = 58.3331 -> Money("58.33")),
    `floor(58.33 / 8.3333) == 6`, one share short of the 7 actually held --
    the sell order must still be sized to close the entire position (7
    shares), leaving no residual position and exactly one closed trade."""
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

    market = FakeMarketData(prices={"ZZZZ": Price(Decimal("8.3333"))}, bars={}, clock=_OPEN_CLOCK)
    broker = _ImmediateFillBroker(fill_price=Price(Decimal("8.3333")))

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
    assert len(broker.submitted) == 1
    assert broker.submitted[0].qty.shares == 7

    positions = list_positions(conn)
    assert positions == ()

    trades = list_trades(conn)
    assert len(trades) == 1
    conn.close()


@dataclass
class _ErrLlm:
    """A `LlmClient` whose `complete` always fails (T-13 test plan: LLM Err)."""

    model: str = "fake-model"

    def complete(self, prompt: object) -> Result[object, object]:
        return Err(LlmError("simulated llm outage", retryable=True))


def test_llm_error_leaves_skip_decisions_for_every_candidate(tmp_path: Path) -> None:
    """AC-8/T-13: an LLM failure must not stop the cycle -- every priced
    candidate is recorded as a `skip` decision, no orders are placed, and
    the cycle itself still ends `outcome=ok` (spec エラー処理: 'LLM 失敗 →
    その銘柄は skip として記録し続行')."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    market = FakeMarketData(
        prices={
            "AAPL": Price(Decimal("10")),
            "TSLA": Price(Decimal("20")),
            "GME": Price(Decimal("15")),
        },
        bars={"AAPL": _bars("AAPL"), "TSLA": _bars("TSLA"), "GME": _bars("GME")},
        clock=_OPEN_CLOCK,
    )
    broker = FakeBroker().with_account(
        BrokerAccount(cash=Money(Decimal("500.00")), equity=Money(Decimal("500.00")))
    )
    llm = _ErrLlm()

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
    assert broker.submit_call_count == 0

    decisions = list_decisions(conn, outcome.cycle_id)
    skip_decisions = [d for d in decisions if d.action.value == "skip"]
    assert {d.ticker for d in skip_decisions} == {"AAPL", "TSLA", "GME"}
    assert len(skip_decisions) == 3
    for decision in skip_decisions:
        assert decision.rule_check.value == "rejected"
    conn.close()


def test_market_clock_error_skips_equity_snapshot_and_orders(tmp_path: Path) -> None:
    """AC-8/T-13: a `market_clock()` failure must not update `equity_snapshots`
    and must not place any orders (spec エラー処理: マーケットデータ失敗 →
    新規買いをしない, equity_snapshots はそのサイクルでは更新しない)."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    market = FakeMarketData(prices={}, bars={}, clock=_OPEN_CLOCK).failing({"market_clock"})
    broker = FakeBroker()
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
    assert outcome.outcome == "market_unavailable"
    assert outcome.orders == 0
    assert outcome.fills == 0
    assert broker.submit_call_count == 0
    assert list_equity_snapshots(conn) == ()
    conn.close()


def test_a_held_positions_unpriceable_ticker_skips_the_cycle_instead_of_halting(
    tmp_path: Path,
) -> None:
    """R-19/エラー処理 review finding (cycle.py:249, holdings.py:80): when
    `market.latest_price` fails for a *held* ticker (market open, so
    `market_clock()` itself succeeds), `evaluate_holdings` records the
    failure in `market_errors` but still returns `Ok` -- so `run_cycle`
    currently ignores it and proceeds to price the (incomplete)
    `positions_value`, upsert `equity_snapshots`, and run
    `loss_limits`/the kill switch against that incomplete equity, and it
    also still evaluates new-buy candidates.

    Per spec エラー処理 (market data failure -> cannot judge sells or value
    the portfolio, so no new buys; holdings are re-evaluated next cycle;
    equity_snapshots is not updated this cycle) -- a held position whose
    price cannot be fetched must make the *whole* cycle `market_unavailable`: no
    `equity_snapshots` write, no kill switch (even though the incomplete
    equity here would fabricate a huge apparent drawdown against a
    generously high prior peak), and no orders.
    """
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    snapshot_result = upsert_equity_snapshot(
        conn,
        "2026-09-13",
        "paper",
        Money(Decimal("500.00")),
        Money(Decimal("0")),
        Money(Decimal("500.00")),
        Decimal("0"),
        _NOW - timedelta(days=1),
    )
    assert isinstance(snapshot_result, Ok)

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

    # ZZZZ has no configured price -> latest_price is Err (holdings.py
    # skips it into market_errors). AAPL is otherwise a perfectly good buy
    # candidate, to prove new buys are blocked too.
    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))}, bars={"AAPL": _bars("AAPL")}, clock=_OPEN_CLOCK
    )
    broker = FakeBroker().with_account(
        BrokerAccount(cash=Money(Decimal("10.00")), equity=Money(Decimal("10.00")))
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
    assert outcome.outcome == "market_unavailable"
    assert outcome.orders == 0
    assert outcome.fills == 0
    assert broker.submit_call_count == 0
    assert not kill_switch.is_halted(conn)
    snapshots = list_equity_snapshots(conn)
    assert len(snapshots) == 1
    assert snapshots[0].snapshot_date == "2026-09-13"

    decisions = list_decisions(conn, outcome.cycle_id)
    assert not any(d.ticker == "AAPL" and d.action.value == "buy" for d in decisions)
    conn.close()


def test_priced_holdings_rule_exit_decision_survives_a_sibling_holdings_market_error(
    tmp_path: Path,
) -> None:
    """R-13/R-19 エラー処理 (cycle.py comment "Holding decisions already
    recorded above ... are kept"): with two held positions where only one
    ticker's price is unavailable, `evaluate_holdings` still walks every
    position in order, so a stop-loss `rule_exit` sell decision on the
    *other*, correctly-priced ticker is inserted before the market error is
    discovered. Fixing this implementation decision: the cycle still ends
    `market_unavailable` (no equity_snapshots write, no new buys), but the
    already-recorded sell decision on the priced ticker is not rolled back."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    opened_at = _NOW - timedelta(days=10)
    # DOWN triggers stop_loss_pct=-15% at price 8.00 (avg_cost 10.00).
    down_position = Position(
        ticker="DOWN",
        qty=Quantity(7),
        avg_cost=Price(Decimal("10.00")),
        opened_at=opened_at,
        high_watermark=Price(Decimal("10.00")),
        partial_tp_done=False,
    )
    # UNPRICED has no configured price -> market_errors.
    unpriced_position = Position(
        ticker="UNPRICED",
        qty=Quantity(3),
        avg_cost=Price(Decimal("10.00")),
        opened_at=opened_at,
        high_watermark=Price(Decimal("10.00")),
        partial_tp_done=False,
    )
    assert isinstance(upsert_position(conn, down_position), Ok)
    assert isinstance(upsert_position(conn, unpriced_position), Ok)

    market = FakeMarketData(prices={"DOWN": Price(Decimal("8.00"))}, bars={}, clock=_OPEN_CLOCK)
    broker = FakeBroker().with_account(
        BrokerAccount(cash=Money(Decimal("10.00")), equity=Money(Decimal("10.00")))
    )

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
    assert outcome.outcome == "market_unavailable"
    assert outcome.orders == 0
    assert list_equity_snapshots(conn) == ()

    decisions = list_decisions(conn, outcome.cycle_id)
    down_decisions = [d for d in decisions if d.ticker == "DOWN"]
    assert len(down_decisions) == 1
    assert down_decisions[0].action.value == "sell"
    assert down_decisions[0].origin.value == "rule_exit"
    conn.close()


def test_multi_cycle_partial_take_profit_then_trailing_stop(tmp_path: Path) -> None:
    """R-21/AC-20/T-13 gap: a position that is partially taken-profit in one
    cycle and trailing-stopped out in a later cycle must close a single
    `trade` whose `entry_decision_id` is the original buy decision,
    `exit_decision_ids` lists both exit decisions in order, and
    `realized_pnl` is the sum across both sells (spec R-21, plan T-11 test
    plan: '部分利確 → 残り → トレーリング売りで exit_decision_ids が 2 件')."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    # Cycle 1: LLM buy -> capital $500, 15% limit = $75, floor(75/10) = 7 shares @ 10.
    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))}, bars={"AAPL": _bars("AAPL")}, clock=_OPEN_CLOCK
    )
    broker1 = _ImmediateFillBroker(fill_price=Price(Decimal("10")))
    llm = _FakeLlm(ticker="AAPL")

    deps1 = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker1,
        market=market,
        llm=llm,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )
    result1 = run_cycle(deps1)
    assert isinstance(result1, Ok)
    outcome1 = result1.value
    assert outcome1.orders == 1
    assert outcome1.fills == 1

    positions_after_1 = list_positions(conn)
    assert len(positions_after_1) == 1
    assert positions_after_1[0].qty.shares == 7

    buy_decisions = [
        d
        for d in list_decisions(conn, outcome1.cycle_id)
        if d.ticker == "AAPL" and d.action.value == "buy"
    ]
    assert len(buy_decisions) == 1
    entry_decision_id = buy_decisions[0].id

    # Cycle 2: price +30% (10 -> 13) triggers partial take-profit,
    # fraction 0.5 -> floor(7 * 0.5) = 3 shares sold, 4 remain.
    market2 = market.with_price("AAPL", Price(Decimal("13")))
    broker2 = _ImmediateFillBroker(fill_price=Price(Decimal("13")))
    deps2 = replace(deps1, broker=broker2, market=market2)
    result2 = run_cycle(deps2)
    assert isinstance(result2, Ok)
    outcome2 = result2.value
    assert outcome2.orders == 1
    assert outcome2.fills == 1

    positions_after_2 = list_positions(conn)
    assert len(positions_after_2) == 1
    assert positions_after_2[0].qty.shares == 4
    assert positions_after_2[0].partial_tp_done is True
    assert list_trades(conn) == ()

    partial_tp_decisions = [
        d
        for d in list_decisions(conn, outcome2.cycle_id)
        if d.ticker == "AAPL" and d.action.value == "sell"
    ]
    assert len(partial_tp_decisions) == 1
    partial_tp_decision_id = partial_tp_decisions[0].id

    # Cycle 3: high_watermark 13 * (1 - 20%) = 10.4 -> trailing stop sells
    # the remaining 4 shares and closes the trade.
    market3 = market2.with_price("AAPL", Price(Decimal("10.4")))
    broker3 = _ImmediateFillBroker(fill_price=Price(Decimal("10.4")))
    deps3 = replace(deps2, broker=broker3, market=market3)
    result3 = run_cycle(deps3)
    assert isinstance(result3, Ok)
    outcome3 = result3.value
    assert outcome3.orders == 1
    assert outcome3.fills == 1

    assert list_positions(conn) == ()
    trades = list_trades(conn)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.ticker == "AAPL"
    assert trade.exit_reason == ExitReason.trailing_stop
    assert trade.entry_decision_id == entry_decision_id, (
        "trade.entry_decision_id must reference the original cycle-1 buy decision, "
        "not the closing sell decision (fills.poll_and_settle always calls "
        "trade_closer.apply_fill with entry_decision_id=None)"
    )
    assert len(trade.exit_decision_ids) == 2, (
        "trade.exit_decision_ids must accumulate both the partial-take-profit and "
        "trailing-stop exit decisions across cycles, not just the closing one"
    )
    if len(trade.exit_decision_ids) == 2:
        assert trade.exit_decision_ids[0] == partial_tp_decision_id
    assert trade.realized_pnl.amount == Decimal("10.60"), (
        "realized_pnl must be the sum of both sells "
        "(3 * (13 - 10) + 4 * (10.4 - 10) = 9.00 + 1.60 = 10.60), not just the "
        "closing fill's realized amount (fills.poll_and_settle always calls "
        "trade_closer.apply_fill with realized_so_far=0)"
    )
    conn.close()


def test_stop_loss_trade_entry_decision_references_original_buy_decision(
    tmp_path: Path,
) -> None:
    """R-21/T-13: even a single-sell stop-loss exit in a later cycle must
    close a trade whose `entry_decision_id` is the original LLM buy
    decision (so `report` can show 'what was it bought for', spec R-21),
    and that decision's `rationale` must be the LLM's own rationale."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))}, bars={"AAPL": _bars("AAPL")}, clock=_OPEN_CLOCK
    )
    broker1 = _ImmediateFillBroker(fill_price=Price(Decimal("10")))
    llm = _FakeLlm(ticker="AAPL")

    deps1 = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=sha256,
        conn=conn,
        broker=broker1,
        market=market,
        llm=llm,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )
    result1 = run_cycle(deps1)
    assert isinstance(result1, Ok)
    outcome1 = result1.value

    buy_decisions = [
        d
        for d in list_decisions(conn, outcome1.cycle_id)
        if d.ticker == "AAPL" and d.action.value == "buy"
    ]
    assert len(buy_decisions) == 1
    buy_decision = buy_decisions[0]
    assert buy_decision.rationale == "rising mentions"

    # Cycle 2: price crashes to 8 (-20%, past the -15% stop loss).
    market2 = market.with_price("AAPL", Price(Decimal("8")))
    broker2 = _ImmediateFillBroker(fill_price=Price(Decimal("8")))
    deps2 = replace(deps1, broker=broker2, market=market2)
    result2 = run_cycle(deps2)
    assert isinstance(result2, Ok)

    trades = list_trades(conn)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == ExitReason.stop_loss
    assert trade.entry_decision_id == buy_decision.id, (
        "the closing sell's decision id must not silently become the "
        "trade's entry_decision_id -- it must reference the LLM buy decision "
        f"whose rationale was {buy_decision.rationale!r}"
    )
    conn.close()


def test_drawdown_breach_halts_engine_and_blocks_next_cycle(tmp_path: Path) -> None:
    """AC-18/T-13: peak_equity 600 -> current equity 500 is a 16.7% drawdown,
    past the 15% limit. The kill switch must fire (`outcome=halted`,
    `is_halted` persisted), and a later cycle with a pending LLM buy must
    still submit nothing."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    snapshot_result = upsert_equity_snapshot(
        conn,
        "2026-09-13",
        "paper",
        Money(Decimal("600.00")),
        Money(Decimal("0")),
        Money(Decimal("600.00")),
        Decimal("0"),
        _NOW - timedelta(days=1),
    )
    assert isinstance(snapshot_result, Ok)

    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))}, bars={"AAPL": _bars("AAPL")}, clock=_OPEN_CLOCK
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
    assert outcome.outcome == "halted"
    assert outcome.orders == 0
    assert kill_switch.is_halted(conn)

    result2 = run_cycle(replace(deps, broker=FakeBroker()))
    assert isinstance(result2, Ok)
    outcome2 = result2.value
    assert outcome2.outcome == "halted"
    assert outcome2.orders == 0
    conn.close()


def test_max_concurrent_positions_rejects_new_buy(tmp_path: Path) -> None:
    """AC-11/T-13: with `max_concurrent_positions` (5) already held, a new
    LLM buy must be rejected by the rule engine and never become an order."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    _ingest_mentions(conn, tmp_path)

    for i in range(5):
        position = Position(
            ticker=f"HLD{i}",
            qty=Quantity(1),
            avg_cost=Price(Decimal("10.00")),
            opened_at=_NOW - timedelta(days=1),
            high_watermark=Price(Decimal("10.00")),
            partial_tp_done=False,
        )
        upsert_result = upsert_position(conn, position)
        assert isinstance(upsert_result, Ok)

    held_prices = {f"HLD{i}": Price(Decimal("10.00")) for i in range(5)}
    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10")), **held_prices},
        bars={"AAPL": _bars("AAPL")},
        clock=_OPEN_CLOCK,
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
    assert outcome.orders == 0
    assert broker.submit_call_count == 0

    decisions = list_decisions(conn, outcome.cycle_id)
    aapl_decisions = [d for d in decisions if d.ticker == "AAPL"]
    assert len(aapl_decisions) == 1
    assert aapl_decisions[0].rule_check.value == "rejected"
    assert "max_concurrent_positions" in aapl_decisions[0].rule_check_reason
    conn.close()


def test_cycles_table_records_one_row_and_updates_last_cycle_id(tmp_path: Path) -> None:
    """R-8/T-13: one `run_cycle` call writes exactly one `cycles` row with
    both a start and finish timestamp, and `engine_state.last_cycle_id` is
    updated to point at it."""
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

    rows = conn.execute("SELECT id, started_at, finished_at FROM cycles").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == outcome.cycle_id
    assert rows[0]["started_at"] is not None
    assert rows[0]["finished_at"] is not None

    assert get_engine_state(conn, "last_cycle_id") == outcome.cycle_id
    conn.close()


def test_run_cycle_writes_are_visible_through_a_fresh_connection(tmp_path: Path) -> None:
    """T-15: `_finish` commits the connection so that a second, independent
    connection opened after `run_cycle` returns (as `trader report`/`trader
    status`/the next `run-cycle` process would do) observes every write this
    cycle made: positions, fills, orders.status, trades, cycles, and
    engine_state.last_cycle_id (spec 'SQLite の同時実行': WAL readers only
    see committed data)."""
    db_path = tmp_path / "trader.sqlite3"
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
    assert isinstance(upsert_position(conn, position), Ok)

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
    conn.close()

    # Open a brand new connection, as another `trader` process would.
    fresh = open_db(db_path).value
    try:
        assert list_positions(fresh) == ()

        trades = list_trades(fresh)
        assert len(trades) == 1
        assert trades[0].ticker == "ZZZZ"
        assert trades[0].realized_pnl.amount == Decimal("-14.00")

        order_rows = fresh.execute("SELECT status FROM orders").fetchall()
        assert len(order_rows) == 1
        assert order_rows[0]["status"] == "filled"

        fill_rows = fresh.execute("SELECT qty, price FROM fills").fetchall()
        assert len(fill_rows) == 1

        cycle_rows = fresh.execute(
            "SELECT id, finished_at, outcome FROM cycles WHERE id = ?", (outcome.cycle_id,)
        ).fetchall()
        assert len(cycle_rows) == 1
        assert cycle_rows[0]["finished_at"] is not None
        assert cycle_rows[0]["outcome"] == "ok"

        assert get_engine_state(fresh, "last_cycle_id") == outcome.cycle_id
    finally:
        fresh.close()


class _FailOnCyclesUpdate(sqlite3.Connection):
    """A `sqlite3.Connection` whose `UPDATE cycles ...` statement (the one
    `finish_cycle` issues) always raises, simulating a real DB exception at
    the point `_finish` closes out the cycle row. Every other statement
    behaves normally, so the cycle's actual writes (decisions, equity
    snapshot, engine_state) still happen for real, exercising `finish_cycle`'s
    own `except sqlite3.Error` -> `Err(LedgerError(...))` conversion."""

    def execute(self, sql: str, *parameters: object) -> sqlite3.Cursor:
        if sql.strip().startswith("UPDATE cycles"):
            raise sqlite3.OperationalError("simulated disk I/O error")
        return super().execute(sql, *parameters)


def test_db_exception_during_finish_yields_err_and_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-15 (Prove-It): a genuine DB exception raised while closing out the
    cycle (`finish_cycle`'s `UPDATE cycles`) must not be swallowed or crash
    the caller uncaught. `finish_cycle` catches `sqlite3.Error` and returns
    `Err(LedgerError)`; `_finish` must propagate that as `Err(CycleError)`
    from `run_cycle` (N-6: no bare `except`, no silent `Ok`) while still
    committing the writes that already happened this cycle (equity snapshot,
    decisions -- spec 'DB 書き込み失敗' error handling)."""
    db_path = tmp_path / "trader.sqlite3"
    real_connect = sqlite3.connect

    def _connect(path: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs.pop("factory", None)
        return real_connect(path, *args, factory=_FailOnCyclesUpdate, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", _connect)

    conn, rule_set, limits, sha256 = _db(tmp_path)

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
        lock_path=None,
    )

    result = run_cycle(deps)

    assert isinstance(result, Err), (
        "a DB exception while finishing the cycle must surface as Err, not be "
        "swallowed into a false Ok"
    )
    assert "could not finish cycle" in result.error.message

    # The equity snapshot upserted earlier in this same cycle must still be
    # committed even though the final `UPDATE cycles` failed.
    snapshots = list_equity_snapshots(conn)
    assert len(snapshots) == 1
    conn.close()

    # A fresh connection (real sqlite3.connect, not the failing factory)
    # confirms the partial writes were actually committed, not lost.
    monkeypatch.undo()
    fresh = open_db(db_path).value
    try:
        assert len(list_equity_snapshots(fresh)) == 1
    finally:
        fresh.close()


def test_run_cycle_cli_refuses_to_run_under_trader_env_test() -> None:
    """N-9/T-13: `trader run-cycle` must refuse to run for real under
    `TRADER_ENV=test` (tests call `engine.cycle.run_cycle` directly instead)."""
    runner = CliRunner()

    result = runner.invoke(cli_app, ["run-cycle"])

    assert result.exit_code == 1
    assert "TRADER_ENV=test" in result.output
