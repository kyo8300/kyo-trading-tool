"""Missing/invalid settings are rejected with a value-free message (AC-25, N-5, N-6)."""

from __future__ import annotations

import pytest

from trader.config.settings import PaperSettings, load_settings
from trader.domain.result import is_err, is_ok

SECRET_VALUE = "super-secret-value"  # noqa: S105 -- test fixture value, not a real secret


def test_missing_required_var_is_rejected_with_variable_name_only() -> None:
    environ = {
        "ANTHROPIC_API_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_SECRET": SECRET_VALUE,
        # ALPACA_PAPER_TRADE_KEY / ALPACA_PAPER_TRADE_SECRET intentionally missing
    }

    result = load_settings(environ)

    assert is_err(result)
    message = result.error.message
    assert "ALPACA_PAPER_TRADE_KEY" in message
    assert "ALPACA_PAPER_TRADE_SECRET" in message
    assert SECRET_VALUE not in message


def test_missing_anthropic_key_is_rejected() -> None:
    environ = {
        "ALPACA_PAPER_READ_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_SECRET": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_KEY": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_SECRET": SECRET_VALUE,
    }

    result = load_settings(environ)

    assert is_err(result)
    assert "ANTHROPIC_API_KEY" in result.error.message
    assert SECRET_VALUE not in result.error.message


def test_settings_repr_never_reveals_secret_values() -> None:
    environ = {
        "ANTHROPIC_API_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_SECRET": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_KEY": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_SECRET": SECRET_VALUE,
    }

    result = load_settings(environ)

    assert is_ok(result)
    settings = result.value
    assert isinstance(settings, PaperSettings)
    assert SECRET_VALUE not in repr(settings)
    assert SECRET_VALUE not in str(settings)


def test_load_settings_never_reads_the_real_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load_settings` only ever consults the `environ` mapping it is given
    -- it must not fall back to `os.environ` (module docstring guarantee).
    A value placed in the real process environment but omitted from the
    mapping must still be reported as missing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_VALUE)

    environ_without_anthropic_key = {
        "ALPACA_PAPER_READ_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_SECRET": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_KEY": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_SECRET": SECRET_VALUE,
    }

    result = load_settings(environ_without_anthropic_key)

    assert is_err(result)
    assert "ANTHROPIC_API_KEY" in result.error.message
    assert SECRET_VALUE not in result.error.message


def test_secret_fields_are_only_readable_via_get_secret_value() -> None:
    """N-6: secret fields are `SecretStr`, so the raw value is never
    exposed by default string conversion and can only be recovered
    explicitly through `get_secret_value()`."""
    environ = {
        "ANTHROPIC_API_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_SECRET": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_KEY": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_SECRET": SECRET_VALUE,
    }

    result = load_settings(environ)

    assert is_ok(result)
    settings = result.value
    assert isinstance(settings, PaperSettings)
    assert str(settings.ANTHROPIC_API_KEY) != SECRET_VALUE
    assert settings.ANTHROPIC_API_KEY.get_secret_value() == SECRET_VALUE
    assert settings.ALPACA_PAPER_TRADE_KEY.get_secret_value() == SECRET_VALUE


def test_settings_apply_documented_defaults() -> None:
    environ = {
        "ANTHROPIC_API_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_KEY": SECRET_VALUE,
        "ALPACA_PAPER_READ_SECRET": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_KEY": SECRET_VALUE,
        "ALPACA_PAPER_TRADE_SECRET": SECRET_VALUE,
    }

    result = load_settings(environ)

    assert is_ok(result)
    settings = result.value
    assert str(settings.TRADER_DB_PATH) == "var/trader.sqlite3"
    assert str(settings.TRADER_RULES_PATH) == "rules/trading-rules.yaml"
