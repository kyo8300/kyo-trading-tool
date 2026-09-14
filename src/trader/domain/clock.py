"""Clock abstraction so time can be injected in tests (N-9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Something that can report the current UTC time."""

    def now(self) -> datetime: ...


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
