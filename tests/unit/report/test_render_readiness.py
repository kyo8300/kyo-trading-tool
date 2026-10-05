"""AC-18/AC-19: the readiness section of `trader report` (LR-9..LR-11)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from trader.domain.money import Money
from trader.report.live_estimate import estimate
from trader.report.metrics import Period, compute
from trader.report.readiness import (
    BenchmarkPoint,
    ConditionResult,
    Readiness,
    Verdict,
)
from trader.report.render import render_readiness, render_report
from trader.rules.schema import CostAssumptions

_S = date(2026, 9, 18)
_E = date(2026, 10, 30)
_COST = CostAssumptions(
    slippage_pct_per_side=Decimal("0"),
    commission_pct=Decimal("0"),
    commission_usd_per_order=Decimal("0"),
    fx_cost_pct_one_way=Decimal("0"),
)


def _readiness(**overrides: object) -> Readiness:
    conditions = (
        ConditionResult("1", "経過日数", Verdict.met, "42 日 >= 42 日"),
        ConditionResult("2", "SPY 超過", Verdict.unmet, "P-B = 0.99%"),
        ConditionResult("3", "最大銘柄除外", Verdict.undetermined, "SPY 終値が未取得"),
        ConditionResult("4a", "実弾期待値", Verdict.met, "期待値 12.00"),
        ConditionResult("4b", "ドローダウン", Verdict.met, "最大 1.00%"),
        ConditionResult("4c", "kill switch", Verdict.needs_review, "kill switch 2 回"),
        ConditionResult("5", "手動 close", Verdict.met, "1 件"),
    )
    base = Readiness(
        start_date=_S,
        end_date=_E,
        elapsed_days=42,
        realized_pnl=Money(Decimal("10")),
        unrealized_pnl=Money(Decimal("40")),
        window_pnl=Money(Decimal("50")),
        p_pct=Decimal("2.00"),
        p_ex_pct=Decimal("0.50"),
        p_net_pct=Decimal("1.50"),
        estimated_live_pnl=Money(Decimal("12")),
        benchmark_pct=Decimal("1.00"),
        benchmark_start=BenchmarkPoint(_S, Decimal("100"), False),
        benchmark_end=BenchmarkPoint(_E, Decimal("101"), True),
        dd_cap_pct=Decimal("6"),
        kill_switch_count=2,
        manual_close_count=1,
        conditions=conditions,
        missing=(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _text(readiness: Readiness) -> str:
    return "\n".join(render_readiness(readiness))


def test_ac18_section_has_heading_and_period_line() -> None:
    text = _text(_readiness())

    assert "## 移行条件" in text
    assert "2026-09-18" in text and "2026-10-30" in text and "42" in text


def test_ac18_p_p_net_and_spy_return_lines_are_shown_together() -> None:
    lines = render_readiness(_readiness())

    assert any(ln.startswith("ポートフォリオ損益率 P: 2.00%") for ln in lines)
    assert any(ln.startswith("コスト控除後 P_net: 1.50%") for ln in lines)
    spy = next(ln for ln in lines if ln.startswith("SPY リターン B: 1.00%"))
    assert "配当含まず" in spy
    assert "代用" in spy


def test_ac18_every_condition_line_has_a_verdict_and_detail() -> None:
    lines = render_readiness(_readiness())
    expected = {
        "1": "満たした",
        "2": "未達",
        "3": "未判定",
        "4a": "満たした",
        "4b": "満たした",
        "4c": "要確認",
        "5": "満たした",
    }

    for number, label in expected.items():
        line = next(ln for ln in lines if ln.startswith(f"条件 {number}:"))
        assert label in line
        assert " — " in line
        assert any(ch.isdigit() for ch in line.split(" — ", 1)[1]) or label == "未判定"


def test_ac18_summary_line_counts_verdicts_and_defers_to_kyo() -> None:
    lines = render_readiness(_readiness())
    summary = lines[-1]

    assert "未達 1 件" in summary
    assert "未判定 1 件" in summary
    assert "要確認 1 件" in summary
    assert "最終判断は kyo" in summary


def test_ac18_never_declares_pass_or_live_switch() -> None:
    text = _text(_readiness()) + _text(
        _readiness(
            conditions=tuple(
                ConditionResult(c.number, c.label, Verdict.met, c.detail)
                for c in _readiness().conditions
            )
        )
    )

    assert "合格" not in text
    assert "live に切り替え" not in text


def test_ac18_missing_data_names_the_gap_and_run_cycle_hint() -> None:
    readiness = _readiness(
        elapsed_days=None,
        start_date=None,
        end_date=None,
        realized_pnl=None,
        unrealized_pnl=None,
        p_pct=None,
        p_net_pct=None,
        benchmark_pct=None,
        benchmark_start=None,
        benchmark_end=None,
        missing=("equity_snapshots", "spy_close"),
        conditions=tuple(
            ConditionResult(c.number, c.label, Verdict.undetermined, "データ不足")
            for c in _readiness().conditions
        ),
    )
    text = _text(readiness)

    assert "equity_snapshots" in text and "spy_close" in text
    assert "run-cycle" in text
    assert "未判定 7 件" in text
    assert "最終判断は kyo" in text


def test_ac19_render_report_without_readiness_has_no_section() -> None:
    period = Period(start=None, end=None)
    metrics = compute([], [], [], [], period)
    out = render_report(
        metrics, estimate(Money(Decimal("0")), [], 0, Money(Decimal("2500")), _COST), [], {}, period
    )

    assert "移行条件" not in out


def test_ac18_render_report_with_readiness_appends_section_at_the_end() -> None:
    period = Period(start=None, end=None)
    metrics = compute([], [], [], [], period)
    out = render_report(
        metrics,
        estimate(Money(Decimal("0")), [], 0, Money(Decimal("2500")), _COST),
        [],
        {},
        period,
        readiness=_readiness(),
    )

    assert out.index("## トレード一覧") < out.index("## 移行条件")
