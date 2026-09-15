"""T-9: Alpaca order/position/account responses are validated at the boundary,
and rejection messages never leak the raw offending values (AC-25, N-5, N-6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from trader.broker.alpaca_broker import AlpacaBroker
from trader.config.mode import TradingMode
from trader.domain.result import Err, Ok


def _raw_order(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "order-1",
        "client_order_id": "client-1",
        "status": "filled",
        "filled_qty": "7",
        "filled_avg_price": "10.5",
        "updated_at": datetime(2024, 1, 2, tzinfo=UTC).isoformat(),
    }
    base.update(overrides)
    return base


class _FakeTradingClient:
    def __init__(self, get_order_by_client_id: Any = None) -> None:
        self.get_order_by_client_id = get_order_by_client_id or (lambda client_id: _raw_order())


def _broker(client: Any) -> AlpacaBroker:
    result = AlpacaBroker.create(
        TradingMode.paper,
        SecretStr("trade-key"),
        SecretStr("trade-secret"),
        client_factory=lambda **_kwargs: client,
        sleep=lambda _delay: None,
    )
    assert isinstance(result, Ok)
    return result.value


def test_negative_filled_qty_is_rejected_without_leaking_the_value() -> None:
    raw = _raw_order(filled_qty="-3")
    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Err)
    assert "-3" not in result.error.message


def test_unknown_status_is_rejected_without_leaking_the_value() -> None:
    raw = _raw_order(status="something_unheard_of")

    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Err)
    assert "something_unheard_of" not in result.error.message


def test_missing_id_is_rejected() -> None:
    raw = _raw_order()
    del raw["id"]

    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Err)
    assert "id" in result.error.message


def test_negative_filled_avg_price_is_rejected_without_leaking_the_value() -> None:
    raw = _raw_order(filled_avg_price="-10.5")

    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Err)
    assert "-10.5" not in result.error.message


def test_non_integer_filled_qty_is_rejected() -> None:
    raw = _raw_order(filled_qty="1.5")
    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Err)
    assert "1.5" not in result.error.message


def test_filled_avg_price_none_is_accepted_for_an_unfilled_order() -> None:
    raw = _raw_order(status="new", filled_qty="0", filled_avg_price=None)
    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Ok)
    assert result.value.filled_avg_price is None
    assert result.value.filled_qty.shares == 0


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("new", "submitted"),
        ("accepted", "submitted"),
        ("pending_new", "submitted"),
        ("partially_filled", "partially_filled"),
        ("filled", "filled"),
        ("canceled", "canceled"),
        ("expired", "canceled"),
        ("rejected", "rejected"),
    ],
)
def test_known_statuses_map_to_the_expected_domain_status(raw_status: str, expected: str) -> None:
    raw = _raw_order(status=raw_status, filled_qty="0", filled_avg_price=None)
    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Ok)
    assert result.value.status.value == expected


def test_unknown_status_foo_is_rejected_without_leaking_the_value() -> None:
    raw = _raw_order(status="foo")
    broker = _broker(_FakeTradingClient(get_order_by_client_id=lambda client_id: raw))

    result = broker.get_order("client-1")

    assert isinstance(result, Err)
    assert "foo" not in result.error.message


def test_valid_response_is_accepted() -> None:
    broker = _broker(_FakeTradingClient())

    result = broker.get_order("client-1")

    assert isinstance(result, Ok)
    assert result.value.status.value == "filled"
    assert result.value.filled_qty.shares == 7
