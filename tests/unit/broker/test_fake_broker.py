"""T-9: `FakeBroker` scenarios used by other tasks' tests."""

from __future__ import annotations

from decimal import Decimal

from trader.broker.broker import BrokerAccount, BrokerPosition, OrderRequest
from trader.broker.fake_broker import FakeBroker
from trader.domain.models import OrderStatus, Side
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok


def _req(client_order_id: str = "client-1", qty: int = 7) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id, ticker="AAPL", side=Side.buy, qty=Quantity(qty)
    )


def test_submit_records_the_request_and_returns_an_unfilled_order() -> None:
    broker = FakeBroker()

    result = broker.submit_market_order(_req())

    assert isinstance(result, Ok)
    assert result.value.status is OrderStatus.submitted
    assert broker.submitted == (_req(),)
    assert broker.submit_call_count == 1


def test_fail_next_submit_fails_only_the_next_call_and_does_not_mutate_the_original() -> None:
    original = FakeBroker()
    failing = original.fail_next_submit()

    fail_result = failing.submit_market_order(_req())
    assert isinstance(fail_result, Err)
    assert failing.submit_call_count == 0

    # the failing flag was consumed; a second submit on the same (new) instance succeeds
    ok_result = failing.submit_market_order(_req())
    assert isinstance(ok_result, Ok)

    # the original instance was never mutated by `fail_next_submit()` or by calls
    # made on the instance it returned
    original_result = original.submit_market_order(_req())
    assert isinstance(original_result, Ok)


def test_fill_plan_progresses_from_partially_filled_to_filled_across_get_order_calls() -> None:
    broker = FakeBroker().with_fill_plan(
        "client-1", ((Quantity(3), Price(Decimal("10"))), (Quantity(4), Price(Decimal("10"))))
    )
    submit_result = broker.submit_market_order(_req(qty=7))
    assert isinstance(submit_result, Ok)

    first = broker.get_order("client-1")
    assert isinstance(first, Ok)
    assert first.value.status is OrderStatus.partially_filled
    assert first.value.filled_qty.shares == 3

    second = broker.get_order("client-1")
    assert isinstance(second, Ok)
    assert second.value.status is OrderStatus.filled
    assert second.value.filled_qty.shares == 7


def test_get_order_for_unknown_client_order_id_is_err() -> None:
    broker = FakeBroker()

    result = broker.get_order("does-not-exist")

    assert isinstance(result, Err)


def test_with_open_orders_is_reported_by_cancel_all_open() -> None:
    broker = FakeBroker().with_open_orders(2)

    result = broker.cancel_all_open()

    assert isinstance(result, Ok)
    assert result.value == 2


def test_with_positions_and_with_account_are_returned_as_configured() -> None:
    position = BrokerPosition(ticker="AAPL", qty=Quantity(5), avg_cost=Price(Decimal("10")))
    account = BrokerAccount(cash=Money(Decimal("100")), equity=Money(Decimal("150")))
    broker = FakeBroker().with_positions((position,)).with_account(account)

    positions_result = broker.positions()
    account_result = broker.account()

    assert isinstance(positions_result, Ok)
    assert positions_result.value == (position,)
    assert isinstance(account_result, Ok)
    assert account_result.value == account
