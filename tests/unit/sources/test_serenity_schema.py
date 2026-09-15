"""AC-4: boundary coverage for `serenity/schema.py` -- `$TICKER` extraction,
whitelist parsing, and the `created_at`/`timestamp` fallback (R-1, R-2).
"""

from __future__ import annotations

from datetime import UTC

import pytest
from pydantic import ValidationError

from trader.sources.serenity.schema import Tweet, extract_tickers, parse_ticker_whitelist

_WHITELIST = frozenset({"AAPL", "TSLA"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$aapl is down", ()),  # lowercase is not matched by the extraction regex
        ("$AAPL.", ("AAPL",)),
        ("($AAPL)", ("AAPL",)),
        ("$AAPL,", ("AAPL",)),
        ("$AAPLX is a fake six-letter ticker", ()),  # too long, not in whitelist either
        ("$GME is not whitelisted here", ()),  # well-formed but outside the whitelist
    ],
)
def test_extract_tickers_boundary_cases(text: str, expected: tuple[str, ...]) -> None:
    assert extract_tickers(text, _WHITELIST) == expected


def test_extract_tickers_dedupes_within_one_text() -> None:
    assert extract_tickers("$AAPL great, $AAPL again", _WHITELIST) == ("AAPL",)


def test_extract_tickers_returns_multiple_distinct_tickers() -> None:
    assert extract_tickers("$AAPL and $TSLA both up", _WHITELIST) == ("AAPL", "TSLA")


def test_parse_ticker_whitelist_ignores_comments_blank_lines_and_bad_tokens() -> None:
    text = """
    # a comment line
    AAPL 12 mentions

    tsla 8 mentions
    123 5 mentions
    GME 3 mentions
    """
    assert parse_ticker_whitelist(text) == frozenset({"AAPL", "GME"})


def test_tweet_requires_created_at_or_timestamp() -> None:
    with pytest.raises(ValidationError):
        Tweet(id="1", text="no timestamp at all")


def test_tweet_uses_timestamp_when_created_at_missing() -> None:
    tweet = Tweet(id="1", timestamp="2026-01-01T10:00:00Z", text="hi")
    assert tweet.posted_at.isoformat() == "2026-01-01T10:00:00+00:00"


def test_tweet_naive_datetime_is_treated_as_utc() -> None:
    tweet = Tweet(id="1", timestamp="2026-01-01T10:00:00", text="hi")
    assert tweet.posted_at.tzinfo == UTC
    assert tweet.posted_at.hour == 10
