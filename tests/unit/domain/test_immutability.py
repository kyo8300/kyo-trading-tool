"""Every domain model and Money-family type is frozen (AC-26, N-7)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trader.domain.clock import FixedClock, SystemClock
from trader.domain.models import (
    Action,
    Approval,
    Approver,
    Decision,
    EquitySnapshot,
    Evidence,
    ExitReason,
    ExitSignal,
    Fill,
    Mention,
    MentionStats,
    Order,
    OrderStatus,
    Origin,
    Position,
    RuleCheck,
    Side,
    Trade,
)
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err, Ok
from trader.domain.retry import RetryPolicy

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_money_price_quantity_are_frozen() -> None:
    money = Money(Decimal("10"))
    price = Price(Decimal("10"))
    quantity = Quantity(1)
    with pytest.raises(FrozenInstanceError):
        money.amount = Decimal("20")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        price.amount = Decimal("20")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        quantity.shares = 2  # type: ignore[misc]


def test_result_variants_are_frozen() -> None:
    ok = Ok(1)
    err = Err("bad")
    with pytest.raises(FrozenInstanceError):
        ok.value = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        err.error = "worse"  # type: ignore[misc]


def test_clocks_are_frozen() -> None:
    system_clock = SystemClock()
    fixed_clock = FixedClock(NOW)
    with pytest.raises(FrozenInstanceError):
        fixed_clock.fixed_now = NOW  # type: ignore[misc]
    assert isinstance(system_clock.now(), datetime)


def test_retry_policy_is_frozen() -> None:
    policy = RetryPolicy(3, Decimal("0.5"), Decimal("4"))
    with pytest.raises(FrozenInstanceError):
        policy.max_attempts = 5  # type: ignore[misc]


def test_mention_evidence_and_stats_are_frozen() -> None:
    mention = Mention(
        id="m1",
        source_id="serenity",
        external_id="123",
        ticker="ABCD",
        posted_at=NOW,
        text_excerpt="hello",
        url=None,
        raw_sha256="deadbeef",
    )
    stats = MentionStats(
        ticker="ABCD",
        first_seen_at=NOW,
        last_seen_at=NOW,
        mention_count=1,
        mention_count_last_14d=1,
        mention_count_prior_14d=0,
    )
    evidence = Evidence(ticker="ABCD", stats=stats, excerpts=("hello",), mention_ids=("m1",))
    with pytest.raises(FrozenInstanceError):
        mention.ticker = "WXYZ"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        stats.mention_count = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evidence.ticker = "WXYZ"  # type: ignore[misc]


def test_decision_and_related_records_are_frozen() -> None:
    decision = Decision(
        id="d1",
        cycle_id="c1",
        decided_at=NOW,
        mode="paper",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="strong mentions",
        evidence_mention_ids=("m1",),
        llm_model="claude",
        prompt_sha256="p",
        response_sha256="r",
        rule_set_sha256="rs",
        rule_check=RuleCheck.passed,
        rule_check_reason="ok",
        proposed_notional=Money(Decimal("75")),
        reference_price=Price(Decimal("10")),
    )
    approval = Approval(
        id="a1", decision_id="d1", approver=Approver.system, approved_at=NOW, expires_at=NOW
    )
    order = Order(
        id="o1",
        decision_id="d1",
        approval_id="a1",
        client_order_id="order_d1",
        broker_order_id=None,
        mode="paper",
        side=Side.buy,
        qty=Quantity(7),
        order_type="market",
        status=OrderStatus.recorded,
        submitted_at=None,
        last_error=None,
    )
    fill = Fill(
        id="f1",
        order_id="o1",
        filled_at=NOW,
        qty=Quantity(7),
        price=Price(Decimal("10")),
        fee=Money(Decimal("0")),
    )
    position = Position(
        ticker="ABCD",
        qty=Quantity(7),
        avg_cost=Price(Decimal("10")),
        opened_at=NOW,
        high_watermark=Price(Decimal("10")),
        partial_tp_done=False,
    )
    trade = Trade(
        id="t1",
        ticker="ABCD",
        opened_at=NOW,
        closed_at=NOW,
        entry_decision_id="d1",
        exit_decision_ids=("d2",),
        exit_reason=ExitReason.stop_loss,
        realized_pnl=Money(Decimal("-14")),
        fees=Money(Decimal("0")),
        holding_days=1,
    )
    snapshot = EquitySnapshot(
        snapshot_date="2026-01-01",
        mode="paper",
        cash=Money(Decimal("500")),
        positions_value=Money(Decimal("0")),
        equity=Money(Decimal("500")),
        peak_equity=Money(Decimal("500")),
        drawdown_pct=Decimal("0"),
        taken_at=NOW,
    )
    exit_signal = ExitSignal(
        ticker="ABCD",
        reason=ExitReason.stop_loss,
        fraction=Decimal("1"),
        trigger_price=Price(Decimal("8.5")),
    )

    for obj, field, value in (
        (decision, "rationale", "changed"),
        (approval, "approver", Approver.kyo),
        (order, "status", OrderStatus.filled),
        (fill, "qty", Quantity(1)),
        (position, "qty", Quantity(1)),
        (trade, "holding_days", 2),
        (snapshot, "equity", Money(Decimal("1"))),
        (exit_signal, "fraction", Decimal("0.5")),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)
