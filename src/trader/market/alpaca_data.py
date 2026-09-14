"""`AlpacaMarketData`: `MarketDataProvider` backed by the Alpaca Market Data
and Trading APIs (Q7).

This module is a boundary (N-5): raw SDK responses are received as `Any`,
validated with pydantic, and converted to `Decimal`-based domain types before
leaving this module. `float` only ever appears here as the shape the SDK
hands back at the boundary -- it never crosses into `Bar` / `Price` /
`MarketClock`.

The Alpaca SDK clients (`StockHistoricalDataClient` for read-only market
data, `TradingClient` for `get_clock`) are constructor-injected via
`create`'s factory hooks so tests can supply fake clients instead of talking
to a real Alpaca account (N-9).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from trader.config.mode import TradingMode
from trader.domain.money import Price
from trader.domain.result import Err, Ok, Result
from trader.domain.retry import RetryPolicy, backoff_delays
from trader.market.data_provider import Bar, MarketClock, MarketError

_GET_RETRY_POLICY = RetryPolicy(
    max_attempts=3, base_delay_s=Decimal("0.5"), max_delay_s=Decimal("4")
)
_DEFAULT_TIMEOUT_S = 10


def _default_sleep(delay_s: Decimal) -> None:
    time.sleep(float(delay_s))


class _RawBar(BaseModel):
    """A single raw Alpaca bar (abbreviated keys `t/o/h/l/c/v`)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    timestamp: datetime = Field(alias="t")
    open: float = Field(alias="o")
    high: float = Field(alias="h")
    low: float = Field(alias="l")
    close: float = Field(alias="c")
    volume: float = Field(alias="v")

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def _non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("volume")
    @classmethod
    def _volume_is_whole(cls, value: float) -> float:
        if value != int(value):
            raise ValueError("must be a whole number")
        return value

    @model_validator(mode="after")
    def _consistent_ohlc(self) -> _RawBar:
        if self.high < self.low:
            raise ValueError("high must be >= low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("open must be between low and high")
        if not (self.low <= self.close <= self.high):
            raise ValueError("close must be between low and high")
        return self


class _RawTrade(BaseModel):
    """A single raw Alpaca latest-trade record (`p` = price)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    price: float = Field(alias="p")

    @field_validator("price")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value


class _RawClock(BaseModel):
    """A raw Alpaca `TradingClient.get_clock()` response."""

    model_config = ConfigDict(extra="ignore")

    is_open: bool
    next_open: datetime
    next_close: datetime

    @field_validator("next_open", "next_close")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("must include a timezone offset")
        return value


def _validation_summary(exc: ValidationError) -> str:
    """A human-readable, value-free summary of a pydantic `ValidationError`.

    Only field names are included -- never the offending values (N-6).
    """
    fields = sorted({str(error["loc"][-1]) for error in exc.errors() if error["loc"]})
    if not fields:
        return "invalid response"
    return "invalid fields: " + ", ".join(fields)


def _to_bar(ticker: str, raw: Any) -> Result[Bar, MarketError]:
    try:
        parsed = _RawBar.model_validate(raw)
    except ValidationError as exc:
        return Err(MarketError(f"invalid bar data for {ticker}: {_validation_summary(exc)}"))
    return Ok(
        Bar(
            ticker=ticker,
            date=parsed.timestamp.date(),
            open=Price(Decimal(str(parsed.open))),
            high=Price(Decimal(str(parsed.high))),
            low=Price(Decimal(str(parsed.low))),
            close=Price(Decimal(str(parsed.close))),
            volume=int(parsed.volume),
        )
    )


def _install_timeout(client: Any, timeout_s: int) -> None:
    """Make the injected SDK client's HTTP session default to `timeout_s`.

    Real `alpaca-py` REST clients expose a `requests.Session` as `_session`
    but never pass a `timeout` to it (see `alpaca.common.rest.RESTClient`),
    so we wrap the session's `request` method to inject a default timeout.
    This avoids `signal`/thread-based timeouts entirely. Clients without a
    `_session` attribute (e.g. some fakes) are left untouched.
    """
    session = getattr(client, "_session", None)
    if session is None:
        return
    original_request = session.request

    def _timed_request(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout_s)
        return original_request(*args, **kwargs)

    session.request = _timed_request


@dataclass(frozen=True, slots=True)
class AlpacaMarketData:
    """`MarketDataProvider` backed by Alpaca's read-only market data APIs."""

    _data_client: Any
    _trading_client: Any
    _sleep: Callable[[Decimal], None]
    _retry_policy: RetryPolicy

    @classmethod
    def create(
        cls,
        read_key: SecretStr,
        read_secret: SecretStr,
        *,
        mode: TradingMode,
        data_client_factory: Callable[..., Any] = StockHistoricalDataClient,
        trading_client_factory: Callable[..., Any] = TradingClient,
        sleep: Callable[[Decimal], None] = _default_sleep,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> AlpacaMarketData:
        """Build an `AlpacaMarketData` from read-only Alpaca credentials.

        `mode` only affects `TradingClient` (paper vs live calendar/clock);
        the Market Data API URL does not vary by mode. SDK clients are built
        via the injected factories so tests can supply fakes.
        """
        data_client = data_client_factory(
            api_key=read_key.get_secret_value(),
            secret_key=read_secret.get_secret_value(),
            raw_data=True,
        )
        trading_client = trading_client_factory(
            api_key=read_key.get_secret_value(),
            secret_key=read_secret.get_secret_value(),
            paper=mode is TradingMode.paper,
            raw_data=True,
        )
        _install_timeout(data_client, timeout_s)
        _install_timeout(trading_client, timeout_s)
        return cls(
            _data_client=data_client,
            _trading_client=trading_client,
            _sleep=sleep,
            _retry_policy=_GET_RETRY_POLICY,
        )

    def daily_bars(self, ticker: str, days: int = 20) -> Result[tuple[Bar, ...], MarketError]:
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            limit=days,
        )
        raw_result = self._call_with_retry(
            lambda: self._data_client.get_stock_bars(request), op=f"daily_bars({ticker})"
        )
        if isinstance(raw_result, Err):
            return raw_result

        raw_value = raw_result.value
        if not isinstance(raw_value, dict) or ticker not in raw_value:
            return Err(MarketError(f"no bars data returned for {ticker}"))
        raw_bars = raw_value[ticker]
        bars: list[Bar] = []
        for raw_bar in raw_bars:
            bar_result = _to_bar(ticker, raw_bar)
            if isinstance(bar_result, Err):
                return bar_result
            bars.append(bar_result.value)
        return Ok(tuple(bars))

    def latest_price(self, ticker: str) -> Result[Price, MarketError]:
        request = StockLatestTradeRequest(symbol_or_symbols=ticker)
        raw_result = self._call_with_retry(
            lambda: self._data_client.get_stock_latest_trade(request),
            op=f"latest_price({ticker})",
        )
        if isinstance(raw_result, Err):
            return raw_result

        raw_value = raw_result.value
        raw_trade = raw_value.get(ticker) if isinstance(raw_value, dict) else None
        if raw_trade is None:
            return Err(MarketError(f"no latest trade returned for {ticker}"))
        try:
            parsed = _RawTrade.model_validate(raw_trade)
        except ValidationError as exc:
            return Err(MarketError(f"invalid trade data for {ticker}: {_validation_summary(exc)}"))
        return Ok(Price(Decimal(str(parsed.price))))

    def market_clock(self) -> Result[MarketClock, MarketError]:
        raw_result = self._call_with_retry(self._trading_client.get_clock, op="market_clock")
        if isinstance(raw_result, Err):
            return raw_result
        try:
            parsed = _RawClock.model_validate(raw_result.value)
        except ValidationError as exc:
            return Err(MarketError(f"invalid clock data: {_validation_summary(exc)}"))
        return Ok(
            MarketClock(
                is_open=parsed.is_open,
                next_open=parsed.next_open.astimezone(UTC),
                next_close=parsed.next_close.astimezone(UTC),
            )
        )

    def _call_with_retry(self, call: Callable[[], Any], *, op: str) -> Result[Any, MarketError]:
        """Retry `call` on any exception, following `_retry_policy`'s backoff.

        Only used for idempotent GET calls (N-4). `self._sleep` is invoked
        (not `time.sleep` directly) so tests can record backoff without
        actually waiting.
        """
        delays = backoff_delays(self._retry_policy)
        last_error: Exception | None = None
        for attempt in range(self._retry_policy.max_attempts):
            try:
                return Ok(call())
            except Exception as exc:
                last_error = exc
                if attempt < len(delays):
                    self._sleep(delays[attempt])
        return Err(
            MarketError(
                f"{op} failed after {self._retry_policy.max_attempts} attempts: "
                f"{type(last_error).__name__}"
            )
        )
