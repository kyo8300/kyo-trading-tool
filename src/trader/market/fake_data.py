"""`FakeMarketData`: an in-memory `MarketDataProvider` for tests.

Every "mutation" (`with_price`, `with_clock`, `failing`) returns a new frozen
instance; the original is never touched (N-7).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from trader.domain.money import Price
from trader.domain.result import Err, Ok, Result
from trader.market.data_provider import Bar, MarketClock, MarketError


@dataclass(frozen=True, slots=True)
class FakeMarketData:
    """Test double for `MarketDataProvider`. Unknown tickers are `Err`."""

    prices: Mapping[str, Price]
    bars: Mapping[str, tuple[Bar, ...]]
    clock: MarketClock
    errors: frozenset[str] = frozenset()

    def daily_bars(self, ticker: str, days: int = 20) -> Result[tuple[Bar, ...], MarketError]:
        if "daily_bars" in self.errors:
            return Err(MarketError("daily_bars is set to fail"))
        bars = self.bars.get(ticker)
        if bars is None:
            return Err(MarketError(f"no bars configured for ticker {ticker}"))
        return Ok(bars[-days:])

    def latest_price(self, ticker: str) -> Result[Price, MarketError]:
        if "latest_price" in self.errors:
            return Err(MarketError("latest_price is set to fail"))
        price = self.prices.get(ticker)
        if price is None:
            return Err(MarketError(f"no price configured for ticker {ticker}"))
        return Ok(price)

    def market_clock(self) -> Result[MarketClock, MarketError]:
        if "market_clock" in self.errors:
            return Err(MarketError("market_clock is set to fail"))
        return Ok(self.clock)

    def with_price(self, ticker: str, price: Price) -> FakeMarketData:
        """Return a new instance with `ticker` mapped to `price` (others unchanged)."""
        return replace(self, prices={**self.prices, ticker: price})

    def with_bars(self, ticker: str, bars: tuple[Bar, ...]) -> FakeMarketData:
        """Return a new instance with `ticker` mapped to `bars` (others unchanged)."""
        return replace(self, bars={**self.bars, ticker: bars})

    def with_clock(self, clock: MarketClock) -> FakeMarketData:
        """Return a new instance with the market clock replaced."""
        return replace(self, clock=clock)

    def failing(self, method_names: Iterable[str]) -> FakeMarketData:
        """Return a new instance where each name in `method_names` returns `Err`."""
        return replace(self, errors=frozenset(method_names))
