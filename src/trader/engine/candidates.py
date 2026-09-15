"""Evaluate new-buy candidates each cycle (spec データフロー step 4, R-5〜R-7).

Candidates are tickers mentioned in the last 14 days that are not already
held and not excluded, capped at 20. Each is priced (dropped on a market
data error); if none remain, the LLM is never called. Otherwise the LLM is
asked once for the whole batch, and every proposal (or, on an LLM/validation
failure, every candidate as a `skip`) is turned into a `Decision` and
recorded. Only `action="buy"` proposals go through `rules.entry_checks`;
`hold`/`skip` are recorded as rejected without a rule check. Sells are never
produced here (R-13: sells are rule-driven, from `engine.holdings` only).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from trader.analysis.analyst import LlmClient, propose
from trader.analysis.output_schema import LlmProposal
from trader.analysis.prompt import build_prompt, rules_summary
from trader.domain.clock import Clock
from trader.domain.models import Action, Decision, Evidence, Mention, Origin, RuleCheck
from trader.domain.money import Price
from trader.domain.result import Err, Ok, Result
from trader.ledger.repository import insert_decision
from trader.market.data_provider import Bar, MarketDataProvider, average_volume
from trader.rules.entry_checks import check_entry
from trader.rules.loss_limits import LossLimitBreach
from trader.rules.schema import DerivedLimits, RuleSet
from trader.sources.evidence import build_evidence

_MAX_CANDIDATES = 20


@dataclass(frozen=True, slots=True)
class CandidatesError:
    """A human-readable candidate-evaluation error (N-6)."""

    message: str


@dataclass(frozen=True, slots=True)
class CandidatesOutcome:
    """Every decision (passed/rejected/skip) produced for this cycle's candidates."""

    decisions: tuple[Decision, ...]


def _select_candidates(
    evidences: tuple[Evidence, ...],
    held_tickers: Collection[str],
    excluded_tickers: Collection[str],
) -> list[str]:
    eligible = [
        evidence
        for evidence in evidences
        if evidence.stats.mention_count_last_14d > 0
        and evidence.ticker not in held_tickers
        and evidence.ticker not in excluded_tickers
    ]
    # Most-discussed first so the cap keeps the strongest signals, not the
    # alphabetically-earliest tickers; ticker breaks ties deterministically.
    ranked = sorted(eligible, key=lambda e: (-e.stats.mention_count_last_14d, e.ticker))
    return [evidence.ticker for evidence in ranked[:_MAX_CANDIDATES]]


def _skip_decision(
    ticker: str,
    cycle_id: str,
    now: datetime,
    mode: str,
    rule_set_sha256: str,
    reason: str,
    llm_model: str | None,
    prompt_sha256: str | None,
    response_sha256: str | None,
    evidence_mention_ids: tuple[str, ...],
) -> Decision:
    return Decision(
        id=f"dec_{uuid.uuid4().hex}",
        cycle_id=cycle_id,
        decided_at=now,
        mode=mode,
        ticker=ticker,
        action=Action.skip,
        origin=Origin.llm,
        confidence=None,
        rationale=reason,
        evidence_mention_ids=evidence_mention_ids,
        llm_model=llm_model,
        prompt_sha256=prompt_sha256 or None,
        response_sha256=response_sha256 or None,
        rule_set_sha256=rule_set_sha256,
        rule_check=RuleCheck.rejected,
        rule_check_reason=reason,
        proposed_notional=None,
        reference_price=None,
    )


def _non_buy_decision(
    proposal: LlmProposal,
    cycle_id: str,
    now: datetime,
    mode: str,
    rule_set_sha256: str,
    llm_model: str,
    prompt_sha256: str | None,
    response_sha256: str | None,
) -> Decision:
    return Decision(
        id=f"dec_{uuid.uuid4().hex}",
        cycle_id=cycle_id,
        decided_at=now,
        mode=mode,
        ticker=proposal.ticker,
        action=Action(proposal.action),
        origin=Origin.llm,
        confidence=proposal.confidence,
        rationale=proposal.rationale,
        evidence_mention_ids=proposal.evidence_mention_ids,
        llm_model=llm_model,
        prompt_sha256=prompt_sha256,
        response_sha256=response_sha256,
        rule_set_sha256=rule_set_sha256,
        rule_check=RuleCheck.rejected,
        rule_check_reason=f"llm {proposal.action}",
        proposed_notional=None,
        reference_price=None,
    )


def _buy_decision(
    proposal: LlmProposal,
    cycle_id: str,
    now: datetime,
    mode: str,
    rule_set_sha256: str,
    llm_model: str,
    prompt_sha256: str | None,
    response_sha256: str | None,
    rules: RuleSet,
    limits: DerivedLimits,
    held_tickers: Collection[str],
    market_open: bool,
    latest_price: Price,
    avg_volume: int,
    loss_breach: LossLimitBreach | None,
) -> Decision:
    entry_result = check_entry(
        proposal.ticker,
        rules,
        limits,
        held_tickers,
        market_open,
        latest_price,
        avg_volume,
        loss_breach,
    )
    if isinstance(entry_result, Err):
        return Decision(
            id=f"dec_{uuid.uuid4().hex}",
            cycle_id=cycle_id,
            decided_at=now,
            mode=mode,
            ticker=proposal.ticker,
            action=Action.buy,
            origin=Origin.llm,
            confidence=proposal.confidence,
            rationale=proposal.rationale,
            evidence_mention_ids=proposal.evidence_mention_ids,
            llm_model=llm_model,
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
            rule_set_sha256=rule_set_sha256,
            rule_check=RuleCheck.rejected,
            rule_check_reason=entry_result.error.reason,
            proposed_notional=None,
            reference_price=None,
        )

    plan = entry_result.value
    return Decision(
        id=f"dec_{uuid.uuid4().hex}",
        cycle_id=cycle_id,
        decided_at=now,
        mode=mode,
        ticker=proposal.ticker,
        action=Action.buy,
        origin=Origin.llm,
        confidence=proposal.confidence,
        rationale=proposal.rationale,
        evidence_mention_ids=proposal.evidence_mention_ids,
        llm_model=llm_model,
        prompt_sha256=prompt_sha256,
        response_sha256=response_sha256,
        rule_set_sha256=rule_set_sha256,
        rule_check=RuleCheck.passed,
        rule_check_reason=(
            f"notional {plan.notional.amount} <= limit {plan.limit.amount} "
            f"({rules.position.max_notional_per_ticker_pct}% of {rules.capital_usd})"
        ),
        proposed_notional=plan.notional,
        reference_price=plan.reference_price,
    )


def _price_candidates(
    market: MarketDataProvider, candidate_tickers: list[str]
) -> tuple[dict[str, Price], dict[str, tuple[Bar, ...]], dict[str, int], list[str]]:
    priced: dict[str, Price] = {}
    bars_by_ticker: dict[str, tuple[Bar, ...]] = {}
    avg_volumes: dict[str, int] = {}
    final_candidates: list[str] = []
    for ticker in candidate_tickers:
        price_result = market.latest_price(ticker)
        bars_result = market.daily_bars(ticker)
        if isinstance(price_result, Err) or isinstance(bars_result, Err):
            continue
        priced[ticker] = price_result.value
        bars_by_ticker[ticker] = bars_result.value
        avg_volumes[ticker] = average_volume(bars_result.value)
        final_candidates.append(ticker)
    return priced, bars_by_ticker, avg_volumes, final_candidates


def evaluate_candidates(
    conn: sqlite3.Connection,
    mentions: tuple[Mention, ...],
    held_tickers: Collection[str],
    market: MarketDataProvider,
    llm: LlmClient | None,
    rules: RuleSet,
    limits: DerivedLimits,
    clock: Clock,
    cycle_id: str,
    rule_set_sha256: str,
    mode: str,
    loss_breach: LossLimitBreach | None,
    market_open: bool,
) -> Result[CandidatesOutcome, CandidatesError]:
    """Turn recently-mentioned tickers into rule-checked buy `Decision`s."""
    now = clock.now()
    evidence_by_ticker = {e.ticker: e for e in build_evidence(mentions, now)}
    candidate_tickers = _select_candidates(
        tuple(evidence_by_ticker.values()), held_tickers, rules.entry.excluded_tickers
    )

    priced, bars_by_ticker, avg_volumes, final_candidates = _price_candidates(
        market, candidate_tickers
    )
    if not final_candidates:
        return Ok(CandidatesOutcome(decisions=()))

    decisions: list[Decision] = []

    def _record_skip_all(
        reason: str, llm_model: str | None, p_sha: str | None, r_sha: str | None
    ) -> None:
        for ticker in final_candidates:
            evidence = evidence_by_ticker.get(ticker)
            decisions.append(
                _skip_decision(
                    ticker,
                    cycle_id,
                    now,
                    mode,
                    rule_set_sha256,
                    reason,
                    llm_model,
                    p_sha,
                    r_sha,
                    evidence.mention_ids if evidence is not None else (),
                )
            )

    if llm is None:
        _record_skip_all("no llm client configured", None, None, None)
    else:
        rules_text = rules_summary(rules, limits)
        evidences = tuple(evidence_by_ticker[t] for t in final_candidates)
        prompt = build_prompt(rules_text, evidences, bars_by_ticker, final_candidates)
        analysis = propose(llm, prompt, final_candidates)

        if analysis.skipped_reason is not None:
            _record_skip_all(
                analysis.skipped_reason,
                analysis.llm_model,
                analysis.prompt_sha256 or None,
                analysis.response_sha256 or None,
            )
        else:
            # `held_tickers` must grow as earlier proposals in this batch
            # pass, so a later proposal in the same batch sees the tickers
            # that would already be held once earlier buys execute (caps
            # `max_concurrent_positions` and de-dupes a ticker proposed
            # twice within one batch).
            batch_held_tickers = set(held_tickers)
            for proposal in analysis.proposals:
                if proposal.action != "buy":
                    decisions.append(
                        _non_buy_decision(
                            proposal,
                            cycle_id,
                            now,
                            mode,
                            rule_set_sha256,
                            analysis.llm_model,
                            analysis.prompt_sha256 or None,
                            analysis.response_sha256 or None,
                        )
                    )
                    continue
                buy_decision = _buy_decision(
                    proposal,
                    cycle_id,
                    now,
                    mode,
                    rule_set_sha256,
                    analysis.llm_model,
                    analysis.prompt_sha256 or None,
                    analysis.response_sha256 or None,
                    rules,
                    limits,
                    batch_held_tickers,
                    market_open,
                    priced[proposal.ticker],
                    avg_volumes[proposal.ticker],
                    loss_breach,
                )
                if buy_decision.rule_check is RuleCheck.passed:
                    batch_held_tickers.add(proposal.ticker)
                decisions.append(buy_decision)

    for decision in decisions:
        insert_result = insert_decision(conn, decision)
        if isinstance(insert_result, Err):
            return Err(CandidatesError(insert_result.error.message))

    return Ok(CandidatesOutcome(decisions=tuple(decisions)))
