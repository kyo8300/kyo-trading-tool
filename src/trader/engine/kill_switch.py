"""Kill switch (R-19): cancel open orders, halt, and persist why.

`trigger` always persists `halted` even when `broker.cancel_all_open()`
itself fails: the DB record's job is to *guarantee* no more orders are
submitted (checked by `cycle.run_cycle` at the top of every cycle, T-13),
so a broker-side cancellation failure must never prevent that. Both
failures (if any) are carried in the returned `KillSwitchOutcome` so the
caller can log/report everything instead of picking one to report.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from trader.broker.broker import Broker
from trader.domain.clock import Clock
from trader.domain.result import Err, Ok, Result
from trader.ledger.db import transaction
from trader.ledger.portfolio_repository import (
    delete_engine_state,
    get_engine_state,
    set_engine_state,
)
from trader.rules.loss_limits import LossLimitBreach

logger = logging.getLogger(__name__)

_HALTED_KEY = "halted"
_HALTED_REASON_KEY = "halted_reason"
_HALTED_AT_KEY = "halted_at"
_HALTED_VALUE = "true"


@dataclass(frozen=True, slots=True)
class KillSwitchError:
    """A human-readable kill-switch error (N-6)."""

    message: str


@dataclass(frozen=True, slots=True)
class KillSwitchOutcome:
    """What happened when the kill switch fired.

    `canceled_count` / `cancel_error` are mutually exclusive: cancellation
    either reported how many open orders it attempted to cancel, or it
    failed and `cancel_error` explains why. Either way `halted_at` is when
    the halt was persisted (it always succeeds, or `trigger` returns `Err`).
    """

    canceled_count: int | None
    cancel_error: str | None
    halted_at: datetime


@dataclass(frozen=True, slots=True)
class HaltInfo:
    """The persisted reason/time an already-halted engine stopped."""

    reason: str
    halted_at: datetime


def trigger(
    breach: LossLimitBreach,
    broker: Broker,
    conn: sqlite3.Connection,
    clock: Clock,
) -> Result[KillSwitchOutcome, KillSwitchError]:
    """Cancel every open order, then persist `halted` regardless (R-19)."""
    cancel_result = broker.cancel_all_open()
    canceled_count = cancel_result.value if isinstance(cancel_result, Ok) else None
    cancel_error = cancel_result.error.message if isinstance(cancel_result, Err) else None

    now = clock.now()
    try:
        with transaction(conn) as tx:
            for key, value in (
                (_HALTED_KEY, _HALTED_VALUE),
                (_HALTED_REASON_KEY, breach.reason),
                (_HALTED_AT_KEY, now.isoformat()),
            ):
                set_result = set_engine_state(tx, key, value)
                if isinstance(set_result, Err):
                    raise RuntimeError(set_result.error.message)
    except (RuntimeError, sqlite3.Error) as exc:
        return Err(KillSwitchError(f"could not persist halted state: {exc}"))

    logger.warning("kill switch triggered (%s): %s", breach.kind, breach.reason)
    if cancel_error is not None:
        logger.warning("cancel_all_open failed while halting: %s", cancel_error)

    return Ok(
        KillSwitchOutcome(canceled_count=canceled_count, cancel_error=cancel_error, halted_at=now)
    )


def is_halted(conn: sqlite3.Connection) -> bool:
    """Return whether `engine_state.halted` is currently set (R-19)."""
    return get_engine_state(conn, _HALTED_KEY) == _HALTED_VALUE


def halt_info(conn: sqlite3.Connection) -> HaltInfo | None:
    """Return why/when the engine halted, or `None` if it is not halted."""
    if not is_halted(conn):
        return None
    reason = get_engine_state(conn, _HALTED_REASON_KEY) or ""
    halted_at_raw = get_engine_state(conn, _HALTED_AT_KEY)
    halted_at = (
        datetime.fromisoformat(halted_at_raw) if halted_at_raw else datetime.min.replace(tzinfo=UTC)
    )
    return HaltInfo(reason=reason, halted_at=halted_at)


def resume(conn: sqlite3.Connection) -> Result[None, KillSwitchError]:
    """Clear the `halted*` engine_state keys. `peak_equity` is untouched (R-19)."""
    try:
        with transaction(conn) as tx:
            delete_engine_state(tx, _HALTED_KEY)
            delete_engine_state(tx, _HALTED_REASON_KEY)
            delete_engine_state(tx, _HALTED_AT_KEY)
    except sqlite3.Error as exc:
        return Err(KillSwitchError(f"could not resume: {exc}"))
    return Ok(None)
