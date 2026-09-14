"""Build per-ticker `Evidence` (mention history + excerpts) from `Mention`s
(R-4, Q4). Pure function, no I/O -- the caller supplies `mentions` and the
current time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from trader.domain.models import Evidence, Mention, MentionStats

_DEFAULT_WINDOW_DAYS = 14
_MAX_EXCERPTS = 3


def build_evidence(
    mentions: Iterable[Mention],
    now: datetime,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> tuple[Evidence, ...]:
    """Group `mentions` by ticker and derive mention history + excerpts.

    For each ticker: first/last seen, total mention count, the count in the
    most recent `window_days` days, and the count in the `window_days`
    window before that. Excerpts are the 3 most recent mentions' text,
    newest first. Returned in alphabetical ticker order.
    """
    by_ticker: dict[str, list[Mention]] = defaultdict(list)
    for mention in mentions:
        by_ticker[mention.ticker].append(mention)

    recent_cutoff = now - timedelta(days=window_days)
    prior_cutoff = now - timedelta(days=window_days * 2)

    evidences: list[Evidence] = []
    for ticker in sorted(by_ticker):
        ticker_mentions = sorted(by_ticker[ticker], key=lambda m: m.posted_at)
        recent_count = sum(1 for m in ticker_mentions if m.posted_at > recent_cutoff)
        prior_count = sum(1 for m in ticker_mentions if prior_cutoff < m.posted_at <= recent_cutoff)
        stats = MentionStats(
            ticker=ticker,
            first_seen_at=ticker_mentions[0].posted_at,
            last_seen_at=ticker_mentions[-1].posted_at,
            mention_count=len(ticker_mentions),
            mention_count_last_14d=recent_count,
            mention_count_prior_14d=prior_count,
        )
        newest_first = sorted(ticker_mentions, key=lambda m: m.posted_at, reverse=True)
        excerpts = tuple(m.text_excerpt for m in newest_first[:_MAX_EXCERPTS])
        mention_ids = tuple(m.id for m in ticker_mentions)
        evidences.append(
            Evidence(ticker=ticker, stats=stats, excerpts=excerpts, mention_ids=mention_ids)
        )

    return tuple(evidences)
