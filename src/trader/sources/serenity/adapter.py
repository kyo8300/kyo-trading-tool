"""`SerenityAdapter`: reads `data_dir/tweets.json` + `data_dir/ticker_stats.txt`
and turns them into validated `Mention` records (R-1, R-2, R-4).

Validation is fail-fast for the whole file: if any record is invalid, no
`Mention`s are returned at all (`IngestError` with the invalid count and a
handful of `"index N: <field>: <msg>"` examples).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trader.domain.models import Mention
from trader.domain.result import Err, Ok, Result
from trader.sources.adapter import IngestError
from trader.sources.serenity.schema import Tweet, extract_tickers, parse_ticker_whitelist

_MAX_EXCERPT_CHARS = 280
_MAX_FIRST_ERRORS = 5
_SOURCE_ID = "serenity"


def _boundary_error(message: str) -> IngestError:
    return IngestError(message=message, invalid_count=0, first_errors=())


def _read_tweets(path: Path) -> Result[list[Any], IngestError]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return Err(_boundary_error("tweets.json が見つからない、または読み込めない"))

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return Err(_boundary_error("tweets.json の形式が不正 (JSON として解析できない)"))

    if isinstance(data, dict):
        if not isinstance(data.get("tweets"), list):
            return Err(
                _boundary_error(
                    "tweets.json の形式が不正 (トップレベルが配列、"
                    "または 'tweets' キーに配列を持つオブジェクトである必要がある)"
                )
            )
        data = data["tweets"]
    if not isinstance(data, list):
        return Err(_boundary_error("tweets.json の形式が不正 (配列が期待される)"))
    return Ok(data)


def _read_whitelist(path: Path) -> Result[frozenset[str], IngestError]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Err(_boundary_error("ticker_stats.txt が見つからない、または読み込めない"))
    return Ok(parse_ticker_whitelist(text))


def _validate_all(records: list[Any]) -> Result[list[Tweet], IngestError]:
    errors: list[str] = []
    invalid_indices: set[int] = set()
    tweets: list[Tweet] = []
    for index, record in enumerate(records):
        try:
            tweets.append(Tweet.model_validate(record))
        except ValidationError as exc:
            invalid_indices.add(index)
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"]) or "value"
                errors.append(f"index {index}: {field}: {error['msg']}")

    if invalid_indices:
        return Err(
            IngestError(
                message=f"{len(invalid_indices)} 件のレコードが不正です",
                invalid_count=len(invalid_indices),
                first_errors=tuple(errors[:_MAX_FIRST_ERRORS]),
            )
        )
    return Ok(tweets)


def _tweet_raw_sha256(tweet: Tweet) -> str:
    return hashlib.sha256(tweet.model_dump_json().encode("utf-8")).hexdigest()


def _tweet_to_mentions(tweet: Tweet, whitelist: frozenset[str]) -> tuple[Mention, ...]:
    tickers = extract_tickers(tweet.text, whitelist)
    if not tickers:
        return ()
    excerpt = tweet.text[:_MAX_EXCERPT_CHARS]
    raw_sha256 = _tweet_raw_sha256(tweet)
    return tuple(
        Mention(
            id=f"{_SOURCE_ID}:{tweet.id}:{ticker}",
            source_id=_SOURCE_ID,
            external_id=tweet.id,
            ticker=ticker,
            posted_at=tweet.posted_at,
            text_excerpt=excerpt,
            url=tweet.url,
            raw_sha256=raw_sha256,
        )
        for ticker in tickers
    )


@dataclass(frozen=True, slots=True)
class SerenityAdapter:
    """`SourceAdapter` for the yan-labs `serenity` aggregated tweet archive."""

    @property
    def source_id(self) -> str:
        return _SOURCE_ID

    def load(self, data_dir: Path) -> Result[tuple[Mention, ...], IngestError]:
        records_result = _read_tweets(data_dir / "tweets.json")
        if isinstance(records_result, Err):
            return records_result

        whitelist_result = _read_whitelist(data_dir / "ticker_stats.txt")
        if isinstance(whitelist_result, Err):
            return whitelist_result
        whitelist = whitelist_result.value

        tweets_result = _validate_all(records_result.value)
        if isinstance(tweets_result, Err):
            return tweets_result

        mentions: list[Mention] = []
        for tweet in tweets_result.value:
            mentions.extend(_tweet_to_mentions(tweet, whitelist))
        return Ok(tuple(mentions))
