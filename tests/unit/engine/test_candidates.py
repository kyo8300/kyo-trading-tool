"""R-12 review finding (candidates.py:150): `evaluate_candidates` never
updates the `held_tickers` it passes into `rules.entry_checks.check_entry`
as buy decisions within the same batch pass, so a single LLM response
proposing more buys than `max_concurrent_positions` allows -- or proposing
the same ticker twice -- is not capped/deduplicated within the cycle.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.analysis.llm_client import LlmRaw
from trader.domain.clock import FixedClock
from trader.domain.models import Mention
from trader.domain.money import Money, Price
from trader.domain.result import Ok, Result
from trader.engine.candidates import evaluate_candidates
from trader.ledger.db import open_db
from trader.ledger.portfolio_repository import insert_cycle, insert_rule_set
from trader.ledger.source_repository import insert_mentions_many, list_mentions, upsert_source
from trader.market.data_provider import Bar
from trader.market.fake_data import FakeMarketData
from trader.rules.lock import sha256_of_file
from trader.rules.schema import derive_limits, load_rules

_FIXTURES_RULES = Path(__file__).parent.parent.parent / "fixtures" / "rules" / "valid.yaml"
_NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


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
    insert_cycle(conn, cycle_id="cycle-1", started_at=_NOW, mode="paper")
    return conn, rule_set, derive_limits(rule_set), sha256


def _mention(ticker: str) -> Mention:
    return Mention(
        id=f"m_{uuid.uuid4().hex}",
        source_id="serenity",
        external_id=f"ext_{ticker}",
        ticker=ticker,
        posted_at=_NOW - timedelta(days=1),
        text_excerpt=f"{ticker} is mooning",
        url=None,
        raw_sha256="s" * 64,
    )


def _insert_mentions(conn, tickers: list[str]) -> None:
    upsert_source(conn, "serenity", "serenity", _NOW, "sha", len(tickers))
    insert_mentions_many(conn, tuple(_mention(t) for t in tickers))


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


def _market(tickers: list[str]) -> FakeMarketData:
    return FakeMarketData(
        prices={t: Price(Decimal("10")) for t in tickers},
        bars={t: _bars(t) for t in tickers},
        clock=None,  # type: ignore[arg-type]  # market_clock() is never called here
    )


@dataclass
class _MultiBuyLlm:
    """A fake `LlmClient` that proposes "buy" for every ticker in `tickers`
    (possibly with duplicates), all in one `complete` call."""

    tickers: list[str]
    model: str = "fake-model"

    def complete(self, prompt: object) -> Result[LlmRaw, object]:
        proposals = [
            {
                "ticker": ticker,
                "action": "buy",
                "confidence": "0.8",
                "rationale": "rising mentions",
                "evidence_mention_ids": [],
            }
            for ticker in self.tickers
        ]
        text = json.dumps({"proposals": proposals})
        return Ok(
            LlmRaw(text=text, model=self.model, prompt_sha256="p" * 64, response_sha256="r" * 64)
        )


def test_a_single_batch_of_buys_over_max_concurrent_positions_caps_passed_decisions_at_the_limit(
    tmp_path: Path,
) -> None:
    """`rules/trading-rules.yaml` fixture has `max_concurrent_positions: 5`.
    Six distinct, otherwise-valid buy proposals in one cycle must yield at
    most 5 `passed` decisions -- the 6th must be `rejected` for a
    concurrent-positions reason, not `passed` -- because `held_tickers`
    (starting empty here) must grow as earlier proposals in the same batch
    pass, not stay fixed for the whole batch."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    tickers = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"]
    _insert_mentions(conn, tickers)

    mentions = list_mentions(conn, "serenity")
    result = evaluate_candidates(
        conn,
        mentions,
        set(),
        _market(tickers),
        _MultiBuyLlm(tickers=tickers),
        rule_set,
        limits,
        FixedClock(_NOW),
        "cycle-1",
        sha256,
        "paper",
        None,
        True,
    )
    assert isinstance(result, Ok)
    decisions = result.value.decisions
    passed = [d for d in decisions if d.rule_check.value == "passed"]
    rejected_concurrent = [
        d
        for d in decisions
        if d.rule_check.value == "rejected" and "concurrent" in d.rule_check_reason.lower()
    ]
    assert len(passed) == rule_set.position.max_concurrent_positions
    assert len(rejected_concurrent) == 1
    conn.close()


def test_the_same_ticker_proposed_twice_in_one_batch_only_produces_one_passed_buy(
    tmp_path: Path,
) -> None:
    """A duplicate ticker in one LLM response must not become two `passed`
    buy decisions for the same ticker -- the second occurrence must be
    rejected as already (about to be) held, since `held_tickers` must grow
    as earlier proposals in the same batch pass."""
    conn, rule_set, limits, sha256 = _db(tmp_path)
    tickers = ["AAAA"]
    _insert_mentions(conn, tickers)

    mentions = list_mentions(conn, "serenity")
    result = evaluate_candidates(
        conn,
        mentions,
        set(),
        _market(tickers),
        _MultiBuyLlm(tickers=["AAAA", "AAAA"]),
        rule_set,
        limits,
        FixedClock(_NOW),
        "cycle-1",
        sha256,
        "paper",
        None,
        True,
    )
    assert isinstance(result, Ok)
    decisions = [d for d in result.value.decisions if d.ticker == "AAAA"]
    passed = [d for d in decisions if d.rule_check.value == "passed"]
    assert len(passed) == 1
    conn.close()
