"""`FakeBroker`: an in-memory `Broker` for tests (T-9).

Configuration (`fail_next_submit`, `with_fill_plan`, `with_open_orders`, ...)
returns a *new* `FakeBroker` instance so the original is never mutated
(N-7 for the public surface). The one place that cannot be frozen top to
bottom is the progression of `get_order()` calls for a single order (each
call is supposed to reveal the next simulated fill) -- that needs some
counter that survives across calls *on the same instance*. This is
implemented with a small internal, non-frozen `_FakeBrokerState` holder
that each `FakeBroker` owns exclusively: the configuration methods build a
shallow copy of that holder (so the original instance's state is
untouched) and only the *new* instance's own state is ever mutated in
place afterwards (by `submit_market_order` / `get_order`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from trader.broker.broker import (
    BrokerAccount,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from trader.domain.models import OrderStatus
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok, Result

_ZERO_ACCOUNT = BrokerAccount(cash=Money(Decimal(0)), equity=Money(Decimal(0)))


@dataclass(slots=True)
class _FakeBrokerState:
    """Mutable state owned by exactly one `FakeBroker` instance."""

    submitted: list[OrderRequest] = field(default_factory=list)
    fail_next_submit: bool = False
    fill_plans: dict[str, tuple[tuple[Quantity, Price], ...]] = field(default_factory=dict)
    call_counts: dict[str, int] = field(default_factory=dict)
    orders: dict[str, OrderRequest] = field(default_factory=dict)
    open_order_count: int = 0
    account_value: BrokerAccount = field(default_factory=lambda: _ZERO_ACCOUNT)
    positions_value: tuple[BrokerPosition, ...] = ()

    def copy(self) -> _FakeBrokerState:
        return _FakeBrokerState(
            submitted=list(self.submitted),
            fail_next_submit=self.fail_next_submit,
            fill_plans=dict(self.fill_plans),
            call_counts=dict(self.call_counts),
            orders=dict(self.orders),
            open_order_count=self.open_order_count,
            account_value=self.account_value,
            positions_value=self.positions_value,
        )


def _order_from_fills(
    req: OrderRequest, filled_qty: Quantity, filled_avg_price: Price | None
) -> BrokerOrder:
    status = OrderStatus.submitted
    if filled_qty.shares > 0:
        status = (
            OrderStatus.filled
            if filled_qty.shares >= req.qty.shares
            else OrderStatus.partially_filled
        )
    return BrokerOrder(
        broker_order_id=f"broker_{req.client_order_id}",
        client_order_id=req.client_order_id,
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        updated_at=datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class FakeBroker:
    """In-memory `Broker` used by tests instead of `AlpacaBroker`."""

    _state: _FakeBrokerState = field(default_factory=_FakeBrokerState)

    def fail_next_submit(self) -> FakeBroker:
        """Return a new `FakeBroker` whose next `submit_market_order` call fails."""
        new_state = self._state.copy()
        new_state.fail_next_submit = True
        return FakeBroker(_state=new_state)

    def with_fill_plan(
        self, client_order_id: str, fills: tuple[tuple[Quantity, Price], ...]
    ) -> FakeBroker:
        """Return a new `FakeBroker` where `get_order(client_order_id)` reveals
        one more (cumulative) entry of `fills` on each successive call."""
        new_state = self._state.copy()
        new_state.fill_plans[client_order_id] = fills
        return FakeBroker(_state=new_state)

    def with_open_orders(self, n: int) -> FakeBroker:
        """Return a new `FakeBroker` whose `cancel_all_open()` reports `n`."""
        new_state = self._state.copy()
        new_state.open_order_count = n
        return FakeBroker(_state=new_state)

    def with_account(self, account: BrokerAccount) -> FakeBroker:
        new_state = self._state.copy()
        new_state.account_value = account
        return FakeBroker(_state=new_state)

    def with_positions(self, positions: tuple[BrokerPosition, ...]) -> FakeBroker:
        new_state = self._state.copy()
        new_state.positions_value = positions
        return FakeBroker(_state=new_state)

    @property
    def submitted(self) -> tuple[OrderRequest, ...]:
        return tuple(self._state.submitted)

    @property
    def submit_call_count(self) -> int:
        return len(self._state.submitted)

    def submit_market_order(self, req: OrderRequest) -> Result[BrokerOrder, BrokerError]:
        state = self._state
        if state.fail_next_submit:
            state.fail_next_submit = False
            return Err(BrokerError("simulated submit failure"))
        state.submitted.append(req)
        state.orders[req.client_order_id] = req
        state.call_counts.setdefault(req.client_order_id, 0)
        return Ok(_order_from_fills(req, Quantity(0), None))

    def cancel_all_open(self) -> Result[int, BrokerError]:
        return Ok(self._state.open_order_count)

    def get_order(self, client_order_id: str) -> Result[BrokerOrder, BrokerError]:
        state = self._state
        req = state.orders.get(client_order_id)
        if req is None:
            return Err(BrokerError(f"no such order: {client_order_id}"))

        count = state.call_counts.get(client_order_id, 0) + 1
        state.call_counts[client_order_id] = count

        plan = state.fill_plans.get(client_order_id, ())
        revealed = plan[:count]

        filled_shares = min(sum((qty.shares for qty, _price in revealed), 0), req.qty.shares)
        filled_qty = Quantity(filled_shares)
        filled_avg_price = revealed[-1][1] if revealed else None

        return Ok(_order_from_fills(req, filled_qty, filled_avg_price))

    def positions(self) -> Result[tuple[BrokerPosition, ...], BrokerError]:
        return Ok(self._state.positions_value)

    def account(self) -> Result[BrokerAccount, BrokerError]:
        return Ok(self._state.account_value)
