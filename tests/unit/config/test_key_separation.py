"""paper/live settings never cross-read each other's secrets (AC-14, N-3)."""

from __future__ import annotations

from trader.config.mode import TradingMode
from trader.config.settings import LiveSettings, PaperSettings, load_settings
from trader.domain.result import is_ok

PAPER_ENV = {
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "ALPACA_PAPER_READ_KEY": "paper-read-key",
    "ALPACA_PAPER_READ_SECRET": "paper-read-secret",
    "ALPACA_PAPER_TRADE_KEY": "paper-trade-key",
    "ALPACA_PAPER_TRADE_SECRET": "paper-trade-secret",
}

LIVE_ENV = {
    "TRADER_MODE": "live",
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "ALPACA_LIVE_READ_KEY": "live-read-key",
    "ALPACA_LIVE_READ_SECRET": "live-read-secret",
    "ALPACA_LIVE_TRADE_KEY": "live-trade-key",
    "ALPACA_LIVE_TRADE_SECRET": "live-trade-secret",
    "TRADER_LIVE_CONFIRM": "I_ACCEPT_REAL_MONEY_RISK",
}


def test_paper_settings_has_no_live_fields() -> None:
    field_names = set(PaperSettings.model_fields)
    assert not any("LIVE" in name for name in field_names)


def test_load_settings_with_only_paper_vars_succeeds_as_paper_settings() -> None:
    result = load_settings(PAPER_ENV)
    assert is_ok(result)
    assert isinstance(result.value, PaperSettings)
    assert result.value.mode is TradingMode.paper


def test_paper_mode_ignores_live_vars_present_in_environ() -> None:
    mixed = {**PAPER_ENV, **{k: v for k, v in LIVE_ENV.items() if k != "TRADER_MODE"}}
    result = load_settings(mixed)
    assert is_ok(result)
    settings = result.value
    assert isinstance(settings, PaperSettings)
    assert not hasattr(settings, "ALPACA_LIVE_TRADE_KEY")
    assert "live-trade-key" not in repr(settings)


def test_load_settings_with_live_vars_succeeds_as_live_settings() -> None:
    result = load_settings(LIVE_ENV)
    assert is_ok(result)
    assert isinstance(result.value, LiveSettings)
    assert result.value.mode is TradingMode.live
