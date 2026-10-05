"""T-8 / AC-17: `load_readiness_inputs` assembles ledger data for readiness."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.domain.models import (
    Action,
    Approval,
    Approver,
    Decision,
    ExitReason,
    Fill,
    Order,
    OrderStatus,
    Origin,
    Position,
    PositionMark,
    Price,
    RuleCheck,
    Side,
    Trade,
)
from trader.domain.money import Money, Quantity
from trader.domain.result import Err, Ok
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger.benchmark_repository import upsert_benchmark_price, upsert_position_mark
from trader.ledger.db import open_db
from trader.ledger.repository import insert_approval, insert_decision, insert_fill, insert_order
from trader.report.readiness_inputs import load_readiness_inputs
from trader.rules.schema import load_rules

_RULES = Path(__file__).parent.parent.parent / "fixtures" / "rules" / "valid.yaml"


@pytest.fixture
def conn(tmp_path: Path):
    result = open_db(tmp_path / "t.db")
    assert isinstance(result, Ok)
    yield result.value
    result.value.close()


def _rules():
    loaded = load_rules(_RULES)
    assert isinstance(loaded, Ok)
    return loaded.value


def _snapshot(conn, day: str) -> None:
    taken = datetime.fromisoformat(f"{day}T20:00:00+00:00").astimezone(UTC)
    portfolio_repo.upsert_equity_snapshot(
        conn,
        day,
        "paper",
        Money(Decimal(1000)),
        Money(Decimal(0)),
        Money(Decimal(1000)),
        Decimal(0),
        taken,
    )


def test_no_snapshots_gives_none_start_and_missing_name(conn) -> None:
    result = load_readiness_inputs(conn, _rules(), None)
    assert isinstance(result, Ok)
    assert result.value.start_date is None
    assert "equity_snapshots" in result.value.missing


def test_since_before_first_snapshot_is_err(conn) -> None:
    _snapshot(conn, "2026-09-14")
    result = load_readiness_inputs(conn, _rules(), date(2026, 9, 1))
    assert isinstance(result, Err)
    assert "YYYY-MM-DD" in result.error.message


def test_benchmark_start_substituted_and_end_exact(conn) -> None:
    for day in ("2026-09-14", "2026-09-16"):
        _snapshot(conn, day)
    upsert_benchmark_price(conn, "SPY", "2026-09-15", Price(Decimal(500)), datetime.now(UTC))
    upsert_benchmark_price(conn, "SPY", "2026-09-16", Price(Decimal(510)), datetime.now(UTC))
    result = load_readiness_inputs(conn, _rules(), None)
    assert isinstance(result, Ok)
    inputs = result.value
    assert inputs.start_date == date(2026, 9, 14)
    assert inputs.end_date == date(2026, 9, 16)
    assert inputs.benchmark_start is not None and inputs.benchmark_start.substituted
    assert inputs.benchmark_end is not None and not inputs.benchmark_end.substituted
    assert len(inputs.equity_series) == 2


# --- AC-17 expanded: LR-2..LR-7 -------------------------------------------

_SHA = "a" * 64


def _snap_equity(conn, day: str, equity: str) -> None:
    taken = datetime.fromisoformat(f"{day}T20:00:00+00:00")
    portfolio_repo.upsert_equity_snapshot(
        conn,
        day,
        "paper",
        Money(Decimal(equity)),
        Money(Decimal(0)),
        Money(Decimal(equity)),
        Decimal(0),
        taken,
    )


def _ensure_rule_set(conn) -> None:
    portfolio_repo.insert_rule_set(conn, _SHA, "kyo", "2026-09-01", Money(Decimal(2500)), "x: 1")


def _cycle(conn, cycle_id: str, started: str, outcome: str | None, summary: str | None) -> None:
    started_at = datetime.fromisoformat(started)
    assert isinstance(portfolio_repo.insert_cycle(conn, cycle_id, started_at, "paper"), Ok)
    if outcome is not None:
        done = portfolio_repo.finish_cycle(conn, cycle_id, started_at, outcome, summary)
        assert isinstance(done, Ok)


def _decision(conn, decision_id: str, origin: Origin, mode: str = "paper") -> None:
    _ensure_rule_set_once(conn)
    if conn.execute("SELECT 1 FROM cycles WHERE id = 'c-d'").fetchone() is None:
        _cycle(conn, "c-d", "2026-09-14T15:00:00+00:00", "ok", None)
    decision = Decision(
        id=decision_id,
        cycle_id="c-d",
        decided_at=datetime(2026, 9, 14, 15, 0, tzinfo=UTC),
        mode=mode,
        ticker="AAA",
        action=Action.buy if origin is Origin.llm else Action.sell,
        origin=origin,
        confidence=None,
        rationale="r",
        evidence_mention_ids=(),
        llm_model=None,
        prompt_sha256=None,
        response_sha256=None,
        rule_set_sha256=_SHA,
        rule_check=RuleCheck.passed,
        rule_check_reason="ok",
        proposed_notional=None,
        reference_price=None,
    )
    assert isinstance(insert_decision(conn, decision), Ok)


def _ensure_rule_set_once(conn) -> None:
    if conn.execute("SELECT 1 FROM rule_sets WHERE sha256 = ?", (_SHA,)).fetchone() is None:
        _ensure_rule_set(conn)


def _trade(
    conn,
    trade_id: str,
    ticker: str,
    closed: str,
    pnl: str,
    reason: ExitReason = ExitReason.llm,
    exit_decision: str | None = None,
) -> None:
    _decision(conn, f"entry-{trade_id}", Origin.llm) if conn.execute(
        "SELECT 1 FROM decisions WHERE id = ?", (f"entry-{trade_id}",)
    ).fetchone() is None else None
    closed_at = datetime.fromisoformat(f"{closed}T15:00:00+00:00")
    trade = Trade(
        id=trade_id,
        ticker=ticker,
        opened_at=closed_at,
        closed_at=closed_at,
        entry_decision_id=f"entry-{trade_id}",
        exit_decision_ids=(exit_decision,) if exit_decision else (),
        exit_reason=reason,
        realized_pnl=Money(Decimal(pnl)),
        fees=Money(Decimal(0)),
        holding_days=1,
    )
    assert isinstance(portfolio_repo.insert_trade(conn, trade), Ok)


def _mark(conn, day: str, ticker: str, pnl: str) -> None:
    mark = PositionMark(
        mark_date=day,
        ticker=ticker,
        qty=Quantity(1),
        avg_cost=Price(Decimal(10)),
        mark_price=Price(Decimal(10)),
        unrealized_pnl=Money(Decimal(pnl)),
        taken_at=datetime.fromisoformat(f"{day}T20:00:00+00:00"),
    )
    assert isinstance(upsert_position_mark(conn, mark), Ok)


def _spy(conn, day: str, close: str) -> None:
    upsert_benchmark_price(conn, "SPY", day, Price(Decimal(close)), datetime.now(UTC))


def _ok(conn, since: date | None = None):
    result = load_readiness_inputs(conn, _rules(), since)
    assert isinstance(result, Ok)
    return result.value


def test_ac17_start_is_oldest_snapshot_and_end_is_newest(conn) -> None:
    for day in ("2026-09-16", "2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    inputs = _ok(conn)
    assert inputs.start_date == date(2026, 9, 14)
    assert inputs.end_date == date(2026, 9, 18)
    assert inputs.since_requested is None


def test_ac17_since_after_first_moves_start_back_only_forward(conn) -> None:
    for day in ("2026-09-14", "2026-09-16", "2026-09-18"):
        _snapshot(conn, day)
    inputs = _ok(conn, date(2026, 9, 16))
    assert inputs.start_date == date(2026, 9, 16)
    assert inputs.since_requested == date(2026, 9, 16)
    assert [d for d, _ in inputs.equity_series] == [date(2026, 9, 16), date(2026, 9, 18)]


def test_ac17_since_equal_to_first_snapshot_is_accepted(conn) -> None:
    _snapshot(conn, "2026-09-14")
    assert _ok(conn, date(2026, 9, 14)).start_date == date(2026, 9, 14)


def test_ac17_capital_comes_from_rule_set(conn) -> None:
    _snapshot(conn, "2026-09-14")
    assert _ok(conn).capital == Money(_rules().capital_usd)


def test_ac17_realized_pnl_is_summed_per_ticker_up_to_each_day(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    _trade(conn, "t1", "AAA", "2026-09-14", "5")  # closed on S: counted in start
    _trade(conn, "t2", "AAA", "2026-09-16", "7")
    _trade(conn, "t3", "BBB", "2026-09-17", "-3")
    _trade(conn, "t4", "BBB", "2026-09-19", "100")  # after E: excluded
    inputs = _ok(conn)
    assert dict(inputs.realized_start_by_ticker) == {"AAA": Money(Decimal(5))}
    assert dict(inputs.realized_end_by_ticker) == {
        "AAA": Money(Decimal(12)),
        "BBB": Money(Decimal(-3)),
    }


def test_ac17_realized_uses_et_day_not_utc_day(conn) -> None:
    _snapshot(conn, "2026-09-14")
    _snapshot(conn, "2026-09-15")
    _decision(conn, "entry-late", Origin.llm)
    closed_at = datetime(2026, 9, 16, 2, 0, tzinfo=UTC)  # 09-15 22:00 ET
    trade = Trade(
        "late", "AAA", closed_at, closed_at, "entry-late", (), ExitReason.llm,
        Money(Decimal(9)), Money(Decimal(0)), 1,
    )  # fmt: skip
    assert isinstance(portfolio_repo.insert_trade(conn, trade), Ok)
    assert dict(_ok(conn).realized_end_by_ticker) == {"AAA": Money(Decimal(9))}


def test_ac17_marks_read_on_start_and_end_day_only(conn) -> None:
    for day in ("2026-09-14", "2026-09-16", "2026-09-18"):
        _snapshot(conn, day)
    _mark(conn, "2026-09-14", "AAA", "1")
    _mark(conn, "2026-09-16", "AAA", "50")
    _mark(conn, "2026-09-18", "AAA", "4")
    _mark(conn, "2026-09-18", "BBB", "-2")
    inputs = _ok(conn)
    assert dict(inputs.unreal_start_by_ticker) == {"AAA": Money(Decimal(1))}
    assert inputs.unreal_end_by_ticker is not None
    assert dict(inputs.unreal_end_by_ticker) == {
        "AAA": Money(Decimal(4)),
        "BBB": Money(Decimal(-2)),
    }
    assert "position_marks" not in inputs.missing


def test_ac17_missing_start_mark_means_empty_start_marks(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    _mark(conn, "2026-09-18", "AAA", "4")
    assert dict(_ok(conn).unreal_start_by_ticker) == {}


def test_ac17_since_without_mark_on_that_day_has_empty_start_marks(conn) -> None:
    for day in ("2026-09-14", "2026-09-16", "2026-09-18"):
        _snapshot(conn, day)
    _mark(conn, "2026-09-14", "AAA", "1")
    _mark(conn, "2026-09-18", "AAA", "4")
    assert dict(_ok(conn, date(2026, 9, 16)).unreal_start_by_ticker) == {}


def test_ac17_no_end_marks_while_holding_is_missing_position_marks(conn) -> None:
    _snapshot(conn, "2026-09-14")
    _snapshot(conn, "2026-09-18")
    now = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
    held = Position("AAA", Quantity(1), Price(Decimal(10)), now, Price(Decimal(10)), False)
    assert isinstance(portfolio_repo.upsert_position(conn, held), Ok)
    inputs = _ok(conn)
    assert inputs.unreal_end_by_ticker is None
    assert "position_marks" in inputs.missing


def test_ac17_no_end_marks_and_no_positions_is_empty_not_missing(conn) -> None:
    _snapshot(conn, "2026-09-14")
    inputs = _ok(conn)
    assert inputs.unreal_end_by_ticker is not None
    assert dict(inputs.unreal_end_by_ticker) == {}
    assert "position_marks" not in inputs.missing


def test_ac17_spy_exact_rows_are_not_substituted(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    _spy(conn, "2026-09-14", "500")
    _spy(conn, "2026-09-18", "510")
    inputs = _ok(conn)
    assert inputs.benchmark_start is not None and inputs.benchmark_end is not None
    assert (inputs.benchmark_start.day, inputs.benchmark_start.substituted) == (
        date(2026, 9, 14),
        False,
    )
    assert inputs.benchmark_start.close == Decimal(500)
    assert (inputs.benchmark_end.close, inputs.benchmark_end.substituted) == (Decimal(510), False)


def test_ac17_spy_end_substitutes_latest_row_before_e(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    _spy(conn, "2026-09-14", "500")
    _spy(conn, "2026-09-16", "505")
    _spy(conn, "2026-09-17", "507")
    inputs = _ok(conn)
    assert inputs.benchmark_end is not None
    assert inputs.benchmark_end.day == date(2026, 9, 17)
    assert inputs.benchmark_end.close == Decimal(507)
    assert inputs.benchmark_end.substituted is True


def test_ac17_spy_start_substitutes_within_five_days_boundary(conn) -> None:
    for day in ("2026-09-14", "2026-09-30"):
        _snapshot(conn, day)
    _spy(conn, "2026-09-19", "500")  # S + 5 days: allowed
    _spy(conn, "2026-09-30", "510")
    inputs = _ok(conn)
    assert inputs.benchmark_start is not None
    assert inputs.benchmark_start.day == date(2026, 9, 19)
    assert "benchmark_prices" not in inputs.missing


def test_ac17_spy_start_six_days_away_is_missing(conn) -> None:
    for day in ("2026-09-14", "2026-09-30"):
        _snapshot(conn, day)
    _spy(conn, "2026-09-20", "500")  # S + 6 days: too far
    _spy(conn, "2026-09-30", "510")
    inputs = _ok(conn)
    assert inputs.benchmark_start is None
    assert "benchmark_prices" in inputs.missing


def test_ac17_spy_end_six_days_before_is_missing(conn) -> None:
    for day in ("2026-09-14", "2026-09-30"):
        _snapshot(conn, day)
    _spy(conn, "2026-09-14", "500")
    _spy(conn, "2026-09-24", "505")  # E - 6 days: too far
    inputs = _ok(conn)
    assert inputs.benchmark_end is None
    assert "benchmark_prices" in inputs.missing


def test_ac17_empty_benchmark_table_is_missing_benchmark_prices(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    inputs = _ok(conn)
    assert inputs.benchmark_start is None and inputs.benchmark_end is None
    assert "benchmark_prices" in inputs.missing


def test_ac17_non_spy_benchmark_rows_are_ignored(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    upsert_benchmark_price(conn, "QQQ", "2026-09-14", Price(Decimal(1)), datetime.now(UTC))
    upsert_benchmark_price(conn, "QQQ", "2026-09-18", Price(Decimal(2)), datetime.now(UTC))
    assert _ok(conn).benchmark_start is None


def test_ac17_equity_series_is_ordered_equity_from_start(conn) -> None:
    for day, equity in (("2026-09-14", "1000"), ("2026-09-15", "1100"), ("2026-09-16", "1050")):
        _snap_equity(conn, day, equity)
    inputs = _ok(conn)
    assert inputs.equity_series == (
        (date(2026, 9, 14), Money(Decimal(1000))),
        (date(2026, 9, 15), Money(Decimal(1100))),
        (date(2026, 9, 16), Money(Decimal(1050))),
    )


def test_ac17_kill_switch_counts_only_loss_limit_halts_from_start(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    _cycle(conn, "c-old", "2026-09-10T15:00:00+00:00", "halted", "daily loss limit")
    _cycle(conn, "c-hit", "2026-09-15T15:00:00+00:00", "halted", "weekly loss limit reached")
    _cycle(conn, "c-skip", "2026-09-16T15:00:00+00:00", "halted", "engine is halted")
    _cycle(conn, "c-ok", "2026-09-17T15:00:00+00:00", "ok", None)
    events = _ok(conn).kill_switch_events
    assert events == (("c-hit", "weekly loss limit reached"),)


def test_ac17_kill_switch_respects_since_day(conn) -> None:
    for day in ("2026-09-14", "2026-09-16", "2026-09-18"):
        _snapshot(conn, day)
    _cycle(conn, "c-1", "2026-09-15T15:00:00+00:00", "halted", "loss limit")
    _cycle(conn, "c-2", "2026-09-16T15:00:00+00:00", "halted", "loss limit")
    assert [cid for cid, _ in _ok(conn, date(2026, 9, 16)).kill_switch_events] == ["c-2"]


def test_ac17_no_halts_gives_zero_events(conn) -> None:
    _snapshot(conn, "2026-09-14")
    assert _ok(conn).kill_switch_events == ()


def test_ac17_manual_close_count_only_manual_paper_trades(conn) -> None:
    _snapshot(conn, "2026-09-14")
    _decision(conn, "x-paper", Origin.manual)
    _decision(conn, "x-live", Origin.manual, mode="live")
    _decision(conn, "x-rule", Origin.rule_exit)
    _trade(conn, "m1", "AAA", "2026-09-15", "1", ExitReason.manual, "x-paper")
    _trade(conn, "m2", "BBB", "2026-09-15", "1", ExitReason.manual, "x-paper")
    _trade(conn, "m3", "CCC", "2026-09-15", "1", ExitReason.manual, "x-live")
    _trade(conn, "r1", "DDD", "2026-09-15", "1", ExitReason.stop_loss, "x-rule")
    _trade(conn, "l1", "EEE", "2026-09-15", "1", ExitReason.llm)
    assert _ok(conn).manual_close_count == 2


def test_ac17_manual_close_zero_without_trades(conn) -> None:
    _snapshot(conn, "2026-09-14")
    assert _ok(conn).manual_close_count == 0


def test_ac17_window_fills_and_orders_are_limited_to_start_through_end(conn) -> None:
    for day in ("2026-09-14", "2026-09-18"):
        _snapshot(conn, day)
    _decision(conn, "d-o", Origin.llm)
    approval = Approval(
        "ap", "d-o", Approver.system,
        datetime(2026, 9, 14, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC),
    )  # fmt: skip
    assert isinstance(insert_approval(conn, approval), Ok)
    for oid, day in (("o-before", "13"), ("o-start", "14"), ("o-in", "16"), ("o-after", "19")):
        order = Order(
            oid, "d-o", "ap", f"cl-{oid}", None, "paper", Side.buy, Quantity(1), "market",
            OrderStatus.filled, datetime.fromisoformat(f"2026-09-{day}T15:00:00+00:00"), None,
        )  # fmt: skip
        assert isinstance(insert_order(conn, order), Ok)
        fill = Fill(
            f"f-{oid}", oid, datetime.fromisoformat(f"2026-09-{day}T15:00:01+00:00"),
            Quantity(1), Price(Decimal(10)), Money(Decimal(0)),
        )  # fmt: skip
        assert isinstance(insert_fill(conn, fill), Ok)
    inputs = _ok(conn)
    # window is (S, E] to match PnL(E) - PnL(S): before/start/after-E are excluded
    assert inputs.window_order_count == 1
    assert sorted(f.id for f in inputs.window_fills) == ["f-o-in"]
