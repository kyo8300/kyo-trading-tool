"""T-2 (live-readiness): `daily_bars_since` on Alpaca and Fake providers (AC-8)."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    t: str = "2026-09-01T04:00:00Z",
    o: float = 10.0,
    h: float = 11.0,
    lo: float = 9.5,
    c: float = 10.5,
    v: float = 200_000.0,
) -> dict[str, Any]:
    return {"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v}


class _Flaky:
    def __init__(self, results: list[Any]) -> None:
        self._results = results
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        result = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class _Capturing:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return self.raw


class _DataClient:
    def __init__(self, bars_call: Any) -> None:
        self.get_stock_bars = bars_call
        self.get_stock_latest_trade = lambda request: {}


class _TradingClient:
    def get_clock(self) -> dict[str, Any]:
        return {}


def _market(bars_call: Any, sleep: Any = None) -> AlpacaMarketData:
    return AlpacaMarketData(
        _data_client=_DataClient(bars_call),
        _trading_client=_TradingClient(),
        _sleep=sleep or (lambda _delay: None),
        _retry_policy=_RETRY_POLICY,
        _now=lambda: datetime(2026, 10, 5, 13, 30, tzinfo=UTC),
    )


# --- Alpaca ----------------------------------------------------------------


def test_ac8_passes_start_at_midnight_utc_to_the_request() -> None:
    bars = _Capturing({"SPY": [_raw_bar()]})

    result = _market(bars).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Ok)
    assert len(bars.requests) == 1
    start = bars.requests[0].start
    assert start.replace(tzinfo=UTC) == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def test_ac8_returns_all_bars_oldest_first() -> None:
    raw = {
        "SPY": [
            _raw_bar(t="2026-09-03T04:00:00Z"),
            _raw_bar(t="2026-09-01T04:00:00Z"),
            _raw_bar(t="2026-09-02T04:00:00Z"),
        ]
    }

    result = _market(_Capturing(raw)).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Ok)
    assert [b.date.isoformat() for b in result.value] == ["2026-09-01", "2026-09-02", "2026-09-03"]


def test_ac8_empty_bars_for_known_ticker_is_ok() -> None:
    result = _market(_Capturing({"SPY": []})).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Ok)
    assert result.value == ()


def test_ac8_missing_ticker_key_is_err() -> None:
    result = _market(_Capturing({})).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Err)


def test_ac8_rejects_negative_price_without_leaking_the_value() -> None:
    raw = {"SPY": [_raw_bar(c=-5.0)]}

    result = _market(_Capturing(raw)).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Err)
    assert "-5" not in result.error.message


def test_ac8_rejects_high_below_low_without_leaking_the_value() -> None:
    raw = {"SPY": [_raw_bar(h=8.0, lo=9.5)]}

    result = _market(_Capturing(raw)).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Err)
    assert "8.0" not in result.error.message


def test_ac8_retries_and_succeeds_after_two_failures() -> None:
    flaky = _Flaky([RuntimeError("net"), RuntimeError("net"), {"SPY": [_raw_bar()]}])
    sleeps: list[Decimal] = []

    result = _market(flaky, sleep=sleeps.append).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Ok)
    assert flaky.calls == 3
    assert sleeps == [Decimal("0.5"), Decimal("1")]


def test_ac8_gives_up_with_err_after_three_failures_without_leaking_secret() -> None:
    secret = "super-secret-value"  # noqa: S105
    flaky = _Flaky([RuntimeError(f"auth {secret}") for _ in range(3)])

    result = _market(flaky).daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Err)
    assert flaky.calls == 3
    assert secret not in result.error.message


class _SessionRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, *_args: object, **kwargs: object) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {}


class _SdkClient:
    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self._session = _SessionRecorder()

    def get_stock_bars(self, request: Any) -> dict[str, Any]:
        self._session.request("GET", "/bars")
        return {"SPY": [_raw_bar()]}


def test_ac8_timeout_is_applied_to_the_bars_session() -> None:
    market = AlpacaMarketData.create(
        SecretStr("k"),
        SecretStr("s"),
        mode=TradingMode.paper,
        data_client_factory=_SdkClient,
        trading_client_factory=_SdkClient,
        sleep=lambda _delay: None,
        timeout_s=17,
    )

    result = market.daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Ok)
    assert market._data_client._session.calls[-1]["timeout"] == 17


# --- Fake ------------------------------------------------------------------


def _bar(day: date, ticker: str = "SPY") -> Bar:
    return Bar(
        ticker=ticker,
        date=day,
        open=Price(Decimal("10")),
        high=Price(Decimal("11")),
        low=Price(Decimal("9")),
        close=Price(Decimal("10.5")),
        volume=1000,
    )


def _fake(*days: date) -> FakeMarketData:
    clock = MarketClock(
        is_open=True,
        next_open=datetime(2026, 9, 2, 13, 30, tzinfo=UTC),
        next_close=datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
    )
    return FakeMarketData(prices={}, bars={"SPY": tuple(_bar(d) for d in days)}, clock=clock)


def test_ac8_fake_drops_bars_before_start_and_keeps_start_day() -> None:
    fake = _fake(date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2))

    result = fake.daily_bars_since("SPY", date(2026, 9, 1))

    assert isinstance(result, Ok)
    assert [b.date for b in result.value] == [date(2026, 9, 1), date(2026, 9, 2)]


def test_ac8_fake_unknown_ticker_is_err() -> None:
    assert isinstance(_fake(date(2026, 9, 1)).daily_bars_since("QQQ", date(2026, 9, 1)), Err)


def test_ac8_fake_failing_daily_bars_since_is_err_but_daily_bars_is_not() -> None:
    fake = _fake(date(2026, 9, 1)).failing(["daily_bars_since"])

    assert isinstance(fake.daily_bars_since("SPY", date(2026, 9, 1)), Err)
    assert isinstance(fake.daily_bars("SPY"), Ok)
