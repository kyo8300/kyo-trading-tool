"""T-12 / AC-18: kill switch cancels open orders and persists `halted`
even when cancellation itself fails; `resume` clears it (R-19)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.broker.broker import BrokerError
from trader.broker.fake_broker import FakeBroker
from trader.domain.clock import FixedClock
from trader.domain.money import Money
from trader.domain.result import Err, Ok
from trader.engine import kill_switch
from trader.ledger.db import open_db
from trader.rules.loss_limits import LossLimitBreach

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)

_BREACH = LossLimitBreach(
    kind="daily",
    observed=Money(Decimal("-20.00")),
    limit=Money(Decimal("15.00")),
    reason="daily P&L -20.00 <= -15.00 (realized -20.00 + unrealized 0.00)",
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "trader.sqlite3"
    open_db(path).value.close()
    return path


def test_trigger_cancels_open_orders_and_persists_halted(db_path: Path) -> None:
    conn = open_db(db_path).value
    broker = FakeBroker().with_open_orders(2)

    result = kill_switch.trigger(_BREACH, broker, conn, FixedClock(_NOW))

    assert isinstance(result, Ok)
    outcome = result.value
    assert outcome.canceled_count == 2
    assert outcome.cancel_error is None
    assert kill_switch.is_halted(conn)
    conn.close()


def test_halted_state_survives_reopening_the_database(db_path: Path) -> None:
    conn = open_db(db_path).value
    broker = FakeBroker().with_open_orders(1)
    kill_switch.trigger(_BREACH, broker, conn, FixedClock(_NOW))
    conn.close()

    reopened = open_db(db_path).value
    assert kill_switch.is_halted(reopened)
    info = kill_switch.halt_info(reopened)
    assert info is not None
    assert info.reason == _BREACH.reason
    reopened.close()


def test_resume_clears_halted_state(db_path: Path) -> None:
    conn = open_db(db_path).value
    broker = FakeBroker().with_open_orders(0)
    kill_switch.trigger(_BREACH, broker, conn, FixedClock(_NOW))
    assert kill_switch.is_halted(conn)

    resume_result = kill_switch.resume(conn)

    assert isinstance(resume_result, Ok)
    assert not kill_switch.is_halted(conn)
    assert kill_switch.halt_info(conn) is None
    conn.close()


def test_halted_is_persisted_even_when_cancel_all_open_fails(db_path: Path) -> None:
    conn = open_db(db_path).value

    class _FailingCancelBroker(FakeBroker):
        def cancel_all_open(self):  # type: ignore[override]
            return Err(BrokerError("simulated cancel failure"))

    broker = _FailingCancelBroker()

    result = kill_switch.trigger(_BREACH, broker, conn, FixedClock(_NOW))

    assert isinstance(result, Ok)
    outcome = result.value
    assert outcome.canceled_count is None
    assert outcome.cancel_error == "simulated cancel failure"
    assert kill_switch.is_halted(conn)
    conn.close()
