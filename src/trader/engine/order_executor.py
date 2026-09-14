"""Order execution (R-16): decide-then-execute in the correct order.

Record before submit: `execute` writes the decision (if not already
persisted), the approval, and the order row (`status=recorded`) inside one
SQLite transaction (`ledger.db.transaction`), and only *after* that
transaction commits does it call `Broker.submit_market_order`. If any
insert fails, the whole transaction rolls back and the broker is never
called (R-16, AC-15). If the broker submission itself fails, the order is
marked `failed` with `last_error` set and is never retried (N-4) -- a human
or the next cycle decides what to do next.

`execute` is the *only* place in `src/trader` allowed to call
`Broker.submit_market_order` (R-6, AC-8; enforced statically by
`tests/unit/engine/test_no_bypass.py`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from trader.broker.broker import Broker, OrderRequest
from trader.config.mode import TradingMode
from trader.domain.clock import Clock
from trader.domain.models import Order, OrderStatus
from trader.domain.result import Err, Ok, Result
from trader.engine.approval import ApprovedOrderRequest
from trader.ledger.db import transaction
from trader.ledger.repository import (
    get_decision,
    insert_approval,
    insert_decision,
    insert_order,
    update_order_status,
)


@dataclass(frozen=True, slots=True)
class ExecError:
    """A human-readable execution error (N-6)."""

    message: str


class _RecordError(RuntimeError):
    """Raised inside the transaction to trigger a rollback."""


def execute(
    req: ApprovedOrderRequest,
    conn: sqlite3.Connection,
    broker: Broker,
    clock: Clock,
    mode: TradingMode,
) -> Result[Order, ExecError]:
    """Record `req` (decision/approval/order), then submit it to `broker` (R-16).

    `req.decision` may already be persisted (e.g. a rule-exit decision
    written earlier in the same cycle) -- this checks `get_decision` first
    and skips the insert rather than failing on a duplicate-key error.
    """
    order_id = f"order_{req.decision.id}"
    order = Order(
        id=order_id,
        decision_id=req.decision.id,
        approval_id=req.approval.id,
        client_order_id=req.client_order_id,
        broker_order_id=None,
        mode=mode.value,
        side=req.side,
        qty=req.qty,
        order_type="market",
        status=OrderStatus.recorded,
        submitted_at=None,
        last_error=None,
    )

    try:
        with transaction(conn) as tx:
            if get_decision(tx, req.decision.id) is None:
                decision_result = insert_decision(tx, req.decision)
                if isinstance(decision_result, Err):
                    raise _RecordError(decision_result.error.message)

            approval_result = insert_approval(tx, req.approval)
            if isinstance(approval_result, Err):
                raise _RecordError(approval_result.error.message)

            order_result = insert_order(tx, order)
            if isinstance(order_result, Err):
                raise _RecordError(order_result.error.message)
    except (_RecordError, sqlite3.Error) as exc:
        return Err(ExecError(str(exc)))

    broker_req = OrderRequest(
        client_order_id=req.client_order_id,
        ticker=req.decision.ticker,
        side=req.side,
        qty=req.qty,
    )
    submit_result = broker.submit_market_order(broker_req)

    if isinstance(submit_result, Err):
        failure_message = submit_result.error.message
        update_result = update_order_status(
            conn, order_id, OrderStatus.failed, last_error=failure_message
        )
        if isinstance(update_result, Err):
            return Err(ExecError(update_result.error.message))
        return Err(ExecError(failure_message))

    broker_order = submit_result.value
    submitted_at = clock.now()
    update_result = update_order_status(
        conn,
        order_id,
        OrderStatus.submitted,
        broker_order_id=broker_order.broker_order_id,
        submitted_at=submitted_at,
    )
    if isinstance(update_result, Err):
        return Err(ExecError(update_result.error.message))

    return Ok(
        replace(
            order,
            status=OrderStatus.submitted,
            broker_order_id=broker_order.broker_order_id,
            submitted_at=submitted_at,
        )
    )
