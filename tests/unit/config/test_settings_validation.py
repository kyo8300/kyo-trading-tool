"""Missing/invalid settings are rejected with a value-free message (AC-25, N-5, N-6)."""

from __future__ import annotations

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
