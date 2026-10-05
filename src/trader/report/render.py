# ruff: noqa: RUF001
"""Text rendering for `trader report` (R-22, R-23, AC-33).

`render_report` is a pure function: it only formats already-computed
`Metrics`/`LiveEstimate` values into a human-readable string. Every trade
must be traceable back to "why bought / why sold / how much" without
touching the database again (AC-33, checked by kyo after deploy).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from trader.domain.models import Decision, Trade
from trader.report.live_estimate import LiveEstimate
from trader.report.metrics import Metrics, Period
from trader.report.readiness import BenchmarkPoint, Readiness, Verdict

_RATIONALE_EXCERPT_LEN = 200


def _period_label(period: Period) -> str:
    start = period.start.isoformat() if period.start is not None else "指定なし"
    end = period.end.isoformat() if period.end is not None else "指定なし"
    return f"{start} 〜 {end}"


def _render_summary(metrics: Metrics) -> list[str]:
    return [
        "## 損益サマリ",
        f"期間損益: {metrics.period_pnl.amount}",
        f"トレード数: {metrics.trade_count}(勝ち {metrics.win_count} / 負け {metrics.loss_count})",
        f"勝率: {metrics.win_rate_pct}%",
        f"平均損益: {metrics.avg_pnl.amount}",
        f"最大ドローダウン: {metrics.max_drawdown_pct}%",
    ]


def _render_live_estimate(estimate: LiveEstimate) -> list[str]:
    return [
        "## 実弾期待値(概算)",
        f"paper 損益: {estimate.paper_pnl.amount}",
        f"想定スリッページ: -{estimate.slippage_cost.amount}",
        f"想定手数料(約定連動): -{estimate.commission_cost.amount}",
        f"想定手数料(注文固定): -{estimate.fixed_commission_cost.amount}",
        f"想定為替コスト: -{estimate.fx_cost.amount}",
        f"実弾期待値: {estimate.estimated_live_pnl.amount}",
    ]


def _render_decision_counts(metrics: Metrics) -> list[str]:
    lines = ["## 判断内訳"]
    if not metrics.decision_counts:
        lines.append("(判断なし)")
        return lines
    for bucket, count in sorted(metrics.decision_counts.items()):
        lines.append(f"{bucket}: {count}")
    return lines


def _render_exit_reason_counts(metrics: Metrics) -> list[str]:
    lines = ["## ルール発火回数(売却理由別)"]
    if not metrics.exit_reason_counts:
        lines.append("(該当トレードなし)")
        return lines
    for reason, count in sorted(metrics.exit_reason_counts.items(), key=lambda item: item[0].value):
        lines.append(f"{reason.value}: {count}")
    return lines


def _render_open_positions(metrics: Metrics) -> list[str]:
    lines = ["## 保有中ポジション"]
    if not metrics.open_positions:
        lines.append("(保有なし)")
        return lines
    for position in metrics.open_positions:
        lines.append(
            f"{position.ticker}: {position.qty.shares} 株 "
            f"@ 平均取得 {position.avg_cost.amount}"
            f"(高値 {position.high_watermark.amount})"
        )
    return lines


def _render_trade(trade: Trade, entry_decisions: Mapping[str, Decision]) -> list[str]:
    entry_decision = entry_decisions.get(trade.entry_decision_id)
    if entry_decision is not None:
        rationale_excerpt = entry_decision.rationale[:_RATIONALE_EXCERPT_LEN]
        evidence_count = len(entry_decision.evidence_mention_ids)
        rationale_line = f"根拠: {rationale_excerpt}(evidence {evidence_count} 件)"
    else:
        rationale_line = "根拠: (entry decision が見つかりません)"

    return [
        f"### {trade.ticker}(closed {trade.closed_at.date().isoformat()})",
        rationale_line,
        f"売却理由: {trade.exit_reason.value}",
        f"損益: 実現損益 {trade.realized_pnl.amount} / 手数料 {trade.fees.amount}",
        f"保有日数: {trade.holding_days}",
    ]


def _render_trades(trades: Sequence[Trade], entry_decisions: Mapping[str, Decision]) -> list[str]:
    lines = ["## トレード一覧"]
    if not trades:
        lines.append("(該当期間のトレードなし)")
        return lines
    for trade in trades:
        lines.extend(_render_trade(trade, entry_decisions))
    return lines


_VERDICT_LABELS = {
    Verdict.met: "満たした",
    Verdict.unmet: "未達",
    Verdict.undetermined: "未判定",
    Verdict.needs_review: "要確認",
}


def _fmt_pct(value: object) -> str:
    return "未取得" if value is None else f"{value}%"


def _fmt_day(day: object) -> str:
    return "なし" if day is None else str(day)


def _benchmark_note(start: BenchmarkPoint | None, end: BenchmarkPoint | None) -> str:
    if start is None or end is None:
        return "close(S) → close(E)、配当含まず"
    note = f"close({start.day}) → close({end.day})、配当含まず"
    if start.substituted or end.substituted:
        note += "、代用: 取得日の近傍営業日の終値"
    return note


def _summary_line(readiness: Readiness) -> str:
    parts = []
    for verdict in (Verdict.unmet, Verdict.undetermined, Verdict.needs_review):
        numbers = [c.number for c in readiness.conditions if c.verdict == verdict]
        parts.append(f"{_VERDICT_LABELS[verdict]} {len(numbers)} 件 ({', '.join(numbers)})")
    return " / ".join(parts) + "。最終判断は kyo"


def render_readiness(readiness: Readiness) -> list[str]:
    """Render the paper -> small-live readiness section (LR-9..LR-11)."""
    realized = readiness.realized_pnl.amount if readiness.realized_pnl is not None else "未取得"
    unrealized = (
        readiness.unrealized_pnl.amount if readiness.unrealized_pnl is not None else "未取得"
    )
    capital = readiness.capital.amount if readiness.capital is not None else "未取得"
    elapsed = "なし" if readiness.elapsed_days is None else f"{readiness.elapsed_days} 日"
    lines = [
        "## 移行条件（paper → 少額実弾）",
        f"起点日: {_fmt_day(readiness.start_date)} / 評価日: {_fmt_day(readiness.end_date)} "
        f"/ 経過日数: {elapsed}",
        f"ポートフォリオ損益率 P: {_fmt_pct(readiness.p_pct)}"
        f"（実現 {realized} / 評価 {unrealized} / capital {capital}）",
        f"コスト控除後 P_net: {_fmt_pct(readiness.p_net_pct)}",
        f"SPY リターン B: {_fmt_pct(readiness.benchmark_pct)}"
        f"（{_benchmark_note(readiness.benchmark_start, readiness.benchmark_end)}）",
    ]
    for cond in readiness.conditions:
        lines.append(f"条件 {cond.number}: {_VERDICT_LABELS[cond.verdict]} — {cond.detail}")
    if readiness.missing:
        names = ", ".join(readiness.missing)
        lines.append(f"不足データ: {names}(`run-cycle` が 1 回走れば記録されます)")
    lines.append(_summary_line(readiness))
    return lines


def render_report(
    metrics: Metrics,
    estimate: LiveEstimate,
    trades: Sequence[Trade],
    entry_decisions: Mapping[str, Decision],
    period: Period,
    readiness: Readiness | None = None,
) -> str:
    """Render `metrics`/`estimate`/`trades` as a headed, human-readable report."""
    sections = [
        "# trader report",
        f"期間: {_period_label(period)}",
        *_render_summary(metrics),
        *_render_live_estimate(estimate),
        *_render_decision_counts(metrics),
        *_render_exit_reason_counts(metrics),
        *_render_open_positions(metrics),
        *_render_trades(trades, entry_decisions),
        *(render_readiness(readiness) if readiness is not None else []),
    ]
    return "\n".join(sections) + "\n"
