"""Mode decision truth table (AC-13, R-15)."""

from __future__ import annotations

from trader.config.mode import TradingMode, decide_mode
from trader.domain.result import is_err, is_ok

CONFIRM = "I_ACCEPT_REAL_MONEY_RISK"


def test_missing_mode_env_defaults_to_paper() -> None:
    result = decide_mode(mode_env=None, trader_env=None, live_keys_present=False, live_confirm=None)
    assert is_ok(result)
    assert result.value is TradingMode.paper


def test_empty_mode_env_defaults_to_paper() -> None:
    result = decide_mode(mode_env="", trader_env=None, live_keys_present=False, live_confirm=None)
    assert is_ok(result)
    assert result.value is TradingMode.paper


def test_explicit_paper_is_accepted() -> None:
    result = decide_mode(
        mode_env="paper", trader_env=None, live_keys_present=False, live_confirm=None
    )
    assert is_ok(result)
    assert result.value is TradingMode.paper


def test_live_without_keys_is_rejected() -> None:
    result = decide_mode(
        mode_env="live", trader_env=None, live_keys_present=False, live_confirm=CONFIRM
    )
    assert is_err(result)


def test_live_with_keys_but_no_confirm_is_rejected() -> None:
    result = decide_mode(
        mode_env="live", trader_env=None, live_keys_present=True, live_confirm=None
    )
    assert is_err(result)


def test_live_with_keys_and_confirm_is_accepted() -> None:
    result = decide_mode(
        mode_env="live", trader_env=None, live_keys_present=True, live_confirm=CONFIRM
    )
    assert is_ok(result)
    assert result.value is TradingMode.live


def test_live_is_refused_unconditionally_under_trader_env_test() -> None:
    result = decide_mode(
        mode_env="live", trader_env="test", live_keys_present=True, live_confirm=CONFIRM
    )
    assert is_err(result)


def test_unknown_mode_value_is_rejected() -> None:
    result = decide_mode(
        mode_env="production", trader_env=None, live_keys_present=True, live_confirm=CONFIRM
    )
    assert is_err(result)


def test_uppercase_live_is_rejected_case_sensitively() -> None:
    """AC-13 boundary: `TRADER_MODE` matching is case-sensitive. Uppercase
    'LIVE' is treated as an unknown value, not normalized to live -- this
    pins the current strict behaviour so it cannot silently change."""
    result = decide_mode(
        mode_env="LIVE", trader_env=None, live_keys_present=True, live_confirm=CONFIRM
    )
    assert is_err(result)


def test_live_with_surrounding_whitespace_is_trimmed_and_accepted() -> None:
    """AC-13 boundary: leading/trailing whitespace around 'live' is
    stripped before comparison, so ' live ' behaves like 'live'."""
    result = decide_mode(
        mode_env=" live ", trader_env=None, live_keys_present=True, live_confirm=CONFIRM
    )
    assert is_ok(result)
    assert result.value is TradingMode.live


def test_live_is_refused_even_with_all_conditions_met_under_trader_env_test() -> None:
    """AC-13: TRADER_ENV=test refuses live unconditionally, even when
    mode/keys/confirm are all otherwise valid -- the test-env guard must
    win regardless of ordering."""
    result = decide_mode(
        mode_env="live", trader_env="test", live_keys_present=True, live_confirm=CONFIRM
    )
    assert is_err(result)


def test_live_confirm_with_wrong_value_is_rejected() -> None:
    """AC-13 boundary: a plausible-looking but incorrect confirm string
    ('yes') must not satisfy the live confirmation requirement."""
    result = decide_mode(
        mode_env="live", trader_env=None, live_keys_present=True, live_confirm="yes"
    )
    assert is_err(result)
