"""R-17 review finding: `domain.clock.to_utc` normalizes any datetime to an
absolute UTC instant so downstream comparisons/storage are on the same
timeline regardless of what timezone (or lack thereof) the caller supplied.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from trader.domain.clock import to_utc


def test_naive_datetime_is_assumed_to_already_be_utc() -> None:
    """No tzinfo (e.g. operator CLI input) is treated as UTC, not the local
    timezone -- it must not be silently reinterpreted."""
    naive = datetime(2026, 1, 5, 15, 0)

    result = to_utc(naive)

    assert result == datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    assert result.tzinfo is UTC


def test_plus_nine_offset_is_converted_to_the_equivalent_utc_instant() -> None:
    """`+09:00` is converted, not just re-labeled: 2026-01-05 15:00+09:00 is
    2026-01-05 06:00 UTC, the same absolute instant."""
    jst = datetime(2026, 1, 5, 15, 0, tzinfo=timezone(timedelta(hours=9)))

    result = to_utc(jst)

    assert result == datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
    assert result.utcoffset() == timedelta(0)


def test_already_utc_datetime_is_returned_unchanged() -> None:
    already_utc = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)

    result = to_utc(already_utc)

    assert result == already_utc
    assert result.utcoffset() == timedelta(0)
