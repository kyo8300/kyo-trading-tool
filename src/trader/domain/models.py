"""Frozen domain records mirroring the SQLite tables in the design (N-7).

These are plain data containers -- validation happens at the system
boundaries that produce them (sources/, rules/, analysis/, ledger/). Updates
never mutate an existing instance; use `dataclasses.replace` (or the small
helpers below) to get a new one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trader.domain.money import Money, Price, Quantity


class Side(StrEnum):
    buy = "buy"
    sell = "sell"


class Action(StrEnum):
    buy = "buy"
    sell = "sell"
    hold = "hold"
    skip = "skip"


class Origin(StrEnum):
    llm = "llm"
    rule_exit = "rule_exit"


class RuleCheck(StrEnum):
    passed = "passed"
    rejected = "rejected"


class OrderStatus(StrEnum):
    recorded = "recorded"
    submitted = "submitted"
    partially_filled = "partially_filled"
    filled = "filled"
    canceled = "canceled"
    rejected = "rejected"
    failed = "failed"


class Approver(StrEnum):
    system = "system"
    kyo = "kyo"


class ExitReason(StrEnum):
    stop_loss = "stop_loss"
    partial_take_profit = "partial_take_profit"
    trailing_stop = "trailing_stop"
    max_holding_days = "max_holding_days"
    llm = "llm"


@dataclass(frozen=True, slots=True)
class Mention:
    """A single normalized mention of a ticker in an aggregated source (R-4)."""

    id: str
    source_id: str
    external_id: str
    ticker: str
    posted_at: datetime
    text_excerpt: str
    url: str | None
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class MentionStats:
    """Per-ticker mention history derived from `Mention`s (R-4)."""

    ticker: str
    first_seen_at: datetime
    last_seen_at: datetime
    mention_count: int
    mention_count_last_14d: int
    mention_count_prior_14d: int


@dataclass(frozen=True, slots=True)
class Evidence:
    """What the analyst/LLM is shown for one ticker: stats + excerpts (Q4)."""

    ticker: str
    stats: MentionStats
    excerpts: tuple[str, ...]
    mention_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Decision:
    """One row of `decisions`: every judgement, including skips/rejections."""

    id: str
    cycle_id: str
    decided_at: datetime
    mode: str
    ticker: str
    action: Action
    origin: Origin
    confidence: Decimal | None
    rationale: str
    evidence_mention_ids: tuple[str, ...]
    llm_model: str | None
    prompt_sha256: str | None
    response_sha256: str | None
    rule_set_sha256: str
    rule_check: RuleCheck
    rule_check_reason: str
    proposed_notional: Money | None
    reference_price: Price | None


@dataclass(frozen=True, slots=True)
class Approval:
    """One row of `approvals` (R-17)."""

    id: str
    decision_id: str
    approver: Approver
    approved_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Order:
    """One row of `orders` (R-16)."""

    id: str
    decision_id: str
    approval_id: str
    client_order_id: str
    broker_order_id: str | None
    mode: str
    side: Side
    qty: Quantity
    order_type: str
    status: OrderStatus
    submitted_at: datetime | None
    last_error: str | None


def with_status(order: Order, status: OrderStatus) -> Order:
    """Return a copy of `order` with `status` updated."""
    return replace(order, status=status)


@dataclass(frozen=True, slots=True)
class Fill:
    """One row of `fills`; partial fills are multiple rows (R-18)."""

    id: str
    order_id: str
    filled_at: datetime
    qty: Quantity
    price: Price
    fee: Money


@dataclass(frozen=True, slots=True)
class Position:
    """One row of `positions`, derived from fills."""

    ticker: str
    qty: Quantity
    avg_cost: Price
    opened_at: datetime
    high_watermark: Price
    partial_tp_done: bool


@dataclass(frozen=True, slots=True)
class Trade:
    """One row of `trades`: recorded when a position closes (R-21)."""

    id: str
    ticker: str
    opened_at: datetime
    closed_at: datetime
    entry_decision_id: str
    exit_decision_ids: tuple[str, ...]
    exit_reason: ExitReason
    realized_pnl: Money
    fees: Money
    holding_days: int


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """One row of `equity_snapshots`, used for drawdown kill-switch checks."""

    snapshot_date: str
    mode: str
    cash: Money
    positions_value: Money
    equity: Money
    peak_equity: Money
    drawdown_pct: Decimal
    taken_at: datetime


@dataclass(frozen=True, slots=True)
class ExitSignal:
    """A rule-driven sell signal for a held position (R-13)."""

    ticker: str
    reason: ExitReason
    fraction: Decimal
    trigger_price: Price
