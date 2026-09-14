"""`MarketDataProvider` protocol: daily bars, latest price, market clock (Q7).

This module only defines the shape of market data access. Implementations
(`alpaca_data.AlpacaMarketData`, `fake_data.FakeMarketData`) live behind this
protocol so `engine/` never depends on a specific broker's SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from trader.domain.money import Price
from trader.domain.result import Result


@dataclass(frozen=True, slots=True)
class MarketError:
    """A human-readable market-data error.

    `message` must never contain secret values or stack traces (N-6).
    """

    message: str


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily OHLCV bar for `ticker`."""

    ticker: str
    date: date
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int


@dataclass(frozen=True, slots=True)
class MarketClock:
    """The current market session state (America/New_York, UTC aware)."""

    is_open: bool
    next_open: datetime
    next_close: datetime


class MarketDataProvider(Protocol):
    """Behind-the-interface access to price history and market hours."""

    def daily_bars(self, ticker: str, days: int = 20) -> Result[tuple[Bar, ...], MarketError]:
        """Return up to `days` most recent daily bars for `ticker`, oldest first."""
        ...

    def latest_price(self, ticker: str) -> Result[Price, MarketError]:
        """Return the latest traded price for `ticker`."""
        ...

    def market_clock(self) -> Result[MarketClock, MarketError]:
        """Return whether the market is currently open and the next session bounds."""
        ...


def average_volume(bars: tuple[Bar, ...]) -> int:
    """Return the integer average daily volume across `bars` (0 if empty)."""
    if not bars:
        return 0
    return sum(bar.volume for bar in bars) // len(bars)
