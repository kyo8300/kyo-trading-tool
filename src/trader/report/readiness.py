"""LR-1..LR-8: pure evaluation of the paper -> small-live readiness conditions.

Knows nothing about the database; `readiness_inputs` assembles the inputs.
All figures are `Decimal`; percentages are quantized to 2 places with
ROUND_HALF_EVEN before they are compared or shown (LR-8).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from trader.domain.models import Fill
from trader.domain.money import Money
from trader.report.live_estimate import estimate
from trader.rules.schema import CostAssumptions

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_PCT_STEP = Decimal("0.01")
_MIN_ELAPSED_DAYS = 42
_MIN_EXCESS_PCT = Decimal("1.00")


@dataclass(frozen=True, slots=True)
class BenchmarkPoint:
    """One SPY close; `substituted` when a neighbouring day stood in (LR-4)."""

    day: date
    close: Decimal
    substituted: bool


@dataclass(frozen=True, slots=True)
class ReadinessInputs:
    start_date: date | None
    end_date: date | None
    since_requested: date | None
    capital: Money
    realized_start_by_ticker: Mapping[str, Money]
    realized_end_by_ticker: Mapping[str, Money]
    unreal_start_by_ticker: Mapping[str, Money] | None
    unreal_end_by_ticker: Mapping[str, Money] | None
    benchmark_start: BenchmarkPoint | None
    benchmark_end: BenchmarkPoint | None
    window_fills: tuple[Fill, ...]
    window_order_count: int
    cost: CostAssumptions
    max_weekly_loss_pct: Decimal
    equity_series: tuple[tuple[date, Money], ...]
    kill_switch_events: tuple[tuple[str, str], ...]
    manual_close_count: int
    missing: tuple[str, ...]


class Verdict(StrEnum):
    met = "met"
    unmet = "unmet"
    undetermined = "undetermined"
    needs_review = "needs_review"


@dataclass(frozen=True, slots=True)
class ConditionResult:
    number: str
    label: str
    verdict: Verdict
    detail: str


@dataclass(frozen=True, slots=True)
class Readiness:
    start_date: date | None
    end_date: date | None
    elapsed_days: int | None
    realized_pnl: Money | None
    unrealized_pnl: Money | None
    window_pnl: Money | None
    p_pct: Decimal | None
    p_ex_pct: Decimal | None
    p_net_pct: Decimal | None
    estimated_live_pnl: Money | None
    benchmark_pct: Decimal | None
    benchmark_start: BenchmarkPoint | None
    benchmark_end: BenchmarkPoint | None
    dd_cap_pct: Decimal | None
    kill_switch_count: int
    manual_close_count: int
    conditions: tuple[ConditionResult, ...]
    missing: tuple[str, ...]
    capital: Money | None = None


def _q(value: Decimal) -> Decimal:
    return value.quantize(_PCT_STEP, rounding=ROUND_HALF_EVEN)


def _pct(amount: Decimal, capital: Decimal) -> Decimal:
    return _q(amount / capital * _HUNDRED)


def _total(by_ticker: Mapping[str, Money]) -> Decimal:
    return sum((m.amount for m in by_ticker.values()), _ZERO)


def _benchmark_pct(start: BenchmarkPoint | None, end: BenchmarkPoint | None) -> Decimal | None:
    if start is None or end is None or start.close <= _ZERO:
        return None
    return _q((end.close / start.close - Decimal(1)) * _HUNDRED)


def _best_ticker_pnl(
    inputs: ReadinessInputs, unreal_start: Mapping[str, Money], unreal_end: Mapping[str, Money]
) -> Decimal:
    tickers = (
        set(inputs.realized_start_by_ticker)
        | set(inputs.realized_end_by_ticker)
        | set(unreal_start)
        | set(unreal_end)
    )

    def amount(table: Mapping[str, Money], ticker: str) -> Decimal:
        money = table.get(ticker)
        return money.amount if money is not None else _ZERO

    pnls = [
        amount(inputs.realized_end_by_ticker, t)
        + amount(unreal_end, t)
        - amount(inputs.realized_start_by_ticker, t)
        - amount(unreal_start, t)
        for t in tickers
    ]
    return max([_ZERO, *pnls])


def _max_drawdown_pct(series: tuple[tuple[date, Money], ...], capital: Decimal) -> Decimal | None:
    if not series:
        return None
    peak = series[0][1].amount
    worst = _ZERO
    for _, equity in series:
        peak = max(peak, equity.amount)
        worst = max(worst, peak - equity.amount)
    return _pct(worst, capital)


def _verdict(ok: bool) -> Verdict:
    return Verdict.met if ok else Verdict.unmet


def _undetermined(number: str, label: str, reason: str) -> ConditionResult:
    return ConditionResult(number, label, Verdict.undetermined, reason)


def _cond_1(elapsed: int | None) -> ConditionResult:
    label = "経過日数"
    if elapsed is None:
        return _undetermined("1", label, "起点日なし")
    detail = f"{elapsed} 日 (基準 {_MIN_ELAPSED_DAYS} 日以上)"
    return ConditionResult("1", label, _verdict(elapsed >= _MIN_ELAPSED_DAYS), detail)


def _cond_2_3(p: Decimal | None, p_ex: Decimal | None, b: Decimal | None) -> list[ConditionResult]:
    if p is None or p_ex is None or b is None:
        reason = "SPY または評価損益が未取得"
        return [
            _undetermined("2", "SPY 超過", reason),
            _undetermined("3", "SPY 以上(最大1銘柄除外)", reason),
        ]
    diff = _q(p - b)
    return [
        ConditionResult(
            "2",
            "SPY 超過",
            _verdict(diff >= _MIN_EXCESS_PCT),
            f"P - B = {diff} pt (基準 {_MIN_EXCESS_PCT} pt以上)",
        ),
        ConditionResult(
            "3", "SPY 以上(最大1銘柄除外)", _verdict(p_ex >= b), f"P_ex = {p_ex}% / B = {b}%"
        ),
    ]


def _cond_4a(live_pnl: Money | None, b: Decimal | None) -> ConditionResult:
    label = "実弾期待値(コスト控除後)"
    if live_pnl is None or b is None:  # spec table: "条件 2 と同じ"
        return _undetermined("4a", label, "SPY または評価損益が未取得")
    return ConditionResult(
        "4a", label, _verdict(live_pnl.amount > _ZERO), f"推定損益 {live_pnl.amount} USD"
    )


def _cond_4b(dd: Decimal | None, limit: Decimal) -> ConditionResult:
    label = "capital 基準最大ドローダウン"
    if dd is None:
        return _undetermined("4b", label, "snapshot なし")
    return ConditionResult("4b", label, _verdict(dd < limit), f"{dd}% (上限 {limit}% 未満)")


def _cond_4c(events: tuple[tuple[str, str], ...]) -> ConditionResult:
    label = "キルスイッチ発火"
    if not events:
        return ConditionResult("4c", label, Verdict.met, "0 回")
    summaries = "; ".join(f"{cycle_id}: {summary}" for cycle_id, summary in events)
    return ConditionResult("4c", label, Verdict.needs_review, f"{len(events)} 回 ({summaries})")


def _cond_5(manual: int) -> ConditionResult:
    return ConditionResult(
        "5", "手動売却の検証", _verdict(manual >= 1), f"manual close {manual} 件"
    )


def evaluate_readiness(inputs: ReadinessInputs) -> Readiness:
    """Apply spec 'reading definitions' to `inputs` and judge conditions 1-5."""
    capital = inputs.capital.amount
    unreal_end = inputs.unreal_end_by_ticker
    elapsed = (
        (inputs.end_date - inputs.start_date).days
        if inputs.start_date is not None and inputs.end_date is not None
        else None
    )
    realized = Money(
        _total(inputs.realized_end_by_ticker) - _total(inputs.realized_start_by_ticker)
    )
    unreal_start = inputs.unreal_start_by_ticker
    unrealized = (
        None
        if unreal_end is None or unreal_start is None
        else Money(_total(unreal_end) - _total(unreal_start))
    )
    window = None if unrealized is None else realized + unrealized
    bench = _benchmark_pct(inputs.benchmark_start, inputs.benchmark_end)
    p = p_ex = p_net = None
    live = None
    if window is not None and unreal_end is not None and unreal_start is not None:
        p = _pct(window.amount, capital)
        p_ex = _pct(window.amount - _best_ticker_pnl(inputs, unreal_start, unreal_end), capital)
        live = estimate(
            window, inputs.window_fills, inputs.window_order_count, inputs.capital, inputs.cost
        ).estimated_live_pnl
        p_net = _pct(live.amount, capital)
    dd = _max_drawdown_pct(inputs.equity_series, capital)
    conditions = (
        _cond_1(elapsed),
        *_cond_2_3(p, p_ex, bench),
        _cond_4a(live, bench),
        _cond_4b(dd, inputs.max_weekly_loss_pct),
        _cond_4c(inputs.kill_switch_events),
        _cond_5(inputs.manual_close_count),
    )
    return Readiness(
        start_date=inputs.start_date,
        end_date=inputs.end_date,
        elapsed_days=elapsed,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        window_pnl=window,
        p_pct=p,
        p_ex_pct=p_ex,
        p_net_pct=p_net,
        estimated_live_pnl=live,
        benchmark_pct=bench,
        benchmark_start=inputs.benchmark_start,
        benchmark_end=inputs.benchmark_end,
        dd_cap_pct=dd,
        kill_switch_count=len(inputs.kill_switch_events),
        manual_close_count=inputs.manual_close_count,
        conditions=conditions,
        missing=inputs.missing,
        capital=inputs.capital,
    )
