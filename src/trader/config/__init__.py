"""Public config API: mode decision and mode-scoped settings (R-15, N-2, N-3)."""

from __future__ import annotations

from trader.config.mode import ConfigError, TradingMode, decide_mode
from trader.config.settings import CommonSettings, LiveSettings, PaperSettings, load_settings

__all__ = [
    "CommonSettings",
    "ConfigError",
    "LiveSettings",
    "PaperSettings",
    "TradingMode",
    "decide_mode",
    "load_settings",
]
