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


def test_AC20_same_day_close_has_zero_holding_days() -> None:
    """AC-20: a position opened and closed on the same calendar day."""
    opened_at = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
    closed_at = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)
    opened = apply_fill(
        None, _fill("f1", 2, "10", filled_at=opened_at), _decision("dec-buy"), Side.buy, opened_at
    ).value
    position = opened.position

    result = apply_fill(
        position,
        _fill("f2", 2, "11", filled_at=closed_at),
        _decision("dec-sell", action=Action.sell),
        Side.sell,
        closed_at,
        ExitReason.stop_loss,
        entry_decision_id="dec-buy",
    )

    assert is_ok(result)
    update = result.value
    assert isinstance(update, ClosedTrade)
    assert update.trade.holding_days == 0


def test_AC20_holding_days_uses_calendar_date_not_elapsed_hours() -> None:
    """Crossing a UTC midnight boundary counts as 1 calendar day even though
    less than 24 hours elapsed between the fills."""
    opened_at = datetime(2026, 1, 5, 23, 50, tzinfo=UTC)
    closed_at = datetime(2026, 1, 6, 0, 10, tzinfo=UTC)
    opened = apply_fill(
        None, _fill("f1", 2, "10", filled_at=opened_at), _decision("dec-buy"), Side.buy, opened_at
    ).value
    position = opened.position

    result = apply_fill(
        position,
        _fill("f2", 2, "11", filled_at=closed_at),
        _decision("dec-sell", action=Action.sell),
        Side.sell,
        closed_at,
        ExitReason.stop_loss,
        entry_decision_id="dec-buy",
    )

    assert is_ok(result)
    update = result.value
    assert isinstance(update, ClosedTrade)
    # only 20 minutes elapsed, but the calendar date advanced by one
    assert update.trade.holding_days == 1


def test_AC20_fees_accumulate_across_partial_sells() -> None:
    """Two 0.50 fees across a partial take-profit and a trailing-stop sell
    accumulate to 1.00 on the closed trade."""
    opened = apply_fill(None, _fill("f1", 10, "10"), _decision("dec-buy"), Side.buy, _NOW).value
    position = opened.position

    partial_decision = _decision("dec-partial", action=Action.sell)
    partial_update = apply_fill(
        position,
        _fill("f2", 5, "13", fee="0.50"),
        partial_decision,
        Side.sell,
        _NOW,
        ExitReason.partial_take_profit,
        entry_decision_id="dec-buy",
    ).value
    assert isinstance(partial_update, OpenPosition)

    trailing_decision = _decision("dec-trailing", action=Action.sell)
    final_result = apply_fill(
        partial_update.position,
        _fill("f3", 5, "12", fee="0.50"),
        trailing_decision,
        Side.sell,
        _NOW,
        ExitReason.trailing_stop,
        entry_decision_id="dec-buy",
        exit_decision_ids=(partial_decision.id,),
        realized_so_far=Money(Decimal("14.50")),  # (13-10)*5 - 0.50
        fees_so_far=Money(Decimal("0.50")),
    )

    assert is_ok(final_result)
    trade = final_result.value.trade  # type: ignore[union-attr]
    assert trade.fees == Money(Decimal("1.00"))
    assert trade.exit_decision_ids == ("dec-partial", "dec-trailing")


def test_AC20_additional_buy_computes_weighted_average_cost_to_four_places() -> None:
    """7 @10 then 3 @12 averages to 10.6000, quantized to 4 places, and
    retains the first fill's opened_at."""
    opened = apply_fill(None, _fill("f1", 7, "10"), _decision("dec-buy-1"), Side.buy, _NOW).value
    position = opened.position

    result = apply_fill(position, _fill("f2", 3, "12"), _decision("dec-buy-2"), Side.buy, _NOW)

    assert is_ok(result)
    update = result.value
    assert isinstance(update, OpenPosition)
    # (7*10 + 3*12) / 10 = 10.6000
    assert update.position.avg_cost == Price(Decimal("10.6000"))
    assert update.position.qty == Quantity(10)
    assert update.position.opened_at == _NOW


def test_AC20_high_watermark_is_the_max_seen_price_after_an_additional_buy() -> None:
    """A second buy above the current high_watermark should raise it, not
    silently keep the pre-buy value (trailing-stop math depends on this)."""
    opened = apply_fill(None, _fill("f1", 7, "10"), _decision("dec-buy-1"), Side.buy, _NOW).value
    position = opened.position
    assert position.high_watermark == Price(Decimal("10"))

    result = apply_fill(position, _fill("f2", 3, "12"), _decision("dec-buy-2"), Side.buy, _NOW)

    assert is_ok(result)
    update = result.value
    assert isinstance(update, OpenPosition)
    assert update.position.high_watermark == Price(Decimal("12"))


def test_AC20_fee_can_turn_a_profitable_price_move_into_a_loss() -> None:
    """price > avg_cost, but the fee outweighs the small per-share gain."""
    opened = apply_fill(None, _fill("f1", 1, "10"), _decision("dec-buy"), Side.buy, _NOW).value
    position = opened.position

    result = apply_fill(
        position,
        _fill("f2", 1, "10.05", fee="1.00"),
        _decision("dec-sell", action=Action.sell),
        Side.sell,
        _NOW,
        ExitReason.max_holding_days,
        entry_decision_id="dec-buy",
    )

    assert is_ok(result)
    update = result.value
    assert isinstance(update, ClosedTrade)
    # (10.05 - 10) * 1 - 1.00 = -0.95
    assert update.trade.realized_pnl == Money(Decimal("-0.95"))


def test_AC20_zero_qty_fill_is_a_pinned_no_op_not_a_silent_error() -> None:
    """Current documented behavior: a zero-quantity sell fill does not
    reduce the position and only subtracts its fee. This test pins that
    decision so a future change to reject zero-qty fills is a deliberate
    one, not an accidental regression."""
    opened = apply_fill(None, _fill("f1", 5, "10"), _decision("dec-buy"), Side.buy, _NOW).value
    position = opened.position

    result = apply_fill(
        position,
        _fill("f2", 0, "10", fee="0.10"),
        _decision("dec-sell", action=Action.sell),
        Side.sell,
        _NOW,
        ExitReason.stop_loss,
    )

    assert is_ok(result)
    update = result.value
    assert isinstance(update, OpenPosition)
    assert update.position.qty == Quantity(5)


def test_AC20_result_is_a_new_object_and_input_position_is_unchanged() -> None:
    """apply_fill must not mutate the `Position` passed in (N-7 immutability)."""
    opened = apply_fill(None, _fill("f1", 7, "10"), _decision("dec-buy-1"), Side.buy, _NOW).value
    original_position = opened.position

    result = apply_fill(
        original_position, _fill("f2", 3, "12"), _decision("dec-buy-2"), Side.buy, _NOW
    )

    assert is_ok(result)
    update = result.value
    assert isinstance(update, OpenPosition)
    assert update.position is not original_position
    assert original_position.qty == Quantity(7)
    assert original_position.avg_cost == Price(Decimal("10"))
