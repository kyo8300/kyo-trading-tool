"""T-8 / AC-17: `load_readiness_inputs` assembles ledger data for readiness."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.domain.models import Price
from trader.domain.money import Money
from trader.domain.result import Err, Ok
from trader.ledger import portfolio_repository as portfolio_repo
from trader.ledger.benchmark_repository import upsert_benchmark_price
from trader.ledger.db import open_db
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
