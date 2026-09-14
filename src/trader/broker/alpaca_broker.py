"""`AlpacaBroker`: `Broker` backed by the Alpaca Trading API (R-14, N-3, N-4).

Design constraints enforced here (see plan T-9 / risk 1):

- The base URL is chosen from `mode` alone (paper vs live) and is never a
  caller-supplied argument -- `create`'s signature has no parameter whose
  name contains "url" (verified statically by
  `tests/unit/broker/test_alpaca_urls.py` via `inspect.signature`).
- Only a *trade* key/secret can construct this broker (read-only keys have
  no way to submit orders even if a caller tried).
- `alpaca-py`'s `RESTClient` retries 429/504 internally using a real
  `time.sleep` that is *not* constructor-injectable, and does so for every
  HTTP verb including POST. Left enabled, that would silently retry order
  submission and violate N-4 (POST must never be retried; double-submission
  risk). We disable it by overriding the constructed client's `_retry`
  attribute to `0` after construction (the `retry_attempts=0` constructor
  argument does *not* work: `RESTClient.__init__` only honors
  `retry_attempts` when it is truthy, so `0` is silently ignored and the
  default of 3 stays in effect -- confirmed by reading
  `alpaca.common.rest.RESTClient.__init__`). GET calls get their own
  自前 retry via `RetryPolicy`/`backoff_delays`, with `self._sleep` injected
  so tests never actually wait.
- This module is a boundary (N-5): raw SDK responses arrive as `Any` and are
  validated with pydantic before anything leaves this module as a domain
  type. Money-shaped fields are declared `Decimal` with a `mode="before"`
  validator that does `Decimal(str(x))` -- never a `float` annotation or
  `float()` call (AC-23; `broker/` is one of the directories
  `scripts/check_no_float_money.py` scans).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

from trader.broker.broker import (
    BrokerAccount,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from trader.config.mode import TradingMode
from trader.domain.models import OrderStatus, Side
from trader.domain.money import Money, Price, Quantity, to_canonical
from trader.domain.result import Err, Ok, Result
from trader.domain.retry import RetryPolicy, backoff_delays

_GET_RETRY_POLICY = RetryPolicy(
    max_attempts=3, base_delay_s=Decimal("0.5"), max_delay_s=Decimal("4")
)
_DEFAULT_TIMEOUT_S = 10

_PAPER_URL = "https://paper-api.alpaca.markets"
_LIVE_URL = "https://api.alpaca.markets"

_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.submitted,
    "accepted": OrderStatus.submitted,
    "pending_new": OrderStatus.submitted,
    "partially_filled": OrderStatus.partially_filled,
    "filled": OrderStatus.filled,
    "canceled": OrderStatus.canceled,
    "expired": OrderStatus.canceled,
    "rejected": OrderStatus.rejected,
}


def _default_sleep(delay_s: Decimal) -> None:
    import time

    # `time.sleep` accepts anything implementing `__float__` (which `Decimal`
    # does); passing it directly avoids an explicit `float()` call here,
    # which `scripts/check_no_float_money.py` forbids in this directory.
    time.sleep(delay_s)  # type: ignore[arg-type]


def _url_for_mode(mode: TradingMode) -> str:
    return _PAPER_URL if mode is TradingMode.paper else _LIVE_URL


def _install_timeout(client: Any, timeout_s: int) -> None:
    """Make the injected SDK client's HTTP session default to `timeout_s`.

    Same approach as `market/alpaca_data.py`: wrap the `requests.Session`
    that `alpaca.common.rest.RESTClient` builds internally, rather than
    using signal/thread based timeouts.
    """
    session = getattr(client, "_session", None)
    if session is None:
        return
    original_request = session.request

    def _timed_request(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout_s)
        return original_request(*args, **kwargs)

    session.request = _timed_request


def _disable_internal_retry(client: Any) -> None:
    """Force `alpaca-py`'s internal 429/504 retry off (see module docstring)."""
    if hasattr(client, "_retry"):
        client._retry = 0


def _to_decimal(value: Any) -> Any:
    if value is None:
        return None
    return Decimal(str(value))


class _RawOrder(BaseModel):
    """A raw Alpaca order response (`raw_data=True`)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    client_order_id: str
    status: str
    filled_qty: Decimal
    filled_avg_price: Decimal | None = None
    updated_at: datetime

    @field_validator("filled_qty", "filled_avg_price", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Any:
        return _to_decimal(value)

    @field_validator("filled_qty")
    @classmethod
    def _filled_qty_non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("filled_avg_price")
    @classmethod
    def _price_non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("must not be negative")
        return value


class _RawPosition(BaseModel):
    """A raw Alpaca position response (`raw_data=True`)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal

    @field_validator("qty", "avg_entry_price", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Any:
        return _to_decimal(value)


class _RawAccount(BaseModel):
    """A raw Alpaca account response (`raw_data=True`)."""

    model_config = ConfigDict(extra="ignore")

    cash: Decimal
    equity: Decimal

    @field_validator("cash", "equity", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Any:
        return _to_decimal(value)


def _validation_summary(exc: ValidationError) -> str:
    """A human-readable, value-free summary of a pydantic `ValidationError` (N-6)."""
    fields = sorted({str(error["loc"][-1]) for error in exc.errors() if error["loc"]})
    if not fields:
        return "invalid response"
    return "invalid fields: " + ", ".join(fields)


def _quantity_from_decimal(value: Decimal) -> Result[Quantity, BrokerError]:
    parsed = Quantity.parse(to_canonical(value))
    if isinstance(parsed, Err):
        return Err(BrokerError("broker returned a non-integer share quantity"))
    return Ok(parsed.value)


def _to_broker_order(raw: Any) -> Result[BrokerOrder, BrokerError]:
    try:
        parsed = _RawOrder.model_validate(raw)
    except ValidationError as exc:
        return Err(BrokerError(f"invalid order response: {_validation_summary(exc)}"))

    status = _STATUS_MAP.get(parsed.status)
    if status is None:
        return Err(BrokerError("invalid order response: unrecognized status"))

    qty_result = _quantity_from_decimal(parsed.filled_qty)
    if isinstance(qty_result, Err):
        return qty_result

    return Ok(
        BrokerOrder(
            broker_order_id=parsed.id,
            client_order_id=parsed.client_order_id,
            status=status,
            filled_qty=qty_result.value,
            filled_avg_price=(
                Price(parsed.filled_avg_price) if parsed.filled_avg_price is not None else None
            ),
            updated_at=parsed.updated_at,
        )
    )


def _to_broker_position(raw: Any) -> Result[BrokerPosition, BrokerError]:
    try:
        parsed = _RawPosition.model_validate(raw)
    except ValidationError as exc:
        return Err(BrokerError(f"invalid position response: {_validation_summary(exc)}"))

    qty_result = _quantity_from_decimal(parsed.qty)
    if isinstance(qty_result, Err):
        return qty_result

    return Ok(
        BrokerPosition(
            ticker=parsed.symbol,
            qty=qty_result.value,
            avg_cost=Price(parsed.avg_entry_price),
        )
    )


def _to_broker_account(raw: Any) -> Result[BrokerAccount, BrokerError]:
    try:
        parsed = _RawAccount.model_validate(raw)
    except ValidationError as exc:
        return Err(BrokerError(f"invalid account response: {_validation_summary(exc)}"))
    return Ok(BrokerAccount(cash=Money(parsed.cash), equity=Money(parsed.equity)))


@dataclass(frozen=True, slots=True)
class AlpacaBroker:
    """`Broker` backed by `alpaca.trading.client.TradingClient`."""

    _trading_client: Any
    _sleep: Callable[[Decimal], None]
    _retry_policy: RetryPolicy

    @classmethod
    def create(
        cls,
        mode: TradingMode,
        trade_key: SecretStr,
        trade_secret: SecretStr,
        *,
        client_factory: Callable[..., Any] = TradingClient,
        sleep: Callable[[Decimal], None] = _default_sleep,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> Result[AlpacaBroker, BrokerError]:
        """Build an `AlpacaBroker` from *trade* credentials only.

        The base URL is derived from `mode` and cannot be overridden by the
        caller (N-3). A read-only key pair cannot construct this broker: an
        empty trade key or secret is rejected.
        """
        if not trade_key.get_secret_value() or not trade_secret.get_secret_value():
            return Err(BrokerError("AlpacaBroker requires a non-empty trade key and secret"))

        client = client_factory(
            api_key=trade_key.get_secret_value(),
            secret_key=trade_secret.get_secret_value(),
            paper=mode is TradingMode.paper,
            raw_data=True,
            url_override=_url_for_mode(mode),
        )
        _disable_internal_retry(client)
        _install_timeout(client, timeout_s)
        return Ok(
            cls(
                _trading_client=client,
                _sleep=sleep,
                _retry_policy=_GET_RETRY_POLICY,
            )
        )

    def submit_market_order(self, req: OrderRequest) -> Result[BrokerOrder, BrokerError]:
        """Submit a market order exactly once; never retried (N-4)."""
        order_data = MarketOrderRequest(
            symbol=req.ticker,
            qty=req.qty.shares,
            side=AlpacaOrderSide.BUY if req.side is Side.buy else AlpacaOrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=req.client_order_id,
        )
        try:
            raw = self._trading_client.submit_order(order_data)
        except Exception as exc:
            return Err(BrokerError(f"submit_market_order failed: {type(exc).__name__}"))
        return _to_broker_order(raw)

    def cancel_all_open(self) -> Result[int, BrokerError]:
        """Cancel every open order once (no retry; the call is idempotent enough
        on its own that retrying it is unnecessary risk)."""
        try:
            raw = self._trading_client.cancel_orders()
        except Exception as exc:
            return Err(BrokerError(f"cancel_all_open failed: {type(exc).__name__}"))
        if not isinstance(raw, list):
            return Err(BrokerError("cancel_all_open returned an unexpected response shape"))
        return Ok(len(raw))

    def get_order(self, client_order_id: str) -> Result[BrokerOrder, BrokerError]:
        raw_result = self._call_with_retry(
            lambda: self._trading_client.get_order_by_client_id(client_order_id),
            op=f"get_order({client_order_id})",
        )
        if isinstance(raw_result, Err):
            return raw_result
        return _to_broker_order(raw_result.value)

    def positions(self) -> Result[tuple[BrokerPosition, ...], BrokerError]:
        raw_result = self._call_with_retry(self._trading_client.get_all_positions, op="positions")
        if isinstance(raw_result, Err):
            return raw_result
        raw_value = raw_result.value
        if not isinstance(raw_value, list):
            return Err(BrokerError("positions returned an unexpected response shape"))
        positions: list[BrokerPosition] = []
        for raw_position in raw_value:
            position_result = _to_broker_position(raw_position)
            if isinstance(position_result, Err):
                return position_result
            positions.append(position_result.value)
        return Ok(tuple(positions))

    def account(self) -> Result[BrokerAccount, BrokerError]:
        raw_result = self._call_with_retry(self._trading_client.get_account, op="account")
        if isinstance(raw_result, Err):
            return raw_result
        return _to_broker_account(raw_result.value)

    def _call_with_retry(self, call: Callable[[], Any], *, op: str) -> Result[Any, BrokerError]:
        """Retry `call` on any exception, following `_retry_policy`'s backoff.

        Only used for idempotent GET calls (N-4). `self._sleep` is invoked
        (not `time.sleep` directly) so tests never actually wait.
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
            BrokerError(
                f"{op} failed after {self._retry_policy.max_attempts} attempts: "
                f"{type(last_error).__name__}"
            )
        )
