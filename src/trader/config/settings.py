"""Mode-scoped settings (N-2, N-3): a process only ever loads the secrets it
needs for the mode it decided to run in.

`load_settings` never reads `os.environ` or a `.env` file itself -- the
caller passes an explicit `environ` mapping, and only the keys that the
selected settings class declares are picked out of it. The settings classes
themselves are configured to ignore the process environment entirely (see
`_ExplicitSettings`), so nothing outside the given mapping can leak in.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from trader.config.mode import ConfigError, TradingMode, decide_mode
from trader.domain.result import Err, Ok, Result

_DEFAULT_DB_PATH = Path("var/trader.sqlite3")
_DEFAULT_RULES_PATH = Path("rules/trading-rules.yaml")
_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


class _ExplicitSettings(BaseSettings):
    """Base settings that only ever read from constructor kwargs.

    The process environment and `.env` files are never consulted (even
    though `pydantic_settings.BaseSettings` would do so by default) so that
    `load_settings` fully controls what a settings instance can see.
    """

    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


class CommonSettings(_ExplicitSettings):
    """Settings shared by every mode."""

    ANTHROPIC_API_KEY: SecretStr
    ANTHROPIC_MODEL: str = _DEFAULT_ANTHROPIC_MODEL
    TRADER_DB_PATH: Path = _DEFAULT_DB_PATH
    TRADER_RULES_PATH: Path = _DEFAULT_RULES_PATH


class PaperSettings(CommonSettings):
    """Settings for `TradingMode.paper`. Holds no live-mode fields at all."""

    ALPACA_PAPER_READ_KEY: SecretStr
    ALPACA_PAPER_READ_SECRET: SecretStr
    ALPACA_PAPER_TRADE_KEY: SecretStr
    ALPACA_PAPER_TRADE_SECRET: SecretStr

    @property
    def mode(self) -> TradingMode:
        return TradingMode.paper


class LiveSettings(CommonSettings):
    """Settings for `TradingMode.live`."""

    ALPACA_LIVE_READ_KEY: SecretStr
    ALPACA_LIVE_READ_SECRET: SecretStr
    ALPACA_LIVE_TRADE_KEY: SecretStr
    ALPACA_LIVE_TRADE_SECRET: SecretStr
    TRADER_LIVE_CONFIRM: str

    @property
    def mode(self) -> TradingMode:
        return TradingMode.live


def _pick(environ: Mapping[str, str], settings_cls: type[CommonSettings]) -> dict[str, Any]:
    field_names = set(settings_cls.model_fields)
    return {key: value for key, value in environ.items() if key in field_names}


def _missing_fields_message(exc: ValidationError) -> str:
    fields = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
    if not fields:
        return "invalid settings"
    return "missing or invalid settings: " + ", ".join(fields)


def load_settings(environ: Mapping[str, str]) -> Result[PaperSettings | LiveSettings, ConfigError]:
    """Decide the mode from `environ`, then build only that mode's settings.

    Only the environment variable names declared by the selected settings
    class are read out of `environ`; nothing else (including the real
    process environment) is consulted.
    """
    live_trade_key = environ.get("ALPACA_LIVE_TRADE_KEY", "")
    live_trade_secret = environ.get("ALPACA_LIVE_TRADE_SECRET", "")
    live_keys_present = bool(live_trade_key) and bool(live_trade_secret)

    mode_result = decide_mode(
        mode_env=environ.get("TRADER_MODE"),
        trader_env=environ.get("TRADER_ENV"),
        live_keys_present=live_keys_present,
        live_confirm=environ.get("TRADER_LIVE_CONFIRM"),
    )
    if isinstance(mode_result, Err):
        return Err(mode_result.error)

    settings_cls: type[PaperSettings] | type[LiveSettings] = (
        PaperSettings if mode_result.value is TradingMode.paper else LiveSettings
    )
    picked = _pick(environ, settings_cls)

    try:
        settings = settings_cls(**picked)
    except ValidationError as exc:
        return Err(ConfigError(_missing_fields_message(exc)))

    return Ok(settings)
