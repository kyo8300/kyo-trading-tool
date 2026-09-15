"""Exponential backoff sequence for retried GET calls (N-4)."""

from __future__ import annotations

from decimal import Decimal

from trader.domain.retry import RetryPolicy, backoff_delays


def test_backoff_delays_length_is_max_attempts_minus_one() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_s=Decimal("0.5"), max_delay_s=Decimal("4"))
    delays = backoff_delays(policy)
    assert len(delays) == 2


def test_backoff_delays_double_each_attempt() -> None:
    policy = RetryPolicy(max_attempts=4, base_delay_s=Decimal("0.5"), max_delay_s=Decimal("100"))
    delays = backoff_delays(policy)
    assert delays == (Decimal("0.5"), Decimal("1.0"), Decimal("2.0"))


def test_backoff_delays_clip_to_max_delay() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_s=Decimal("1"), max_delay_s=Decimal("4"))
    delays = backoff_delays(policy)
    assert delays == (Decimal("1"), Decimal("2"), Decimal("4"), Decimal("4"))


def test_single_attempt_policy_has_no_delays() -> None:
    policy = RetryPolicy(max_attempts=1, base_delay_s=Decimal("1"), max_delay_s=Decimal("4"))
    assert backoff_delays(policy) == ()
