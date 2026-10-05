"""Record SPY daily closes into `benchmark_prices` (live-readiness LR-13).

A failure here never stops the cycle: the caller reports it as
`CycleOutcome.benchmark_error`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from trader.domain.result import Err, Ok, Result
from trader.ledger.benchmark_repository import upsert_benchmark_price
from trader.ledger.portfolio_repository import list_equity_snapshots
from trader.market.data_provider import MarketDataProvider
from trader.rules import loss_limits

BENCHMARK_TICKER = "SPY"

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BenchmarkError:
    """A human-readable benchmark-recording error."""

    message: str


@dataclass(frozen=True, slots=True)
class BenchmarkOutcome:
    recorded: int
    skipped_nonpositive: int


def benchmark_start(conn: sqlite3.Connection, now: datetime) -> date:
    """Oldest equity snapshot date, or today's trading day if none exist."""
    snapshots = list_equity_snapshots(conn)
    if snapshots:
        return date.fromisoformat(snapshots[0].snapshot_date)
    return loss_limits.trading_day(now)


def record_benchmark(
    conn: sqlite3.Connection,
    market: MarketDataProvider,
    start: date,
    now: datetime,
) -> Result[BenchmarkOutcome, BenchmarkError]:
    """Upsert SPY closes from `start`; non-positive closes are skipped."""
    bars_result = market.daily_bars_since(BENCHMARK_TICKER, start)
    if isinstance(bars_result, Err):
        return Err(BenchmarkError(f"SPY bars unavailable: {bars_result.error.message}"))
    recorded = 0
    skipped = 0
    for bar in bars_result.value:
        if bar.close.amount <= 0:
            _log.warning("skipping non-positive SPY close on %s", bar.date.isoformat())
            skipped += 1
            continue
        saved = upsert_benchmark_price(conn, BENCHMARK_TICKER, bar.date.isoformat(), bar.close, now)
        if isinstance(saved, Err):
            return Err(BenchmarkError(saved.error.message))
        recorded += 1
    return Ok(BenchmarkOutcome(recorded, skipped))
