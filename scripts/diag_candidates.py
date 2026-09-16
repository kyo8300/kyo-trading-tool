"""候補選定の診断: `trader run-cycle` が decisions=0 で終わる理由を段階ごとに表示する。

DB には書き込まない。LLM は `--llm` を付けたときだけ呼ぶ (費用がかかる)。

    set -a; source .env; set +a
    uv run python scripts/diag_candidates.py [--llm]
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from trader.analysis.analyst import propose
from trader.analysis.llm_client import AnthropicClient
from trader.analysis.prompt import build_prompt, rules_summary
from trader.config.settings import LiveSettings, PaperSettings, load_settings
from trader.domain.result import Err
from trader.engine.candidates import _MAX_CANDIDATES, _select_candidates
from trader.ledger.db import open_db
from trader.ledger.portfolio_repository import list_positions
from trader.ledger.source_repository import list_mentions
from trader.market.alpaca_data import AlpacaMarketData
from trader.market.data_provider import average_volume
from trader.rules import derive_limits, load_rules
from trader.sources.evidence import build_evidence


def main() -> int:
    settings_result = load_settings(os.environ)
    if isinstance(settings_result, Err):
        print(f"settings: {settings_result.error.message}")
        return 1
    settings = settings_result.value

    rules_result = load_rules(settings.TRADER_RULES_PATH)
    if isinstance(rules_result, Err):
        print(f"rules: {rules_result.error.message}")
        return 1
    rules = rules_result.value
    limits = derive_limits(rules)

    db_result = open_db(settings.TRADER_DB_PATH)
    if isinstance(db_result, Err):
        print(f"db: {db_result.error.message}")
        return 1
    conn = db_result.value

    now = datetime.now(UTC)
    mentions = list_mentions(conn, "serenity")
    positions = list_positions(conn)
    conn.close()
    held = {p.ticker for p in positions}
    print(f"now={now.isoformat()} mentions={len(mentions)} held={sorted(held) or '-'}")
    if mentions:
        first, last = mentions[0].posted_at, mentions[-1].posted_at
        print(f"  posted_at: {first.isoformat()} .. {last.isoformat()}")

    evidences = build_evidence(mentions, now)
    recent = [e for e in evidences if e.stats.mention_count_last_14d > 0]
    print(f"evidence tickers={len(evidences)} with mentions in last 14d={len(recent)}")

    candidates = _select_candidates(evidences, held, rules.entry.excluded_tickers)
    print(f"candidates (cap {_MAX_CANDIDATES}) = {candidates}")

    if isinstance(settings, PaperSettings):
        read_key, read_secret = settings.ALPACA_PAPER_READ_KEY, settings.ALPACA_PAPER_READ_SECRET
    else:
        assert isinstance(settings, LiveSettings)  # noqa: S101
        read_key, read_secret = settings.ALPACA_LIVE_READ_KEY, settings.ALPACA_LIVE_READ_SECRET
    market = AlpacaMarketData.create(read_key, read_secret, mode=settings.mode)

    clock = market.market_clock()
    print(f"market_clock: {clock.value if not isinstance(clock, Err) else clock.error.message}")

    priced: list[str] = []
    bars_by_ticker = {}
    for ticker in candidates:
        price = market.latest_price(ticker)
        bars = market.daily_bars(ticker)
        if isinstance(price, Err):
            print(f"  {ticker}: DROP latest_price -> {price.error.message}")
            continue
        if isinstance(bars, Err):
            print(f"  {ticker}: DROP daily_bars -> {bars.error.message}")
            continue
        bars_by_ticker[ticker] = bars.value
        priced.append(ticker)
        print(
            f"  {ticker}: ok price={price.value.amount} bars={len(bars.value)} "
            f"avg_volume={average_volume(bars.value)}"
        )
    print(f"priced candidates = {len(priced)} -> LLM {'called' if priced else 'NOT called'}")

    if "--llm" in sys.argv and priced:
        llm = AnthropicClient.create(settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MODEL)
        evidence_by_ticker = {e.ticker: e for e in evidences}
        prompt = build_prompt(
            rules_summary(rules, limits),
            tuple(evidence_by_ticker[t] for t in priced),
            bars_by_ticker,
            priced,
        )
        analysis = propose(llm, prompt, priced)  # type: ignore[arg-type]
        print(f"llm model={analysis.llm_model} skipped_reason={analysis.skipped_reason}")
        for proposal in analysis.proposals:
            print(f"  {proposal.ticker}: {proposal.action} conf={proposal.confidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
