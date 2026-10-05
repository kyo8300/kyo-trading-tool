"""T-3 / AC-14: approval -> order -> settle exists only in `engine/execution.py`.

The static scan is generic (every `.py` under `src/trader` except
`execution.py` and the modules that *define* the functions), so a later
`manual_close.py` is covered automatically and a missing file cannot fail it.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import trader
from trader.broker.fake_broker import FakeBroker
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.models import Action, Decision, ExitReason, Origin, RuleCheck
from trader.domain.money import Money, Price
from trader.engine.execution import ExecutionDeps, execute_decisions
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger.db import open_db, transaction
from trader.market.data_provider import MarketClock

_SRC_ROOT = Path(trader.__file__).resolve().parent
_EXECUTION = _SRC_ROOT / "engine" / "execution.py"
_GUARDED = frozenset({"resolve_approval", "execute", "poll_and_settle"})
_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_MARKET_CLOCK = MarketClock(is_open=True, next_open=_NOW, next_close=_NOW)


_EXECUTOR_MODULES = frozenset({"order_executor", "executor"})


def _called_name(node: ast.Call) -> str | None:
    """Name of a guarded call; `conn.execute(...)` (sqlite) is not one."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if func.attr != "execute":
            return func.attr
        if isinstance(func.value, ast.Name) and func.value.id in _EXECUTOR_MODULES:
            return func.attr
    return None


def _guarded_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (name := _called_name(node)) in _GUARDED
    ]


def _offenders(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*.py")) if p != _EXECUTION and _guarded_calls(p)]


def test_ac14_only_execution_py_calls_approval_execute_and_settle() -> None:
    assert _offenders(_SRC_ROOT) == []


def test_ac14_execution_py_calls_all_three_guarded_functions() -> None:
    assert set(_guarded_calls(_EXECUTION)) == _GUARDED


def test_ac14_scanner_flags_a_planted_bypass(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def f(c):\n    return poll_and_settle(c)\n\n"
        "def g(m):\n    return order_executor.execute(1)\n",
        encoding="utf-8",
    )
    assert _offenders(tmp_path) == [planted]


def test_ac14_run_cycle_goes_through_execute_decisions() -> None:
    tree = ast.parse((_SRC_ROOT / "engine" / "cycle.py").read_text(encoding="utf-8"))
    names = {
        n for node in ast.walk(tree) if isinstance(node, ast.Call) and (n := _called_name(node))
    }
    assert "execute_decisions" in names


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "trader.sqlite3").value
    with transaction(connection):
        portfolio_repo.insert_rule_set(
            connection,
            sha256="ruleset-sha",
            approved_by="kyo",
            approved_at="2026-01-01T00:00:00+00:00",
            capital_usd=Money(Decimal("500")),
            content_yaml="capital_usd: '500'\n",
        )
        portfolio_repo.insert_cycle(connection, cycle_id="cycle-1", started_at=_NOW, mode="live")
    yield connection
    connection.close()


def _decision(**overrides: object) -> Decision:
    base = dict(
        id="dec-1",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="live",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="x",
        evidence_mention_ids=(),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="ok",
        proposed_notional=Money(Decimal("75.00")),
        reference_price=Price(Decimal("10.0000")),
    )
    base.update(overrides)
    return Decision(**base)  # type: ignore[arg-type]


def _deps(mode: TradingMode) -> ExecutionDeps:
    return ExecutionDeps(
        mode=mode, broker=FakeBroker(), clock=FixedClock(_NOW), sleep=lambda _s: None
    )


def test_approval_err_decision_yields_no_order_and_an_error(conn) -> None:
    summary = execute_decisions(_deps(TradingMode.live), conn, (_decision(),), {}, _MARKET_CLOCK)

    assert (summary.orders, summary.fills) == (0, 0)
    assert len(summary.results) == 1
    result = summary.results[0]
    assert result.decision_id == "dec-1"
    assert result.order is None
    assert result.poll is None
    assert result.error


def test_rejected_and_hold_decisions_are_skipped_without_results(conn) -> None:
    decisions = (
        _decision(id="d-rej", rule_check=RuleCheck.rejected),
        _decision(id="d-hold", action=Action.hold),
    )
    summary = execute_decisions(
        _deps(TradingMode.live), conn, decisions, {"d-hold": ExitReason.manual}, _MARKET_CLOCK
    )
    assert (summary.orders, summary.fills, summary.results) == (0, 0, ())
