"""T-15/AC-28: end-to-end paper cycle through the real CLI + a real (tmp)
SQLite ledger.

Unlike `tests/unit/engine/test_cycle.py` (which builds the DB and ingests
mentions directly through repository helpers), this test drives `trader
rules approve` and `trader ingest` through `CliRunner` -- exercising the
same commands kyo would type by hand (spec AC-32) -- and only calls
`engine.cycle.run_cycle` directly because `trader run-cycle` itself refuses
to run under `TRADER_ENV=test` (N-9). `trader report` and `trader status`
are also driven through the CLI so their output is asserted as kyo would
see it.

Scenario (plan T-15 / spec AC-28):
1. ingest serenity fixtures via CLI
2. run_cycle: AAPL priced at $10, market open, fake LLM proposes "buy" ->
   floor(75/10) = 7 shares fill
3. run_cycle with AAPL at $8 (-20%, past the -15% stop loss) -> rule-driven
   sell, LLM never called, 7 shares fill
4. `trader report` shows the closed trade (-14.00 realized P&L, stop_loss)
5. `trader status` shows no open positions and an unhalted engine
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from trader.broker.broker import (
    BrokerAccount,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from trader.cli import app as cli_app
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.models import OrderStatus
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Ok, Result
from trader.engine.cycle import CycleDeps, run_cycle
from trader.ledger.db import open_db
from trader.market.data_provider import Bar, MarketClock
from trader.market.fake_data import FakeMarketData
from trader.rules.lock import verify_lock
from trader.rules.schema import derive_limits, load_rules

_FIXTURES_RULES = Path(__file__).parent.parent / "fixtures" / "rules" / "valid.yaml"
_FIXTURES_SERENITY = Path(__file__).parent.parent / "fixtures" / "serenity"
_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_OPEN_CLOCK = MarketClock(is_open=True, next_open=_NOW, next_close=_NOW + timedelta(hours=1))


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


@dataclass
class _FakeLlm:
    """Minimal `LlmClient`: always proposes buying `ticker`, and records how
    many times it was asked (used to prove the stop-loss cycle never calls
    the LLM -- sells are rule-driven, R-13)."""

    ticker: str
    model: str = "fake-model"
    call_count: int = 0

    def complete(self, prompt: object) -> Result[object, object]:
        from trader.analysis.llm_client import LlmRaw

        self.call_count += 1
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
    `fill_price` on the very first `get_order` poll."""

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


def _setup_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Copy the rules/serenity fixtures into a tmp workspace, mirroring
    AC-32's manual-copy step, and return (rules_path, db_path, data_dir)."""
    rules_path = tmp_path / "trading-rules.yaml"
    rules_path.write_text(_FIXTURES_RULES.read_text(encoding="utf-8"), encoding="utf-8")

    data_dir = tmp_path / "serenity"
    data_dir.mkdir()
    (data_dir / "tweets.json").write_text(
        (_FIXTURES_SERENITY / "tweets_valid.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (data_dir / "ticker_stats.txt").write_text(
        (_FIXTURES_SERENITY / "ticker_stats.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )

    db_path = tmp_path / "trader.sqlite3"
    return rules_path, db_path, data_dir


def test_paper_cycle_end_to_end_ingest_buy_stop_loss_report(tmp_path: Path) -> None:
    rules_path, db_path, data_dir = _setup_workspace(tmp_path)
    runner = CliRunner()

    # 1. `trader rules approve` writes the lock and records the rule set (AC-9).
    approve_result = runner.invoke(
        cli_app, ["rules", "approve", "--rules", str(rules_path), "--db", str(db_path)]
    )
    assert approve_result.exit_code == 0, approve_result.output

    lock_path = rules_path.parent / "trading-rules.lock"
    verify_result = verify_lock(rules_path, lock_path)
    assert isinstance(verify_result, Ok), verify_result

    # 2. `trader ingest` loads the serenity fixtures via the CLI.
    ingest_result = runner.invoke(
        cli_app,
        [
            "ingest",
            "--source",
            "serenity",
            "--data-dir",
            str(data_dir),
            "--db",
            str(db_path),
        ],
    )
    assert ingest_result.exit_code == 0, ingest_result.output

    conn = sqlite3.connect(db_path)
    try:
        mention_count = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    finally:
        conn.close()
    assert mention_count > 0

    # 3. `run_cycle` directly (`trader run-cycle` refuses under TRADER_ENV=test, N-9):
    #    AAPL priced at $10, market open, fake LLM proposes "buy" -> floor(75/10) = 7 shares.
    rule_set = load_rules(rules_path).value
    limits = derive_limits(rule_set)
    rule_set_sha256 = verify_result.value.sha256

    db_result = open_db(db_path)
    assert isinstance(db_result, Ok)
    conn = db_result.value

    market1 = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))}, bars={"AAPL": _bars("AAPL")}, clock=_OPEN_CLOCK
    )
    broker1 = _ImmediateFillBroker(fill_price=Price(Decimal("10")))
    llm = _FakeLlm(ticker="AAPL")

    deps1 = CycleDeps(
        mode=TradingMode.paper,
        rules=rule_set,
        limits=limits,
        rule_set_sha256=rule_set_sha256,
        conn=conn,
        broker=broker1,
        market=market1,
        llm=llm,
        clock=FixedClock(_NOW),
        capital=Money(rule_set.capital_usd),
        sleep=lambda _s: None,
        lock_path=None,
    )
    result1 = run_cycle(deps1)
    assert isinstance(result1, Ok), result1
    outcome1 = result1.value
    assert outcome1.outcome == "ok"
    assert outcome1.orders == 1
    assert outcome1.fills == 1
    assert len(broker1.submitted) == 1
    assert broker1.submitted[0].qty.shares == 7

    positions_row = conn.execute("SELECT qty, ticker FROM positions").fetchall()
    assert len(positions_row) == 1
    assert positions_row[0][1] == "AAPL"

    llm_calls_after_buy = llm.call_count

    # 4. Price crashes to $8 (-20%, past the -15% stop loss) -> rule-driven sell,
    #    LLM must not be called.
    market2 = market1.with_price("AAPL", Price(Decimal("8")))
    broker2 = _ImmediateFillBroker(fill_price=Price(Decimal("8")))
    deps2 = replace(deps1, broker=broker2, market=market2)
    result2 = run_cycle(deps2)
    assert isinstance(result2, Ok), result2
    outcome2 = result2.value
    assert outcome2.outcome == "ok"
    assert outcome2.orders == 1
    assert outcome2.fills == 1
    assert len(broker2.submitted) == 1
    assert broker2.submitted[0].qty.shares == 7
    assert llm.call_count == llm_calls_after_buy, (
        "sells are rule-driven; the LLM must not be consulted for the stop-loss exit (R-13)"
    )

    positions_after = conn.execute("SELECT * FROM positions").fetchall()
    assert positions_after == []

    trade_rows = conn.execute(
        "SELECT ticker, realized_pnl, exit_reason, entry_decision_id FROM trades"
    ).fetchall()
    assert len(trade_rows) == 1
    ticker, realized_pnl, exit_reason, entry_decision_id = trade_rows[0]
    assert ticker == "AAPL"
    assert realized_pnl == "-14.00"
    assert exit_reason == "stop_loss"

    buy_decision_row = conn.execute(
        "SELECT id FROM decisions WHERE ticker = 'AAPL' AND action = 'buy'"
    ).fetchone()
    assert buy_decision_row is not None
    assert entry_decision_id == buy_decision_row[0]

    conn.close()

    # 5. `trader report` shows the closed trade with its rationale, exit reason and P&L.
    report_result = runner.invoke(
        cli_app, ["report", "--db", str(db_path), "--rules", str(rules_path)]
    )
    assert report_result.exit_code == 0, report_result.output
    assert "AAPL" in report_result.output
    assert "-14.00" in report_result.output
    assert "stop_loss" in report_result.output
    assert "rising mentions" in report_result.output

    # 6. `trader status` shows no open positions and an unhalted engine.
    status_result = runner.invoke(cli_app, ["status", "--db", str(db_path)])
    assert status_result.exit_code == 0, status_result.output
    assert "positions: none" in status_result.output
    assert "HALTED" not in status_result.output
