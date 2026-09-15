"""T-9: POST is never retried, GET is retried with backoff, timeouts are wired (AC-24)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import SecretStr

from trader.broker.alpaca_broker import AlpacaBroker
from trader.broker.broker import OrderRequest
from trader.config.mode import TradingMode
from trader.domain.models import Side
from trader.domain.money import Quantity
from trader.domain.result import Err, Ok


def _raw_order(
    order_id: str = "order-1",
    client_order_id: str = "client-1",
    status: str = "new",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
) -> dict[str, Any]:
    return {
        "id": order_id,
        "client_order_id": client_order_id,
        "status": status,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "updated_at": datetime(2024, 1, 2, tzinfo=UTC).isoformat(),
    }


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


class _FakeTradingClient:
    def __init__(
        self,
        submit_order: Any = None,
        get_order_by_client_id: Any = None,
        get_all_positions: Any = None,
        get_account: Any = None,
        cancel_orders: Any = None,
        session: Any = None,
    ) -> None:
        self.submit_order = submit_order or (lambda order_data: _raw_order())
        self.get_order_by_client_id = get_order_by_client_id or (lambda client_id: _raw_order())
        self.get_all_positions = get_all_positions or (lambda: [])
        self.get_account = get_account or (lambda: {"cash": "100", "equity": "150"})
        self.cancel_orders = cancel_orders or (lambda: [])
        if session is not None:
            self._session = session


def _broker(client: Any = None, sleep: Any = None) -> AlpacaBroker:
    result = AlpacaBroker.create(
        TradingMode.paper,
        SecretStr("trade-key"),
        SecretStr("trade-secret"),
        client_factory=lambda **_kwargs: client or _FakeTradingClient(),
        sleep=sleep or (lambda _delay: None),
    )
    assert isinstance(result, Ok)
    return result.value


def _req(client_order_id: str = "client-1") -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id, ticker="AAPL", side=Side.buy, qty=Quantity(7)
    )


def test_submit_market_order_is_not_retried_on_exception() -> None:
    flaky = _FlakyCall([RuntimeError("net"), _raw_order()])
    sleeps: list[Decimal] = []
    broker = _broker(client=_FakeTradingClient(submit_order=flaky), sleep=sleeps.append)

    result = broker.submit_market_order(_req())

    assert isinstance(result, Err)
    assert flaky.calls == 1
    assert sleeps == []


def test_submit_market_order_is_not_retried_on_invalid_response() -> None:
    """An unparseable response is a permanent failure, not a retry trigger:
    the SDK call must still only happen once (AC-24)."""
    flaky = _FlakyCall([{"not": "a valid order"}])
    sleeps: list[Decimal] = []
    broker = _broker(client=_FakeTradingClient(submit_order=flaky), sleep=sleeps.append)

    result = broker.submit_market_order(_req())

    assert isinstance(result, Err)
    assert flaky.calls == 1
    assert sleeps == []


def test_submit_market_order_passes_client_order_id_side_qty_and_day_tif() -> None:
    captured: list[Any] = []

    def submit_order(order_data: Any) -> Any:
        captured.append(order_data)
        return _raw_order(client_order_id=order_data.client_order_id)

    broker = _broker(client=_FakeTradingClient(submit_order=submit_order))

    result = broker.submit_market_order(_req(client_order_id="client-xyz"))

    assert isinstance(result, Ok)
    assert len(captured) == 1
    sent = captured[0]
    assert sent.client_order_id == "client-xyz"
    assert sent.symbol == "AAPL"
    assert sent.qty == 7
    assert sent.side.value == "buy"
    assert sent.time_in_force.value == "day"


def test_get_order_retries_and_succeeds_after_two_failures() -> None:
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), _raw_order()])
    sleeps: list[Decimal] = []
    broker = _broker(client=_FakeTradingClient(get_order_by_client_id=flaky), sleep=sleeps.append)

    result = broker.get_order("client-1")

    assert isinstance(result, Ok)
    assert flaky.calls == 3
    assert sleeps == [Decimal("0.5"), Decimal("1")]


def test_get_order_gives_up_after_max_attempts() -> None:
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), RuntimeError("net")])
    broker = _broker(
        client=_FakeTradingClient(get_order_by_client_id=flaky), sleep=lambda _delay: None
    )

    result = broker.get_order("client-1")

    assert isinstance(result, Err)
    assert flaky.calls == 3


def test_positions_retries_and_succeeds_after_two_failures() -> None:
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), []])
    sleeps: list[Decimal] = []
    broker = _broker(client=_FakeTradingClient(get_all_positions=flaky), sleep=sleeps.append)

    result = broker.positions()

    assert isinstance(result, Ok)
    assert flaky.calls == 3
    assert sleeps == [Decimal("0.5"), Decimal("1")]


def test_positions_gives_up_after_max_attempts() -> None:
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), RuntimeError("net")])
    broker = _broker(client=_FakeTradingClient(get_all_positions=flaky), sleep=lambda _d: None)

    result = broker.positions()

    assert isinstance(result, Err)
    assert flaky.calls == 3


def test_account_retries_and_succeeds_after_two_failures() -> None:
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), {"cash": "100", "equity": "150"}])
    sleeps: list[Decimal] = []
    broker = _broker(client=_FakeTradingClient(get_account=flaky), sleep=sleeps.append)

    result = broker.account()

    assert isinstance(result, Ok)
    assert flaky.calls == 3
    assert sleeps == [Decimal("0.5"), Decimal("1")]


def test_account_gives_up_after_max_attempts() -> None:
    flaky = _FlakyCall([RuntimeError("net"), RuntimeError("net"), RuntimeError("net")])
    broker = _broker(client=_FakeTradingClient(get_account=flaky), sleep=lambda _d: None)

    result = broker.account()

    assert isinstance(result, Err)
    assert flaky.calls == 3


def test_cancel_all_open_is_not_retried_on_exception() -> None:
    flaky = _FlakyCall([RuntimeError("net"), []])
    sleeps: list[Decimal] = []
    broker = _broker(client=_FakeTradingClient(cancel_orders=flaky), sleep=sleeps.append)

    result = broker.cancel_all_open()

    assert isinstance(result, Err)
    assert flaky.calls == 1
    assert sleeps == []


def test_timeout_is_installed_on_the_client_session() -> None:
    calls: list[dict[str, Any]] = []

    class _Session:
        def request(self, *args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return None

    session = _Session()
    result = AlpacaBroker.create(
        TradingMode.paper,
        SecretStr("trade-key"),
        SecretStr("trade-secret"),
        client_factory=lambda **_kwargs: _FakeTradingClient(session=session),
        timeout_s=7,
    )
    assert isinstance(result, Ok)
    session.request("GET", "https://example.invalid")
    assert calls[0]["timeout"] == 7


def test_internal_retry_attribute_is_disabled_after_construction() -> None:
    class _ClientWithRetry(_FakeTradingClient):
        def __init__(self) -> None:
            super().__init__()
            self._retry = 3

    client = _ClientWithRetry()
    result = AlpacaBroker.create(
        TradingMode.paper,
        SecretStr("trade-key"),
        SecretStr("trade-secret"),
        client_factory=lambda **_kwargs: client,
    )
    assert isinstance(result, Ok)
    assert client._retry == 0
