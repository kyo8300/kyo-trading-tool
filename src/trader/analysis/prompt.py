"""Prompt assembly for the LLM analyst (Q4, R-7).

Pure functions only -- no I/O, no ticket to the rules file or lock (R-11).
`rules_summary` renders a read-only text summary of a `RuleSet` /
`DerivedLimits`; it never writes the YAML file's name or path into the
prompt, so the LLM has no way to refer back to it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from trader.domain.models import Evidence
from trader.market.data_provider import Bar, average_volume
from trader.rules.schema import DerivedLimits, RuleSet

_MAX_EXCERPTS_SHOWN = 3
_MAX_BARS_SHOWN = 20

_SYSTEM_PROMPT = """You are a read-only trading analyst for a personal trading tool.

Rules you must follow:
- The trading rules described to you are fixed and read-only. You cannot change them.
- You must never propose "sell". Selling is decided entirely by the rule engine.
- Your output must be JSON only, matching exactly this schema (no prose, no markdown fence):
{
  "proposals": [
    {
      "ticker": "ABCD",
      "action": "buy" | "hold" | "skip",
      "confidence": 0.0-1.0,
      "rationale": "short explanation",
      "evidence_mention_ids": ["id1", "id2"]
    }
  ]
}
- You may only propose tickers from the candidate list given to you.
  Any other ticker is rejected."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """A fully assembled prompt: system instructions + user content."""

    system: str
    user: str


def rules_summary(rules: RuleSet, limits: DerivedLimits) -> str:
    """Render a read-only text summary of the trading rules (R-7, R-11).

    Absolute limits are already derived (`capital_usd * pct`); this text
    never references the rules file's name or path.
    """
    excluded = ", ".join(rules.entry.excluded_tickers) or "(none)"
    lines = [
        f"Capital: ${rules.capital_usd}",
        (
            f"Max notional per ticker: ${limits.max_notional_per_ticker.amount} "
            f"({rules.position.max_notional_per_ticker_pct}% of capital)"
        ),
        f"Max concurrent positions: {rules.position.max_concurrent_positions}",
        f"Max holding period: {rules.position.max_holding_days} days",
        f"Stop loss: {rules.exit.stop_loss_pct}% from entry price",
        (
            f"Partial take profit: {rules.exit.partial_take_profit_pct}% "
            f"(sell {rules.exit.partial_take_profit_fraction} of the position)"
        ),
        (
            f"Trailing stop (after partial take profit): "
            f"{rules.exit.trailing_stop_pct}% from high watermark"
        ),
        f"Excluded tickers: {excluded}",
        (
            f"Liquidity requirement: price >= ${rules.entry.min_price_usd}, "
            f"avg daily volume >= {rules.entry.min_avg_daily_volume} shares"
        ),
    ]
    return "\n".join(lines)


def _mention_section(evidence: Evidence) -> str:
    stats = evidence.stats
    delta = stats.mention_count_last_14d - stats.mention_count_prior_14d
    is_new = stats.mention_count_prior_14d == 0 and stats.mention_count_last_14d > 0
    header = (
        f"{evidence.ticker}: total mentions={stats.mention_count}, "
        f"last14d={stats.mention_count_last_14d}, prior14d={stats.mention_count_prior_14d}, "
        f"delta={delta:+d}"
    )
    if is_new:
        header += " (NEW in last 14 days)"
    excerpt_lines = [f'  - "{excerpt}"' for excerpt in evidence.excerpts[:_MAX_EXCERPTS_SHOWN]]
    return "\n".join([header, *excerpt_lines])


def _bars_section(ticker: str, bars: Sequence[Bar]) -> str:
    recent = list(bars)[-_MAX_BARS_SHOWN:]
    lines = [f"{ticker} daily bars (most recent {len(recent)}):"]
    lines.extend(
        f"  {bar.date.isoformat()},close={bar.close.amount},volume={bar.volume}" for bar in recent
    )
    lines.append(f"  avg daily volume: {average_volume(tuple(recent))}")
    return "\n".join(lines)


def build_prompt(
    rules_text: str,
    evidences: Sequence[Evidence],
    bars_by_ticker: Mapping[str, Sequence[Bar]],
    candidates: Sequence[str],
) -> Prompt:
    """Assemble the full prompt for one analysis call (Q4).

    `rules_text` is a pre-rendered read-only summary (see `rules_summary`).
    """
    sections = [
        "Trading rules (read-only, cannot be changed):",
        rules_text,
        "",
        "Candidate tickers (only these may be proposed):",
        ", ".join(candidates) if candidates else "(none)",
        "",
        "Mention evidence (recent 14-day new mentions and count changes):",
    ]
    sections.extend(_mention_section(evidence) for evidence in evidences)
    sections.append("")
    sections.append("Market data (recent daily bars and average volume):")
    sections.extend(_bars_section(ticker, bars) for ticker, bars in bars_by_ticker.items())

    user = "\n".join(sections)
    return Prompt(system=_SYSTEM_PROMPT, user=user)
