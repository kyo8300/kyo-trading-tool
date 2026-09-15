"""Shared exponential backoff policy for retried external calls (N-4).

`domain/` has no I/O of its own; this module only computes the delay
sequence. Callers (market/, broker/, analysis/) decide which calls are
idempotent enough to retry and perform the actual sleeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """`max_attempts` total tries, exponential backoff between them."""

    max_attempts: int
    base_delay_s: Decimal
    max_delay_s: Decimal


def backoff_delays(policy: RetryPolicy) -> tuple[Decimal, ...]:
    """Return the delay before each retry (length = `max_attempts - 1`).

    Delay before retry `i` (0-indexed) is `base_delay_s * 2**i`, clipped to
    `max_delay_s`.
    """
    delays: list[Decimal] = []
    for attempt_index in range(policy.max_attempts - 1):
        delay = policy.base_delay_s * Decimal(2**attempt_index)
        delays.append(min(delay, policy.max_delay_s))
    return tuple(delays)
