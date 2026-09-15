"""Pydantic schema for yan-labs `serenity` tweets.json records (R-1, R-2),
plus the ticker-whitelist / `$TICKER` extraction helpers used to turn tweet
text into `Mention`s.

Unknown fields are ignored (`extra="ignore"`) -- the archive may carry
fields this tool does not need, and adding them upstream must not break
ingestion.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_ID_RE = re.compile(r"^[0-9]+$")
_TICKER_TOKEN_RE = re.compile(r"^[A-Z]{1,5}$")
_TICKER_MENTION_RE = re.compile(r"\$([A-Z]{1,5})\b")


class Tweet(BaseModel):
    """One record of `tweets.json`. Only the fields this tool needs."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    created_at: datetime | None = None
    timestamp: datetime | None = None
    text: str
    url: str | None = None

    @field_validator("id")
    @classmethod
    def _id_must_be_numeric(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("id must be a numeric string")
        return value

    @model_validator(mode="after")
    def _requires_a_timestamp(self) -> Tweet:
        if self.created_at is None and self.timestamp is None:
            raise ValueError("created_at or timestamp is required")
        return self

    @property
    def posted_at(self) -> datetime:
        """The tweet's timestamp: `created_at` if present, else `timestamp`.

        Naive datetimes (no tzinfo) are assumed to already be UTC.
        """
        raw = self.created_at if self.created_at is not None else self.timestamp
        if raw is None:
            # Unreachable: `_requires_a_timestamp` rejects this at construction time.
            raise ValueError("created_at or timestamp is required")
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)


def parse_ticker_whitelist(text: str) -> frozenset[str]:
    """Parse a `ticker_stats.txt`-shaped file into a whitelist of tickers.

    Each non-empty, non-comment (`#`) line's first whitespace-separated
    token is taken as a candidate ticker; only tokens matching
    `^[A-Z]{1,5}$` are kept.
    """
    tickers: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        token = stripped.split()[0]
        if _TICKER_TOKEN_RE.match(token):
            tickers.add(token)
    return frozenset(tickers)


def extract_tickers(text: str, whitelist: frozenset[str]) -> tuple[str, ...]:
    """Extract `$TICKER`-shaped mentions from `text` that are in `whitelist`.

    Returned in order of first appearance, with duplicates removed.
    """
    seen: list[str] = []
    for match in _TICKER_MENTION_RE.finditer(text):
        ticker = match.group(1)
        if ticker in whitelist and ticker not in seen:
            seen.append(ticker)
    return tuple(seen)
