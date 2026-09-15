"""`Broker` protocol: the only shape order execution is allowed to take (R-14).

`engine/order_executor.py` is the sole caller of `submit_market_order` (R-16,
AC-8) -- this module only defines the interface and the request/response
types that cross it. No implementation detail (Alpaca or otherwise) leaks
into this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.domain.models import OrderStatus, Side
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Result


@dataclass(frozen=True, slots=True)
class BrokerError:
    """A human-readable broker error.

    `message` must never contain key material or raw response bodies -- only
    a description safe to log and show to a human (N-6).
    """

    message: str


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A market order to submit, keyed by the caller's idempotency token."""

    client_order_id: str
    ticker: str
    side: Side
    qty: Quantity


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """The broker's view of an order (submitted, filled, canceled, ...)."""

    broker_order_id: str
    client_order_id: str
    status: OrderStatus
    filled_qty: Quantity
    filled_avg_price: Price | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """The broker's view of one open position."""

    ticker: str
    qty: Quantity
    avg_cost: Price


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    """The broker's view of account-level cash and equity."""

    cash: Money
    equity: Money


class Broker(Protocol):
    """Behind-the-interface order execution and account state (R-14)."""

    def submit_market_order(self, req: OrderRequest) -> Result[BrokerOrder, BrokerError]:
        """Submit a market order exactly once. Never retried (N-4)."""
        ...

    def cancel_all_open(self) -> Result[int, BrokerError]:
        """Cancel every open order; return how many were attempted (R-19)."""
        ...

    def get_order(self, client_order_id: str) -> Result[BrokerOrder, BrokerError]:
        """Look up an order by the caller's idempotency token (GET, retried)."""
        ...

    def positions(self) -> Result[tuple[BrokerPosition, ...], BrokerError]:
        """Return all open positions (GET, retried)."""
        ...

    def account(self) -> Result[BrokerAccount, BrokerError]:
        """Return account cash/equity (GET, retried)."""
        ...
