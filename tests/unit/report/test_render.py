"""AC-33 (format check): `render_report` surfaces rationale, exit reason,
realized P&L, and holding days for every trade, and doesn't break with zero
trades."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from trader.domain.models import Action, Decision, ExitReason, Origin, RuleCheck, Trade
from trader.domain.money import Money, Price
from trader.report.live_estimate import LiveEstimate
from trader.report.metrics import Metrics, Period
from trader.report.render import render_report

_NOW = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
_PERIOD = Period(start=date(2026, 1, 1), end=date(2026, 1, 31))

_ZERO = Money(Decimal("0"))
_ESTIMATE = LiveEstimate(
    paper_pnl=Money(Decimal("12.00")),
    slippage_cost=Money(Decimal("0.75")),
    commission_cost=_ZERO,
    fixed_commission_cost=_ZERO,
    fx_cost=Money(Decimal("10.00")),
    estimated_live_pnl=Money(Decimal("1.25")),
)


def _metrics() -> Metrics:
    return Metrics(
        period_pnl=Money(Decimal("12.00")),
        trade_count=1,
        win_count=1,
        loss_count=0,
        win_rate_pct=Decimal("100.00"),
        avg_pnl=Money(Decimal("12.00")),
        max_drawdown_pct=Decimal("0.00"),
        exit_reason_counts={ExitReason.stop_loss: 1},
        decision_counts={"passed": 1},
        open_positions=(),
    )


def _entry_decision() -> Decision:
    return Decision(
        id="dec-entry",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="Serenity mentions rising fast over the last 14 days, thesis intact.",
        evidence_mention_ids=("m-1", "m-2", "m-3"),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="notional 70.00 <= limit 75.00",
        proposed_notional=Money(Decimal("70.00")),
        reference_price=Price(Decimal("10")),
    )


def _trade() -> Trade:
    return Trade(
        id="dec-entry:dec-exit",
        ticker="ABCD",
        opened_at=_NOW,
        closed_at=_NOW,
        entry_decision_id="dec-entry",
        exit_decision_ids=("dec-exit",),
        exit_reason=ExitReason.stop_loss,
        realized_pnl=Money(Decimal("-14.00")),
        fees=Money(Decimal("0")),
        holding_days=3,
    )


def test_render_includes_rationale_exit_reason_pnl_and_holding_days() -> None:
    trade = _trade()
    entry_decision = _entry_decision()

    output = render_report(_metrics(), _ESTIMATE, (trade,), {"dec-entry": entry_decision}, _PERIOD)

    assert entry_decision.rationale[:50] in output
    assert "evidence 3 件" in output
    assert "stop_loss" in output
    assert "-14.00" in output
    assert "保有日数: 3" in output


def test_render_with_zero_trades_does_not_break() -> None:
    output = render_report(_metrics(), _ESTIMATE, (), {}, _PERIOD)

    assert "trader report" in output
    assert "該当期間のトレードなし" in output


def test_render_missing_entry_decision_still_renders_trade() -> None:
    trade = _trade()

    output = render_report(_metrics(), _ESTIMATE, (trade,), {}, _PERIOD)

    assert "ABCD" in output
    assert "見つかりません" in output
