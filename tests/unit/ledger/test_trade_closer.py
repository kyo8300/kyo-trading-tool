"""T-11 / AC-20: fills close positions into trades with realized P&L, exit
reason, fees, and holding days.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trader.domain.models import (
    Action,
    Decision,
    ExitReason,
    Fill,
    Origin,
    RuleCheck,
    Side,
)
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import is_err, is_ok
from trader.ledger.trade_closer import ClosedTrade, OpenPosition, apply_fill

_NOW = datetime(2026, 1, 20, 15, 0, tzinfo=UTC)


def _decision(
    decision_id: str,
    ticker: str = "ABCD",
    action: Action = Action.buy,
    origin: Origin = Origin.llm,
    decided_at: datetime = _NOW,
) -> Decision:
    return Decision(
        id=decision_id,
        cycle_id="cycle-1",
        decided_at=decided_at,
        mode="paper",
        ticker=ticker,
        action=action,
        origin=origin,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=("m-1",),
        llm_model="claude-sonnet-5" if origin is Origin.llm else None,
        prompt_sha256="p" * 64 if origin is Origin.llm else None,
        response_sha256="r" * 64 if origin is Origin.llm else None,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="ok",
        proposed_notional=Money(Decimal("70.00")),
        reference_price=Price(Decimal("10")),
    )


def _fill(fill_id: str, qty: int, price: str, fee: str = "0", filled_at: datetime = _NOW) -> Fill:
    return Fill(
        id=fill_id,
        order_id=f"order-{fill_id}",
        filled_at=filled_at,
        qty=Quantity(qty),
        price=Price(Decimal(price)),
        fee=Money(Decimal(fee)),
    )


def test_buy_opens_a_new_position() -> None:
    result = apply_fill(None, _fill("f1", 7, "10"), _decision("dec-buy"), Side.buy, _NOW)

    assert is_ok(result)
    update = result.value
    assert isinstance(update, OpenPosition)
    assert update.position.ticker == "ABCD"
    assert update.position.qty == Quantity(7)
    assert update.position.avg_cost == Price(Decimal("10"))
    assert update.position.opened_at == _NOW
    assert update.position.high_watermark == Price(Decimal("10"))
    assert update.position.partial_tp_done is False


def test_additional_buy_recomputes_weighted_average_cost() -> None:
    opened = apply_fill(None, _fill("f1", 7, "10"), _decision("dec-buy-1"), Side.buy, _NOW).value
    position = opened.position

    result = apply_fill(position, _fill("f2", 3, "20"), _decision("dec-buy-2"), Side.buy, _NOW)

    assert is_ok(result)
    update = result.value
    assert isinstance(update, OpenPosition)
    # (7*10 + 3*20) / 10 = 13.00
    assert update.position.qty == Quantity(10)
    assert update.position.avg_cost == Price(Decimal("13.0000"))
    # opened_at stays the first fill's timestamp
    assert update.position.opened_at == _NOW


def test_full_stop_loss_sell_closes_trade_with_expected_pnl_and_holding_days() -> None:
    opened_at = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    closed_at = datetime(2026, 1, 8, 15, 0, tzinfo=UTC)
    opened = apply_fill(
        None, _fill("f1", 7, "10", filled_at=opened_at), _decision("dec-buy"), Side.buy, opened_at
    ).value
    position = opened.position

    exit_decision = _decision("dec-sell", action=Action.sell)
    result = apply_fill(
        position,
        _fill("f2", 7, "8", filled_at=closed_at),
        exit_decision,
        Side.sell,
        closed_at,
        ExitReason.stop_loss,
        entry_decision_id="dec-buy",
    )

    assert is_ok(result)
    update = result.value
    assert isinstance(update, ClosedTrade)
    trade = update.trade
    assert trade.realized_pnl == Money(Decimal("-14.00"))
    assert trade.exit_reason == ExitReason.stop_loss
    assert trade.holding_days == (closed_at.date() - opened_at.date()).days
    assert trade.entry_decision_id == "dec-buy"
    assert trade.exit_decision_ids == ("dec-sell",)
    assert trade.fees == Money(Decimal("0"))


def test_partial_take_profit_then_trailing_stop_accumulates_pnl_and_exit_ids() -> None:
    opened = apply_fill(None, _fill("f1", 10, "10"), _decision("dec-buy"), Side.buy, _NOW).value
    position = opened.position

    partial_decision = _decision("dec-partial", action=Action.sell)
    partial_result = apply_fill(
        position,
        _fill("f2", 5, "13", fee="1.00"),
        partial_decision,
        Side.sell,
        _NOW,
        ExitReason.partial_take_profit,
        entry_decision_id="dec-buy",
    )
    assert is_ok(partial_result)
    partial_update = partial_result.value
    assert isinstance(partial_update, OpenPosition)
    remaining_position = partial_update.position
    assert remaining_position.qty == Quantity(5)
    assert remaining_position.partial_tp_done is True
    assert remaining_position.avg_cost == Price(Decimal("10"))

    # realized so far from the partial sell: (13-10)*5 - 1.00 = 14.00
    partial_pnl = Money((Decimal("13") - Decimal("10")) * 5) - Money(Decimal("1.00"))
    assert partial_pnl == Money(Decimal("14.00"))

    trailing_decision = _decision("dec-trailing", action=Action.sell)
    final_result = apply_fill(
        remaining_position,
        _fill("f3", 5, "12"),
        trailing_decision,
        Side.sell,
        _NOW,
        ExitReason.trailing_stop,
        entry_decision_id="dec-buy",
        exit_decision_ids=(partial_decision.id,),
        realized_so_far=partial_pnl,
        fees_so_far=Money(Decimal("1.00")),
    )

    assert is_ok(final_result)
    final_update = final_result.value
    assert isinstance(final_update, ClosedTrade)
    trade = final_update.trade
    # 14.00 (partial) + (12-10)*5 = 24.00... plus the second fill has no fee
    assert trade.realized_pnl == Money(Decimal("24.00"))
    assert trade.fees == Money(Decimal("1.00"))
    assert trade.exit_decision_ids == ("dec-partial", "dec-trailing")
    assert trade.exit_reason == ExitReason.trailing_stop


def test_partial_take_profit_fraction_example_from_plan_sums_to_25() -> None:
    """10 shares @10 -> 5 @13 (partial, no fee) -> 5 @12 (trailing, no fee)."""
    opened = apply_fill(None, _fill("f1", 10, "10"), _decision("dec-buy"), Side.buy, _NOW).value
    position = opened.position

    partial_decision = _decision("dec-partial", action=Action.sell)
    partial_update = apply_fill(
        position,
        _fill("f2", 5, "13"),
        partial_decision,
        Side.sell,
        _NOW,
        ExitReason.partial_take_profit,
    ).value
    assert isinstance(partial_update, OpenPosition)

    trailing_decision = _decision("dec-trailing", action=Action.sell)
    final_update = apply_fill(
        partial_update.position,
        _fill("f3", 5, "12"),
        trailing_decision,
        Side.sell,
        _NOW,
        ExitReason.trailing_stop,
        entry_decision_id="dec-buy",
        exit_decision_ids=(partial_decision.id,),
        # (13-10)*5 = 15.00 realized by the partial sell (no fee)
        realized_so_far=Money(Decimal("15.00")),
    ).value

    assert isinstance(final_update, ClosedTrade)
    assert final_update.trade.realized_pnl == Money(Decimal("25.00"))


def test_llm_origin_sell_with_no_rule_exit_reason_is_recorded_as_llm() -> None:
    opened = apply_fill(None, _fill("f1", 4, "10"), _decision("dec-buy"), Side.buy, _NOW).value
    position = opened.position

    llm_sell_decision = _decision("dec-llm-sell", action=Action.sell, origin=Origin.llm)
    result = apply_fill(
        position,
        _fill("f2", 4, "11"),
        llm_sell_decision,
        Side.sell,
        _NOW,
        entry_decision_id="dec-buy",
    )

    assert is_ok(result)
    update = result.value
    assert isinstance(update, ClosedTrade)
    assert update.trade.exit_reason == ExitReason.llm


def test_selling_more_than_held_is_an_error() -> None:
    opened = apply_fill(None, _fill("f1", 3, "10"), _decision("dec-buy"), Side.buy, _NOW).value
    position = opened.position

    result = apply_fill(
        position,
        _fill("f2", 4, "10"),
        _decision("dec-oversell", action=Action.sell),
        Side.sell,
        _NOW,
    )

    assert is_err(result)


def test_selling_without_a_position_is_an_error() -> None:
    result = apply_fill(
        None,
        _fill("f1", 1, "10"),
        _decision("dec-sell-no-position", action=Action.sell),
        Side.sell,
        _NOW,
    )

    assert is_err(result)
