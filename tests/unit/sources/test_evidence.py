"""AC-6: per-ticker mention history (first/last seen, counts, excerpts) is
derived from `Mention`s (R-4).
"""

from __future__ import annotations

from datetime import UTC, datetime

from trader.domain.models import Mention
from trader.sources.evidence import build_evidence

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _mention(ticker: str, external_id: str, posted_at: datetime, text: str = "excerpt") -> Mention:
    return Mention(
        id=f"serenity:{external_id}:{ticker}",
        source_id="serenity",
        external_id=external_id,
        ticker=ticker,
        posted_at=posted_at,
        text_excerpt=text,
        url=None,
        raw_sha256="h",
    )


def test_build_evidence_computes_counts_and_windows() -> None:
    mentions = (
        # AAPL: 2 mentions, both within the last 14 days.
        _mention("AAPL", "1", datetime(2026, 9, 10, 10, 0, tzinfo=UTC), "aapl recent 1"),
        _mention("AAPL", "2", datetime(2026, 9, 5, 9, 0, tzinfo=UTC), "aapl recent 2"),
        # TSLA: 1 mention in the last 14 days, 1 in the 14 days before that.
        _mention("TSLA", "3", datetime(2026, 9, 5, 9, 0, tzinfo=UTC), "tsla recent"),
        _mention("TSLA", "4", datetime(2026, 8, 20, 8, 0, tzinfo=UTC), "tsla prior"),
        # GME: 1 mention far outside both windows, 1 mention in the last 14 days.
        _mention("GME", "5", datetime(2026, 7, 1, 8, 0, tzinfo=UTC), "gme old"),
        _mention("GME", "6", datetime(2026, 9, 12, 11, 0, tzinfo=UTC), "gme recent"),
    )

    evidences = build_evidence(mentions, now=_NOW, window_days=14)

    assert [e.ticker for e in evidences] == ["AAPL", "GME", "TSLA"]

    by_ticker = {e.ticker: e for e in evidences}

    aapl = by_ticker["AAPL"]
    assert aapl.stats.mention_count == 2
    assert aapl.stats.mention_count_last_14d == 2
    assert aapl.stats.mention_count_prior_14d == 0
    assert aapl.stats.first_seen_at == datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    assert aapl.stats.last_seen_at == datetime(2026, 9, 10, 10, 0, tzinfo=UTC)

    tsla = by_ticker["TSLA"]
    assert tsla.stats.mention_count == 2
    assert tsla.stats.mention_count_last_14d == 1
    assert tsla.stats.mention_count_prior_14d == 1

    gme = by_ticker["GME"]
    assert gme.stats.mention_count == 2
    assert gme.stats.mention_count_last_14d == 1
    assert gme.stats.mention_count_prior_14d == 0


def test_excerpts_are_at_most_three_and_newest_first() -> None:
    mentions = tuple(
        _mention("AAPL", str(i), datetime(2026, 9, i, 10, 0, tzinfo=UTC), f"excerpt {i}")
        for i in range(1, 6)
    )

    evidences = build_evidence(mentions, now=_NOW, window_days=14)
    assert len(evidences) == 1
    excerpts = evidences[0].excerpts
    assert excerpts == ("excerpt 5", "excerpt 4", "excerpt 3")


def test_mention_ids_include_every_mention_for_the_ticker() -> None:
    mentions = (
        _mention("AAPL", "1", datetime(2026, 9, 1, tzinfo=UTC)),
        _mention("AAPL", "2", datetime(2026, 9, 2, tzinfo=UTC)),
    )
    evidences = build_evidence(mentions, now=_NOW, window_days=14)
    assert len(evidences[0].mention_ids) == 2


def test_no_mentions_returns_empty_tuple() -> None:
    assert build_evidence((), now=_NOW) == ()


def test_mention_exactly_at_the_window_boundary_counts_as_prior_not_recent() -> None:
    # Pin the implementation's boundary decision: `window_days` ago exactly
    # (== recent_cutoff) is excluded from the "recent" bucket (strict `>`)
    # and falls into the "prior" bucket instead.
    from datetime import timedelta

    exactly_14_days_ago = _NOW - timedelta(days=14)
    mentions = (_mention("AAPL", "1", exactly_14_days_ago),)

    evidences = build_evidence(mentions, now=_NOW, window_days=14)

    assert evidences[0].stats.mention_count_last_14d == 0
    assert evidences[0].stats.mention_count_prior_14d == 1
