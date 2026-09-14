"""Approval resolution (R-17): who is allowed to authorize an order.

`resolve_approval` is the only place a rule-checked `Decision` becomes an
`ApprovedOrderRequest` -- the type `engine.order_executor.execute` requires.
`ApprovedOrderRequest` cannot be built any other way: its `_token` field is
checked in `__post_init__` against `_TOKEN`, a sentinel private to this
module, so external code (and `dataclasses.replace`, which re-runs
`__post_init__`) gets a `TypeError` instead of a usable instance. Combined
with `resolve_approval` refusing any decision whose `rule_check` is not
`passed` (or whose `action` is not `buy`/`sell`), this closes the loop
against LLM proposals reaching a broker without going through the rule
engine (R-6, AC-8).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Final

from trader.config.mode import TradingMode
from trader.domain.clock import Clock
from trader.domain.models import Action, Approval, Approver, Decision, RuleCheck, Side
from trader.domain.money import Quantity
from trader.domain.result import Err, Ok, Result
from trader.ledger.db import transaction
from trader.ledger.repository import find_valid_approval, get_decision, insert_approval
from trader.market.data_provider import MarketClock


@dataclass(frozen=True, slots=True)
class ApprovalError:
    """A human-readable reason an order could not be approved (N-6)."""

    message: str


_TOKEN: Final[object] = object()


@dataclass(frozen=True, slots=True)
class ApprovedOrderRequest:
    """A rule-checked, approved order ready for `order_executor.execute`.

    Only constructible via `resolve_approval` in this module (see module
    docstring). `client_order_id` is always `order_{decision.id}` so a
    decision can never produce two different client order ids.
    """

    decision: Decision
    approval: Approval
    side: Side
    qty: Quantity
    client_order_id: str
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise TypeError(
                "ApprovedOrderRequest can only be constructed by "
                "trader.engine.approval.resolve_approval (R-6, AC-8)"
            )


def _make(
    decision: Decision, approval: Approval, side: Side, qty: Quantity
) -> ApprovedOrderRequest:
    return ApprovedOrderRequest(
        decision=decision,
        approval=approval,
        side=side,
        qty=qty,
        client_order_id=f"order_{decision.id}",
        _token=_TOKEN,
    )


def _order_side(decision: Decision) -> Result[Side, ApprovalError]:
    if decision.action is Action.buy:
        return Ok(Side.buy)
    if decision.action is Action.sell:
        return Ok(Side.sell)
    return Err(ApprovalError(f"decision {decision.id} action '{decision.action}' is not buy/sell"))


def _order_qty(decision: Decision) -> Result[Quantity, ApprovalError]:
    """Derive the share count from `decision.proposed_notional` / `reference_price`.

    Both fields are set by the rule engine when it produces a `passed`
    buy/sell decision (`rules.entry_checks.check_entry` /
    `rules.exit_checks.check_exit`, floor division to whole shares -- same
    derivation as `EntryPlan.qty`). A `passed` buy/sell decision without
    both fields is a caller bug, not a normal input-validation case.
    """
    if decision.proposed_notional is None or decision.reference_price is None:
        return Err(
            ApprovalError(
                f"decision {decision.id} has no proposed_notional/reference_price to size an order"
            )
        )
    if decision.reference_price.amount <= 0:
        return Err(ApprovalError(f"decision {decision.id} reference_price must be positive"))
    shares = int(decision.proposed_notional.amount // decision.reference_price.amount)
    if shares <= 0:
        return Err(ApprovalError(f"decision {decision.id} sizes to zero shares"))
    return Ok(Quantity(shares))


def resolve_approval(
    decision: Decision,
    mode: TradingMode,
    conn: sqlite3.Connection,
    clock: Clock,
    market_clock: MarketClock,
) -> Result[ApprovedOrderRequest, ApprovalError]:
    """Resolve (paper) or look up (live) the approval for `decision` (R-17).

    Refuses immediately (before touching `conn`) unless
    `decision.rule_check is RuleCheck.passed` and `decision.action` is
    `buy`/`sell` -- a rejected/skip/hold decision can never produce an
    `ApprovedOrderRequest`.

    `expires_at` is always `market_clock.next_close`: per Alpaca's clock
    semantics, when the market is open this is *today's* close, and when it
    is closed this is already the *next* session's close (spec R-17
    "承認時刻が市場時間内ならその日の大引け、時間外なら翌営業日の大引け") --
    so passing `market_clock.next_close` straight through is correct in
    both cases without branching on `market_clock.is_open` here.

    - `paper`: creates (but does not yet persist -- `order_executor.execute`
      does that inside its transaction) an `approver=system` approval.
    - `live`: requires an existing, unexpired `approver=kyo` approval
      (`ledger.repository.find_valid_approval`); an expired or missing
      approval is `Err`.
    """
    if decision.rule_check is not RuleCheck.passed:
        return Err(ApprovalError(f"decision {decision.id} did not pass rule checks"))

    side_result = _order_side(decision)
    if isinstance(side_result, Err):
        return side_result
    side = side_result.value

    qty_result = _order_qty(decision)
    if isinstance(qty_result, Err):
        return qty_result
    qty = qty_result.value

    now = clock.now()

    if mode is TradingMode.paper:
        approval = Approval(
            id=f"appr_{uuid.uuid4().hex}",
            decision_id=decision.id,
            approver=Approver.system,
            approved_at=now,
            expires_at=market_clock.next_close,
        )
        return Ok(_make(decision, approval, side, qty))

    existing_approval = find_valid_approval(conn, decision.id, now)
    if existing_approval is None:
        return Err(
            ApprovalError(f"no valid (unexpired, kyo-approved) approval for decision {decision.id}")
        )
    if existing_approval.approver is not Approver.kyo:
        return Err(ApprovalError(f"approval for decision {decision.id} is not from kyo"))
    return Ok(_make(decision, existing_approval, side, qty))


def record_human_approval(
    conn: sqlite3.Connection,
    decision_id: str,
    clock: Clock,
    market_clock: MarketClock,
) -> Result[Approval, ApprovalError]:
    """Record a `kyo` approval for `decision_id` (CLI `approve`, R-17).

    Requires the decision to exist and to have `rule_check == passed`.
    `market_clock.next_close` becomes `expires_at` -- the CLI builds a
    `MarketClock` directly from the operator-supplied `--expires-at`
    instead of fetching a real one, since `approve` must not depend on the
    network (N-9).
    """
    decision = get_decision(conn, decision_id)
    if decision is None:
        return Err(ApprovalError(f"no such decision: {decision_id}"))
    if decision.rule_check is not RuleCheck.passed:
        return Err(ApprovalError(f"decision {decision_id} did not pass rule checks"))

    approval = Approval(
        id=f"appr_{uuid.uuid4().hex}",
        decision_id=decision_id,
        approver=Approver.kyo,
        approved_at=clock.now(),
        expires_at=market_clock.next_close,
    )

    try:
        with transaction(conn) as tx:
            insert_result = insert_approval(tx, approval)
            if isinstance(insert_result, Err):
                return Err(ApprovalError(insert_result.error.message))
    except sqlite3.Error as exc:
        return Err(ApprovalError(f"could not record approval: {exc}"))

    return Ok(approval)
