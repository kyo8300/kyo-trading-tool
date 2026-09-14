"""Decisions / approvals / orders / fills: the decision-and-execution trail
required by R-16, R-18, R-20.

Every query uses `?` placeholders; SQL is never built by string
concatenation or f-strings (security.md). Write functions do not commit --
callers compose them inside `trader.ledger.db.transaction()` so that, e.g.,
a decision + approval + order can be recorded atomically before an order is
submitted to the broker (R-16).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.domain.models import (
    Action,
    Approval,
    Approver,
    Decision,
    Fill,
    Order,
    OrderStatus,
    Origin,
    RuleCheck,
    Side,
)
from trader.domain.money import Money, Price, Quantity, from_canonical, to_canonical
from trader.domain.result import Err, Ok, Result
from trader.ledger.db import LedgerError


class LedgerCorruptionError(RuntimeError):
    """A stored row could not be parsed back into its domain type.

    This indicates the database was written by something other than this
    repository (or was hand-edited) -- it is a bug/tamper signal, not a
    normal input-validation failure, so it is raised rather than returned
    as a `Result`.
    """


def _dt_to_text(value: datetime) -> str:
    return value.isoformat()


def _dt_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _money_to_text(value: Money | None) -> str | None:
    return to_canonical(value.amount) if value is not None else None


def _price_to_text(value: Price | None) -> str | None:
    return to_canonical(value.amount) if value is not None else None


def _decimal_to_text(value: Decimal | None) -> str | None:
    return to_canonical(value) if value is not None else None


def _decimal_or_raise(value: str) -> Decimal:
    result = from_canonical(value)
    if isinstance(result, Err):
        raise LedgerCorruptionError(f"stored decimal '{value}' is not canonical")
    return result.value


def _money_or_none(value: str | None) -> Money | None:
    return Money(_decimal_or_raise(value)) if value is not None else None


def _price_or_none(value: str | None) -> Price | None:
    return Price(_decimal_or_raise(value)) if value is not None else None


def _quantity_or_raise(value: str) -> Quantity:
    result = Quantity.parse(value)
    if isinstance(result, Err):
        raise LedgerCorruptionError(f"stored quantity '{value}' is not a whole share count")
    return result.value


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"],
        cycle_id=row["cycle_id"],
        decided_at=_dt_from_text(row["decided_at"]),
        mode=row["mode"],
        ticker=row["ticker"],
        action=Action(row["action"]),
        origin=Origin(row["origin"]),
        confidence=_decimal_or_raise(row["confidence"]) if row["confidence"] is not None else None,
        rationale=row["rationale"],
        evidence_mention_ids=tuple(json.loads(row["evidence_mention_ids"])),
        llm_model=row["llm_model"],
        prompt_sha256=row["prompt_sha256"],
        response_sha256=row["response_sha256"],
        rule_set_sha256=row["rule_set_sha256"],
        rule_check=RuleCheck(row["rule_check"]),
        rule_check_reason=row["rule_check_reason"],
        proposed_notional=_money_or_none(row["proposed_notional"]),
        reference_price=_price_or_none(row["reference_price"]),
    )


def _row_to_approval(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        decision_id=row["decision_id"],
        approver=Approver(row["approver"]),
        approved_at=_dt_from_text(row["approved_at"]),
        expires_at=_dt_from_text(row["expires_at"]),
    )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        id=row["id"],
        decision_id=row["decision_id"],
        approval_id=row["approval_id"],
        client_order_id=row["client_order_id"],
        broker_order_id=row["broker_order_id"],
        mode=row["mode"],
        side=Side(row["side"]),
        qty=_quantity_or_raise(row["qty"]),
        order_type=row["order_type"],
        status=OrderStatus(row["status"]),
        submitted_at=_dt_from_text(row["submitted_at"]) if row["submitted_at"] else None,
        last_error=row["last_error"],
    )


def _row_to_fill(row: sqlite3.Row) -> Fill:
    return Fill(
        id=row["id"],
        order_id=row["order_id"],
        filled_at=_dt_from_text(row["filled_at"]),
        qty=_quantity_or_raise(row["qty"]),
        price=_price_or_none(row["price"]) or Price(Decimal(0)),
        fee=_money_or_none(row["fee"]) or Money(Decimal(0)),
    )


def insert_decision(conn: sqlite3.Connection, decision: Decision) -> Result[Decision, LedgerError]:
    """Persist one decision row. Skips and rejections are recorded too (R-20)."""
    try:
        conn.execute(
            """
            INSERT INTO decisions (
                id, cycle_id, decided_at, mode, ticker, action, origin, confidence,
                rationale, evidence_mention_ids, llm_model, prompt_sha256,
                response_sha256, rule_set_sha256, rule_check, rule_check_reason,
                proposed_notional, reference_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.id,
                decision.cycle_id,
                _dt_to_text(decision.decided_at),
                decision.mode,
                decision.ticker,
                decision.action.value,
                decision.origin.value,
                _decimal_to_text(decision.confidence),
                decision.rationale,
                json.dumps(list(decision.evidence_mention_ids)),
                decision.llm_model,
                decision.prompt_sha256,
                decision.response_sha256,
                decision.rule_set_sha256,
                decision.rule_check.value,
                decision.rule_check_reason,
                _money_to_text(decision.proposed_notional),
                _price_to_text(decision.reference_price),
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert decision: {exc}"))
    return Ok(decision)


def insert_approval(conn: sqlite3.Connection, approval: Approval) -> Result[Approval, LedgerError]:
    """Persist one approval row (R-17)."""
    try:
        conn.execute(
            """
            INSERT INTO approvals (id, decision_id, approver, approved_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                approval.id,
                approval.decision_id,
                approval.approver.value,
                _dt_to_text(approval.approved_at),
                _dt_to_text(approval.expires_at),
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert approval: {exc}"))
    return Ok(approval)


def insert_order(conn: sqlite3.Connection, order: Order) -> Result[Order, LedgerError]:
    """Persist one order row, initially with `status=recorded` (R-16)."""
    try:
        conn.execute(
            """
            INSERT INTO orders (
                id, decision_id, approval_id, client_order_id, broker_order_id,
                mode, side, qty, order_type, status, submitted_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.id,
                order.decision_id,
                order.approval_id,
                order.client_order_id,
                order.broker_order_id,
                order.mode,
                order.side.value,
                to_canonical(Decimal(order.qty.shares)),
                order.order_type,
                order.status.value,
                _dt_to_text(order.submitted_at) if order.submitted_at else None,
                order.last_error,
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert order: {exc}"))
    return Ok(order)


def update_order_status(
    conn: sqlite3.Connection,
    order_id: str,
    status: OrderStatus,
    last_error: str | None = None,
    broker_order_id: str | None = None,
    submitted_at: datetime | None = None,
) -> Result[None, LedgerError]:
    """Transition an order's status, optionally recording an error, the
    broker-assigned id, or the submission time (R-16, R-18)."""
    try:
        cursor = conn.execute(
            """
            UPDATE orders
            SET status = ?,
                last_error = COALESCE(?, last_error),
                broker_order_id = COALESCE(?, broker_order_id),
                submitted_at = COALESCE(?, submitted_at)
            WHERE id = ?
            """,
            (
                status.value,
                last_error,
                broker_order_id,
                _dt_to_text(submitted_at) if submitted_at else None,
                order_id,
            ),
        )
        if cursor.rowcount == 0:
            return Err(LedgerError(f"no such order: {order_id}"))
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not update order: {exc}"))
    return Ok(None)


def insert_fill(conn: sqlite3.Connection, fill: Fill) -> Result[Fill, LedgerError]:
    """Persist one fill row; partial fills are multiple rows (R-18)."""
    try:
        conn.execute(
            """
            INSERT INTO fills (id, order_id, filled_at, qty, price, fee)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fill.id,
                fill.order_id,
                _dt_to_text(fill.filled_at),
                to_canonical(Decimal(fill.qty.shares)),
                to_canonical(fill.price.amount),
                to_canonical(fill.fee.amount),
            ),
        )
    except sqlite3.Error as exc:
        return Err(LedgerError(f"could not insert fill: {exc}"))
    return Ok(fill)


def list_fills(conn: sqlite3.Connection, order_id: str) -> tuple[Fill, ...]:
    """Return every fill for `order_id`, oldest first."""
    rows = conn.execute(
        "SELECT * FROM fills WHERE order_id = ? ORDER BY filled_at ASC", (order_id,)
    ).fetchall()
    return tuple(_row_to_fill(row) for row in rows)


def get_order(conn: sqlite3.Connection, order_id: str) -> Order | None:
    """Return the order with `order_id`, or `None` if it does not exist."""
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _row_to_order(row) if row is not None else None


def find_valid_approval(
    conn: sqlite3.Connection, decision_id: str, now: datetime
) -> Approval | None:
    """Return an unexpired approval for `decision_id`, if one exists (R-17)."""
    row = conn.execute(
        """
        SELECT * FROM approvals
        WHERE decision_id = ? AND expires_at > ?
        ORDER BY approved_at DESC
        LIMIT 1
        """,
        (decision_id, _dt_to_text(now)),
    ).fetchone()
    return _row_to_approval(row) if row is not None else None


def list_decisions(conn: sqlite3.Connection, cycle_id: str | None = None) -> tuple[Decision, ...]:
    """Return decisions, optionally filtered by `cycle_id`, newest first."""
    if cycle_id is None:
        rows = conn.execute("SELECT * FROM decisions ORDER BY decided_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE cycle_id = ? ORDER BY decided_at DESC", (cycle_id,)
        ).fetchall()
    return tuple(_row_to_decision(row) for row in rows)


def get_decision(conn: sqlite3.Connection, decision_id: str) -> Decision | None:
    """Return the decision with `decision_id`, or `None` if it does not exist."""
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return _row_to_decision(row) if row is not None else None


def list_orders(conn: sqlite3.Connection) -> tuple[Order, ...]:
    """Return every order, oldest first (T-14 report: live-estimate order count)."""
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY COALESCE(submitted_at, '') ASC, id ASC"
    ).fetchall()
    return tuple(_row_to_order(row) for row in rows)


def list_all_fills(conn: sqlite3.Connection) -> tuple[Fill, ...]:
    """Return every fill across every order, oldest first (T-14 report: live-estimate costs)."""
    rows = conn.execute("SELECT * FROM fills ORDER BY filled_at ASC").fetchall()
    return tuple(_row_to_fill(row) for row in rows)


@dataclass(frozen=True, slots=True)
class FillContext:
    """A `Fill` paired with its order's side and originating decision id.

    Used to replay a position's fill history across multiple `run_cycle`
    calls, since `positions` itself has no columns for the running entry
    decision, accumulated exit decisions, or accumulated realized P&L (R-21,
    see `ledger.trade_closer` module docstring).
    """

    fill: Fill
    side: Side
    decision_id: str


def list_fill_context_for_ticker_since(
    conn: sqlite3.Connection, ticker: str, since: datetime
) -> tuple[FillContext, ...]:
    """Return every fill for `ticker` at or after `since`, oldest first, each
    paired with its order's side and originating decision id (R-21, T-13)."""
    rows = conn.execute(
        """
        SELECT fills.id AS fill_id, fills.order_id AS fill_order_id,
               fills.filled_at AS fill_filled_at, fills.qty AS fill_qty,
               fills.price AS fill_price, fills.fee AS fill_fee,
               orders.side AS order_side, orders.decision_id AS order_decision_id
        FROM fills
        JOIN orders ON orders.id = fills.order_id
        JOIN decisions ON decisions.id = orders.decision_id
        WHERE decisions.ticker = ? AND fills.filled_at >= ?
        ORDER BY fills.filled_at ASC
        """,
        (ticker, _dt_to_text(since)),
    ).fetchall()
    return tuple(
        FillContext(
            fill=Fill(
                id=row["fill_id"],
                order_id=row["fill_order_id"],
                filled_at=_dt_from_text(row["fill_filled_at"]),
                qty=_quantity_or_raise(row["fill_qty"]),
                price=_price_or_none(row["fill_price"]) or Price(Decimal(0)),
                fee=_money_or_none(row["fill_fee"]) or Money(Decimal(0)),
            ),
            side=Side(row["order_side"]),
            decision_id=row["order_decision_id"],
        )
        for row in rows
    )
