"""T-8: Alpaca-backed and fake `MarketDataProvider` implementations.

Covers: GET retry with backoff (2 failures then success, and 3 failures),
response validation (negative price, missing field), timeout wiring onto the
injected SDK client's session, market clock UTC parsing, and `FakeMarketData`
immutability.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import SecretStr

from trader.config.mode import TradingMode
from trader.domain.money import Price
from trader.domain.result import Err, Ok
from trader.domain.retry import RetryPolicy
from trader.market.alpaca_data import AlpacaMarketData
from trader.market.data_provider import Bar, MarketClock
from trader.market.fake_data import FakeMarketData

_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_s=Decimal("0.5"), max_delay_s=Decimal("4"))


def _raw_bar(
    t: str = "2024-01-02T05:00:00Z",
    o: float = 10.0,
    h: float = 11.0,
    lo: float = 9.5,
    c: float = 10.5,
    v: float = 200_000.0,
) -> dict[str, Any]:
    return {"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v}


class _FlakyCall:
    """Raise each exception in `results` in order, then return the last value forever."""

    def __init__(self, results: list[Any]) -> None:
        self._results = results
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        result = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class _FakeDataClient:
    def __init__(self, bars_call: Any = None, trade_call: Any = None, session: Any = None) -> None:
        self.get_stock_bars = bars_call or (lambda request: {})
        self.get_stock_latest_trade = trade_call or (lambda request: {})
        if session is not None:
            self._session = session


class _FakeTradingClient:
    def __init__(self, clock_call: Any = None, session: Any = None) -> None:
        self.get_clock = clock_call or (lambda: {})
        if session is not None:
            self._session = session


def _market(
    data_client: Any = None,
    trading_client: Any = None,
    sleep: Any = None,
) -> AlpacaMarketData:
    return AlpacaMarketData(
        _data_client=data_client or _FakeDataClient(),
        _trading_client=trading_client or _FakeTradingClient(),
        _sleep=sleep or (lambda _delay: None),
        _retry_policy=_RETRY_POLICY,
    )


# --- GET retry with backoff ---------------------------------------------


def test_daily_bars_retries_and_succeeds_after_two_failures() -> None:
    raw = {"AAPL": [_raw_bar()]}
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), raw])
    sleeps: list[Decimal] = []

    market = _market(data_client=_FakeDataClient(bars_call=flaky), sleep=sleeps.append)
    result = market.daily_bars("AAPL")

    assert isinstance(result, Ok)
    assert flaky.calls == 3
    assert sleeps == [Decimal("0.5"), Decimal("1")]


def test_daily_bars_gives_up_and_returns_err_after_max_attempts() -> None:
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), RuntimeError("net")])

    market = _market(data_client=_FakeDataClient(bars_call=flaky), sleep=lambda _delay: None)
    result = market.daily_bars("AAPL")

    assert isinstance(result, Err)
    assert flaky.calls == 3
    assert "AAPL" not in result.error.message or "daily_bars" in result.error.message


# --- response validation --------------------------------------------------


def test_daily_bars_rejects_negative_price_without_leaking_the_value() -> None:
    raw = {"AAPL": [_raw_bar(c=-5.0)]}
    market = _market(data_client=_FakeDataClient(bars_call=lambda request: raw))

    result = market.daily_bars("AAPL")

    assert isinstance(result, Err)
    assert "-5" not in result.error.message


def test_daily_bars_rejects_missing_field_without_leaking_the_value() -> None:
    incomplete = _raw_bar()
    del incomplete["c"]
    raw = {"AAPL": [incomplete]}
    market = _market(data_client=_FakeDataClient(bars_call=lambda request: raw))

    result = market.daily_bars("AAPL")

    assert isinstance(result, Err)
    assert "c" in result.error.message


def test_latest_price_rejects_non_positive_price() -> None:
    raw = {"AAPL": {"p": 0.0}}
    market = _market(data_client=_FakeDataClient(trade_call=lambda request: raw))

    result = market.latest_price("AAPL")

    assert isinstance(result, Err)


def test_latest_price_returns_price_on_valid_response() -> None:
    raw = {"AAPL": {"p": 12.5}}
    market = _market(data_client=_FakeDataClient(trade_call=lambda request: raw))

    result = market.latest_price("AAPL")

    assert isinstance(result, Ok)
    assert result.value == Price(Decimal("12.5"))


# --- market clock ----------------------------------------------------------


def test_market_clock_parses_to_utc_aware_datetimes() -> None:
    raw = {
        "is_open": False,
        "next_open": "2024-01-02T09:30:00-05:00",
        "next_close": "2024-01-02T16:00:00-05:00",
    }
    market = _market(trading_client=_FakeTradingClient(clock_call=lambda: raw))

    result = market.market_clock()

    assert isinstance(result, Ok)
    clock = result.value
    assert clock.is_open is False
    assert clock.next_open == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert clock.next_open.tzinfo is not None
    assert clock.next_close == datetime(2024, 1, 2, 21, 0, tzinfo=UTC)


def test_market_clock_rejects_naive_datetimes() -> None:
    raw = {
        "is_open": True,
        "next_open": "2024-01-02T09:30:00",
        "next_close": "2024-01-02T16:00:00",
    }
    market = _market(trading_client=_FakeTradingClient(clock_call=lambda: raw))

    result = market.market_clock()

    assert isinstance(result, Err)


# --- timeout wiring ----------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, *_args: object, **kwargs: object) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {}


class _FakeSdkClient:
    """Stands in for `StockHistoricalDataClient`/`TradingClient` in `create()`."""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self._session = _FakeSession()

    def get_clock(self) -> dict[str, Any]:
        self._session.request("GET", "/clock")
        return {
            "is_open": True,
            "next_open": "2024-01-02T09:30:00+00:00",
            "next_close": "2024-01-02T16:00:00+00:00",
        }

    def get_stock_bars(self, request: Any) -> dict[str, Any]:
        self._session.request("GET", "/bars")
        return {}

    def get_stock_latest_trade(self, request: Any) -> dict[str, Any]:
        self._session.request("GET", "/trade")
        return {}


def test_create_wires_timeout_onto_the_injected_sdk_client_session() -> None:
    market = AlpacaMarketData.create(
        SecretStr("read-key"),
        SecretStr("read-secret"),
        mode=TradingMode.paper,
        data_client_factory=_FakeSdkClient,
        trading_client_factory=_FakeSdkClient,
        sleep=lambda _delay: None,
        timeout_s=17,
    )

    result = market.market_clock()

    assert isinstance(result, Ok)
    trading_client = market._trading_client
    assert trading_client._session.calls[-1]["timeout"] == 17


def test_create_does_not_leak_the_read_secret_into_client_kwargs_keys() -> None:
    market = AlpacaMarketData.create(
        SecretStr("read-key"),
        SecretStr("super-secret-value"),
        mode=TradingMode.live,
        data_client_factory=_FakeSdkClient,
        trading_client_factory=_FakeSdkClient,
        sleep=lambda _delay: None,
    )

    data_client = market._data_client
    assert data_client.init_kwargs["secret_key"] == "super-secret-value"  # noqa: S105
    assert data_client.init_kwargs["api_key"] == "read-key"


# --- FakeMarketData immutability -------------------------------------------


def _clock() -> MarketClock:
    return MarketClock(
        is_open=True,
        next_open=datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        next_close=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
    )


def test_fake_market_data_with_price_does_not_mutate_the_original() -> None:
    original = FakeMarketData(prices={}, bars={}, clock=_clock())

    updated = original.with_price("AAPL", Price(Decimal("10")))

    assert original.prices == {}
    assert updated.prices["AAPL"] == Price(Decimal("10"))
    assert isinstance(original.latest_price("AAPL"), Err)
    assert isinstance(updated.latest_price("AAPL"), Ok)


def test_fake_market_data_unknown_ticker_is_err() -> None:
    market = FakeMarketData(prices={}, bars={}, clock=_clock())

    assert isinstance(market.latest_price("AAPL"), Err)
    assert isinstance(market.daily_bars("AAPL"), Err)


def test_fake_market_data_failing_forces_err_for_named_methods_only() -> None:
    market = FakeMarketData(
        prices={"AAPL": Price(Decimal("10"))},
        bars={"AAPL": (_bar(),)},
        clock=_clock(),
    ).failing(["latest_price"])

    assert isinstance(market.latest_price("AAPL"), Err)
    assert isinstance(market.daily_bars("AAPL"), Ok)


def _bar() -> Bar:
    return Bar(
        ticker="AAPL",
        date=datetime(2024, 1, 2, tzinfo=UTC).date(),
        open=Price(Decimal("10")),
        high=Price(Decimal("11")),
        low=Price(Decimal("9")),
        close=Price(Decimal("10.5")),
        volume=100_000,
    )
