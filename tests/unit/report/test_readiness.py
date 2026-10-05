"""AC-16: `evaluate_readiness` judges conditions 1-5 from spec LR-1..LR-8."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trader.domain.models import Fill
from trader.domain.money import Money, Price, Quantity
from trader.report.live_estimate import estimate
from trader.report.readiness import (
    BenchmarkPoint,
    ReadinessInputs,
    Verdict,
    evaluate_readiness,
)
from trader.rules.schema import CostAssumptions

_S = date(2026, 9, 18)
_CAPITAL = Money(Decimal("2500"))
_NO_COST = CostAssumptions(
    slippage_pct_per_side=Decimal("0"),
    commission_pct=Decimal("0"),
    commission_usd_per_order=Decimal("0"),
    fx_cost_pct_one_way=Decimal("0"),
)


def _m(value: str) -> Money:
    return Money(Decimal(value))


def _bench(start: str, end: str) -> tuple[BenchmarkPoint, BenchmarkPoint]:
    return (
        BenchmarkPoint(_S, Decimal(start), False),
        BenchmarkPoint(date(2026, 10, 30), Decimal(end), False),
    )


def _inputs(**overrides: object) -> ReadinessInputs:
    start, end = _bench("100", "101")  # B = 1.00%
    base = ReadinessInputs(
        start_date=_S,
        end_date=_S + timedelta(days=42),
        since_requested=None,
        capital=_CAPITAL,
        realized_start_by_ticker={},
        realized_end_by_ticker={},
        unreal_start_by_ticker={},
        unreal_end_by_ticker={"AAA": _m("50")},  # P = 2.00%
        benchmark_start=start,
        benchmark_end=end,
        window_fills=(),
        window_order_count=0,
        cost=_NO_COST,
        max_weekly_loss_pct=Decimal("6"),
        equity_series=((_S, _m("2500")),),
        kill_switch_events=(),
        manual_close_count=1,
        missing=(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _verdict(result_inputs: ReadinessInputs, number: str) -> Verdict:
    readiness = evaluate_readiness(result_inputs)
    return next(c.verdict for c in readiness.conditions if c.number == number)


def test_ac16_condition1_41_days_is_unmet() -> None:
    assert _verdict(_inputs(end_date=_S + timedelta(days=41)), "1") is Verdict.unmet


def test_ac16_condition1_42_days_is_met() -> None:
    inputs = _inputs(end_date=_S + timedelta(days=42))
    assert _verdict(inputs, "1") is Verdict.met
    assert evaluate_readiness(inputs).elapsed_days == 42


def test_ac16_condition1_without_start_is_undetermined() -> None:
    assert _verdict(_inputs(start_date=None), "1") is Verdict.undetermined


def test_ac16_condition2_p_minus_b_099_is_unmet() -> None:
    inputs = _inputs(unreal_end_by_ticker={"AAA": _m("49.75")})  # P=1.99, B=1.00
    readiness = evaluate_readiness(inputs)
    assert readiness.p_pct == Decimal("1.99")
    assert readiness.benchmark_pct == Decimal("1.00")
    assert _verdict(inputs, "2") is Verdict.unmet


def test_ac16_condition2_p_minus_b_100_is_met() -> None:
    inputs = _inputs()  # P=2.00, B=1.00
    assert evaluate_readiness(inputs).p_pct == Decimal("2.00")
    assert _verdict(inputs, "2") is Verdict.met


def test_ac16_condition3_excludes_only_the_best_positive_ticker() -> None:
    # AAA +50, BBB +10 -> P=2.40; excluding AAA leaves 10 -> P_ex=0.40 < B=1.00
    inputs = _inputs(unreal_end_by_ticker={"AAA": _m("50"), "BBB": _m("10")})
    readiness = evaluate_readiness(inputs)
    assert readiness.p_ex_pct == Decimal("0.40")
    assert _verdict(inputs, "3") is Verdict.unmet


def test_ac16_condition3_does_not_exclude_when_all_tickers_negative() -> None:
    inputs = _inputs(
        unreal_end_by_ticker={"AAA": _m("-10"), "BBB": _m("-15")},
        benchmark_end=BenchmarkPoint(date(2026, 10, 30), Decimal("99"), False),  # B=-1.00
    )
    readiness = evaluate_readiness(inputs)
    assert readiness.p_pct == Decimal("-1.00")
    assert readiness.p_ex_pct == Decimal("-1.00")
    assert _verdict(inputs, "3") is Verdict.met  # -1.00 >= -1.00


def test_ac16_condition3_equal_positive_tickers_exclude_only_one() -> None:
    inputs = _inputs(unreal_end_by_ticker={"AAA": _m("25"), "BBB": _m("25")})
    readiness = evaluate_readiness(inputs)
    assert readiness.p_pct == Decimal("2.00")
    assert readiness.p_ex_pct == Decimal("1.00")
    assert _verdict(inputs, "3") is Verdict.met  # 1.00 >= B 1.00


def test_ac16_condition3_per_ticker_pnl_uses_window_difference_with_realized() -> None:
    # AAA: realized 30 (end) - 10 (start) + unreal 20 - 5 = 35 ; BBB: unreal 5
    inputs = _inputs(
        realized_start_by_ticker={"AAA": _m("10")},
        realized_end_by_ticker={"AAA": _m("30")},
        unreal_start_by_ticker={"AAA": _m("5")},
        unreal_end_by_ticker={"AAA": _m("20"), "BBB": _m("5")},
    )
    readiness = evaluate_readiness(inputs)
    assert readiness.realized_pnl == _m("20")
    assert readiness.unrealized_pnl == _m("20")
    assert readiness.window_pnl == _m("40")
    assert readiness.p_pct == Decimal("1.60")
    assert readiness.p_ex_pct == Decimal("0.20")  # (40 - 35) / 2500


def test_ac16_p_net_matches_live_estimate() -> None:
    cost = CostAssumptions(
        slippage_pct_per_side=Decimal("0.5"),
        commission_pct=Decimal("0"),
        commission_usd_per_order=Decimal("0"),
        fx_cost_pct_one_way=Decimal("1.0"),
    )
    fill = Fill(
        id="f-1",
        order_id="o-1",
        filled_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
        qty=Quantity(10),
        price=Price(Decimal("100")),
        fee=Money(Decimal("0")),
    )
    inputs = _inputs(cost=cost, window_fills=(fill,), window_order_count=1)
    expected = estimate(_m("50"), (fill,), 1, _CAPITAL, cost).estimated_live_pnl
    readiness = evaluate_readiness(inputs)
    assert readiness.estimated_live_pnl == expected  # 50 - 5 - 50 = -5
    assert readiness.p_net_pct == Decimal("-0.20")
    assert _verdict(inputs, "4a") is Verdict.unmet


def test_ac16_condition4a_positive_estimate_is_met() -> None:
    assert _verdict(_inputs(), "4a") is Verdict.met


def test_ac16_condition4a_is_judged_without_spy_data() -> None:
    # LR-5: 4(a) needs only paper P&L and costs, not the benchmark.
    inputs = _inputs(benchmark_start=None, benchmark_end=None)
    assert _verdict(inputs, "4a") is Verdict.met


def test_ac16_drawdown_denominator_is_capital() -> None:
    inputs = _inputs(equity_series=((_S, _m("100000")), (_S + timedelta(days=1), _m("99950"))))
    readiness = evaluate_readiness(inputs)
    assert readiness.dd_cap_pct == Decimal("2.00")  # 50 / 2500
    assert _verdict(inputs, "4b") is Verdict.met


def test_ac16_drawdown_at_limit_is_unmet() -> None:
    inputs = _inputs(
        equity_series=((_S, _m("2500")), (_S + timedelta(days=1), _m("2350")))
    )  # 150 / 2500 = 6.00% (not < 6)
    assert evaluate_readiness(inputs).dd_cap_pct == Decimal("6.00")
    assert _verdict(inputs, "4b") is Verdict.unmet


def test_ac16_drawdown_measured_from_running_peak() -> None:
    inputs = _inputs(
        equity_series=(
            (_S, _m("2500")),
            (_S + timedelta(days=1), _m("2550")),
            (_S + timedelta(days=2), _m("2500")),
        )
    )
    assert evaluate_readiness(inputs).dd_cap_pct == Decimal("2.00")


def test_ac16_drawdown_without_snapshots_is_undetermined() -> None:
    assert _verdict(_inputs(equity_series=()), "4b") is Verdict.undetermined


def test_ac16_kill_switch_zero_is_met() -> None:
    assert _verdict(_inputs(), "4c") is Verdict.met


def test_ac16_kill_switch_one_or_more_needs_review_with_count() -> None:
    inputs = _inputs(kill_switch_events=(("c1", "loss limit"), ("c2", "loss limit")))
    readiness = evaluate_readiness(inputs)
    assert _verdict(inputs, "4c") is Verdict.needs_review
    assert readiness.kill_switch_count == 2


def test_ac16_manual_close_zero_is_unmet() -> None:
    assert _verdict(_inputs(manual_close_count=0), "5") is Verdict.unmet


def test_ac16_manual_close_one_is_met() -> None:
    assert _verdict(_inputs(manual_close_count=1), "5") is Verdict.met


def test_ac16_missing_benchmark_makes_conditions_2_and_3_undetermined() -> None:
    inputs = _inputs(benchmark_end=None)
    assert _verdict(inputs, "2") is Verdict.undetermined
    assert _verdict(inputs, "3") is Verdict.undetermined
    assert evaluate_readiness(inputs).benchmark_pct is None


def test_ac16_missing_unrealized_end_makes_pnl_conditions_undetermined() -> None:
    inputs = _inputs(unreal_end_by_ticker=None)
    readiness = evaluate_readiness(inputs)
    assert readiness.p_pct is None
    assert readiness.p_net_pct is None
    for number in ("2", "3", "4a"):
        assert _verdict(inputs, number) is Verdict.undetermined


def test_ac16_benchmark_return_is_price_return_quantized() -> None:
    start, end = _bench("300", "310")  # 3.3333...% -> 3.33
    readiness = evaluate_readiness(_inputs(benchmark_start=start, benchmark_end=end))
    assert readiness.benchmark_pct == Decimal("3.33")


def test_ac16_all_percentages_are_decimal_with_two_places() -> None:
    readiness = evaluate_readiness(_inputs(unreal_end_by_ticker={"AAA": _m("33.33")}))
    for value in (readiness.p_pct, readiness.p_ex_pct, readiness.p_net_pct, readiness.dd_cap_pct):
        assert isinstance(value, Decimal)
        assert value.as_tuple().exponent == -2
