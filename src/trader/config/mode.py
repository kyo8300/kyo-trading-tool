"""Trading mode decision: paper vs live, decided by a pure function (R-15).

`decide_mode` never touches the environment itself -- the CLI/settings layer
reads `os.environ` and passes plain values in, which keeps this module
trivially testable and auditable (it is the one place that decides whether
real money is at risk).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trader.domain.result import Err, Ok, Result

_LIVE_CONFIRM_TOKEN = "I_ACCEPT_REAL_MONEY_RISK"  # noqa: S105 -- not a secret, a fixed confirmation phrase


class TradingMode(StrEnum):
    paper = "paper"
    live = "live"


@dataclass(frozen=True, slots=True)
class ConfigError:
    """A human-readable configuration error.

    `message` must never contain secret values (key material) -- only
    variable names, modes, and other non-sensitive context (N-6).
    """

    message: str


def decide_mode(
    mode_env: str | None,
    trader_env: str | None,
    live_keys_present: bool,
    live_confirm: str | None,
) -> Result[TradingMode, ConfigError]:
    """Decide the trading mode from environment inputs.

    - Missing/empty `mode_env` or `"paper"` -> paper.
    - `"live"` requires all of: `live_keys_present`, `live_confirm` equal to
      the exact confirmation token, and `trader_env != "test"`. Any other
      value for `mode_env` is rejected.
    - `trader_env == "test"` unconditionally refuses live, regardless of
      keys/confirmation (tests must never risk real money).
    """
    normalized = (mode_env or "").strip()

    if normalized in ("", TradingMode.paper.value):
        return Ok(TradingMode.paper)

    if normalized != TradingMode.live.value:
        return Err(ConfigError(f"TRADER_MODE must be 'paper' or 'live', got '{normalized}'"))

    if trader_env == "test":
        return Err(ConfigError("live mode is refused while TRADER_ENV=test"))

    if not live_keys_present:
        return Err(
            ConfigError(
                "live mode requires ALPACA_LIVE_TRADE_KEY and ALPACA_LIVE_TRADE_SECRET to be set"
            )
        )

    if live_confirm != _LIVE_CONFIRM_TOKEN:
        return Err(ConfigError(f"live mode requires TRADER_LIVE_CONFIRM={_LIVE_CONFIRM_TOKEN}"))

    return Ok(TradingMode.live)
