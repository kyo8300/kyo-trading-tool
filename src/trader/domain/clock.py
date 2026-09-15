"""Clock abstraction so time can be injected in tests (N-9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Something that can report the current UTC time."""

    def now(self) -> datetime: ...


def to_utc(value: datetime) -> datetime:
    """Normalize `value` to an absolute UTC instant (R-17 review finding).

    A naive `value` (no `tzinfo`) is assumed to already be UTC -- every
    internal `Clock` in `src/` (`SystemClock`, `FixedClock`) always produces
    tz-aware UTC datetimes, so the only source of a naive datetime is
    operator input (e.g. the CLI `approve --expires-at` flag) that this
    normalizes rather than silently misinterpreting in the local timezone.
    An aware `value` with a non-UTC offset (e.g. `+09:00`) is converted to
    the equivalent UTC instant, not just re-labeled, so downstream
    comparisons/storage are on the same absolute timeline regardless of the
    offset the caller supplied.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Real wall-clock time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    """A clock that always returns the same instant, for deterministic tests."""

    fixed_now: datetime

    def now(self) -> datetime:
        return self.fixed_now

    def advance(self, delta: timedelta) -> FixedClock:
        """Return a new `FixedClock` moved forward (or back) by `delta`."""
        return FixedClock(self.fixed_now + delta)
