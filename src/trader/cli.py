"""Typer CLI entrypoint.

This module only wires up subcommands. Command bodies read configuration and
perform work lazily (inside the function), never at import time, so that
`--help` works without any environment variables set (AC-29).

T-1 provides stubs only: every command prints a human-readable "not
implemented yet" message and exits with status 1. Later tasks (T-5, T-7,
T-12, T-13, T-14) replace the bodies with real behavior.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from trader.domain.result import Result
    from trader.report import Period
    from trader.sources import AdapterRegistry

app = typer.Typer(add_completion=False, no_args_is_help=True)
rules_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(rules_app, name="rules")


def _not_implemented(command: str) -> None:
    typer.echo(f"trader {command}: not implemented yet", err=True)
    raise typer.Exit(code=1)


_DEFAULT_SOURCES_DIR = "data/sources"
_DEFAULT_RULES_PATH = "rules/trading-rules.yaml"
_DEFAULT_DB_PATH = "var/trader.sqlite3"


def _compute_data_dir_sha256(data_dir: Path) -> str:
    """Fingerprint the contents of `data_dir` for ingest idempotency.

    Prefers the well-known `tweets.json` file (R-1's serenity layout); falls
    back to hashing every regular file's name + content so other adapters
    (e.g. a test double with no `tweets.json`) still get a stable, content-
    sensitive fingerprint.
    """
    tweets_file = data_dir / "tweets.json"
    if tweets_file.is_file():
        return hashlib.sha256(tweets_file.read_bytes()).hexdigest()

    hasher = hashlib.sha256()
    if data_dir.is_dir():
        for path in sorted(data_dir.glob("*")):
            if path.is_file():
                hasher.update(path.name.encode("utf-8"))
                hasher.update(path.read_bytes())
    return hasher.hexdigest()


class _IngestWriteError(RuntimeError):
    """Raised inside the ingest transaction to trigger a rollback."""


def run_ingest(
    registry: AdapterRegistry,
    source_id: str,
    data_dir: Path,
    db_path: Path,
    now: datetime,
) -> Result[str, str]:
    """Load `source_id`'s data via `registry`, then record it in the ledger.

    Returns `Ok(message)` on success (including the idempotent "unchanged"
    case) and `Err(message)` on any failure. Neither message contains a
    filesystem path or raw record data (N-6). The DB is never touched if
    loading/validating the source data fails.
    """
    from trader.domain.result import Err, Ok
    from trader.ledger.db import open_db, transaction
    from trader.ledger.source_repository import get_source, insert_mentions_many, upsert_source

    adapter_result = registry.get(source_id)
    if isinstance(adapter_result, Err):
        return Err(adapter_result.error.message)
    adapter = adapter_result.value

    load_result = adapter.load(data_dir)
    if isinstance(load_result, Err):
        error = load_result.error
        if error.first_errors:
            detail = "; ".join(error.first_errors)
            return Err(f"{error.message}: {detail}")
        return Err(error.message)
    mentions = load_result.value

    file_sha256 = _compute_data_dir_sha256(data_dir)

    db_result = open_db(db_path)
    if isinstance(db_result, Err):
        return Err(db_result.error.message)
    conn = db_result.value

    try:
        existing = get_source(conn, source_id)
        if existing is not None and existing["file_sha256"] == file_sha256:
            return Ok("変更なし")

        try:
            with transaction(conn) as tx:
                upsert_result = upsert_source(
                    tx,
                    source_id=source_id,
                    name=source_id,
                    imported_at=now,
                    file_sha256=file_sha256,
                    record_count=len(mentions),
                )
                if isinstance(upsert_result, Err):
                    raise _IngestWriteError(upsert_result.error.message)

                insert_result = insert_mentions_many(tx, mentions)
                if isinstance(insert_result, Err):
                    raise _IngestWriteError(insert_result.error.message)
                inserted = insert_result.value
        except (_IngestWriteError, sqlite3.Error) as exc:
            return Err(str(exc))
    finally:
        conn.close()

    return Ok(f"{len(mentions)} 件取り込み(新規 {inserted} 件)")


@app.command("ingest")
def ingest(
    source: str = typer.Option("serenity", "--source", help="Source id to ingest"),
    data_dir: str | None = typer.Option(
        None,
        "--data-dir",
        help="Directory holding the source's files (default: data/sources/<source>)",
    ),
    db: str | None = typer.Option(
        None, "--db", help="Path to the ledger database (default: $TRADER_DB_PATH)"
    ),
) -> None:
    """Import aggregated data (e.g. serenity tweets.json) into the ledger."""
    from trader.domain.result import Err
    from trader.sources import default_registry

    data_dir_path = Path(data_dir) if data_dir else Path(_DEFAULT_SOURCES_DIR) / source
    db_path = Path(db or os.environ.get("TRADER_DB_PATH", _DEFAULT_DB_PATH))

    result = run_ingest(
        registry=default_registry(),
        source_id=source,
        data_dir=data_dir_path,
        db_path=db_path,
        now=datetime.now(UTC),
    )
    if isinstance(result, Err):
        typer.echo(f"trader ingest: {result.error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"trader ingest: {result.value}")


@app.command("run-cycle")
def run_cycle() -> None:
    """Run a single decision/order cycle."""
    _not_implemented("run-cycle")


@app.command("approve")
def approve(
    decision_id: str = typer.Argument(..., help="Decision id to approve"),
    expires_at: str | None = typer.Option(
        None,
        "--expires-at",
        help=(
            "ISO 8601 UTC timestamp this approval expires at (the next market "
            "session close, e.g. 2026-01-05T21:00:00+00:00). Required: fetching "
            "the real market clock here would make 'approve' depend on the "
            "network (N-9). Run 'trader status' or check the broker for the "
            "current session's close time."
        ),
    ),
    db: str | None = typer.Option(
        None, "--db", help="Path to the ledger database (default: $TRADER_DB_PATH)"
    ),
) -> None:
    """Record a human (`kyo`) approval for a decision that passed rule checks (R-17)."""
    from datetime import datetime as dt

    from trader.domain.clock import SystemClock
    from trader.domain.result import Err
    from trader.engine.approval import record_human_approval
    from trader.ledger.db import open_db
    from trader.market.data_provider import MarketClock

    if expires_at is None:
        typer.echo(
            "trader approve: --expires-at is required (see 'trader approve --help')",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        parsed_expires_at = dt.fromisoformat(expires_at)
    except ValueError:
        typer.echo(
            f"trader approve: --expires-at is not a valid ISO 8601 timestamp: {expires_at}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    db_path = Path(db or os.environ.get("TRADER_DB_PATH", _DEFAULT_DB_PATH))
    db_result = open_db(db_path)
    if isinstance(db_result, Err):
        typer.echo(f"trader approve: {db_result.error.message}", err=True)
        raise typer.Exit(code=1)
    conn = db_result.value

    try:
        market_clock = MarketClock(
            is_open=False, next_open=parsed_expires_at, next_close=parsed_expires_at
        )
        result = record_human_approval(conn, decision_id, SystemClock(), market_clock)
    finally:
        conn.close()

    if isinstance(result, Err):
        typer.echo(f"trader approve: {result.error.message}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"trader approve: approved decision {decision_id} (expires {parsed_expires_at.isoformat()})"
    )


def _date_in_period(value: datetime, period: Period) -> bool:
    """Return whether `value`'s date falls within `period`."""
    day = value.date()
    if period.start is not None and day < period.start:
        return False
    if period.end is not None and day > period.end:
        return False
    return True


@app.command("report")
def report(
    db: str | None = typer.Option(
        None, "--db", help="Path to the ledger database (default: $TRADER_DB_PATH)"
    ),
    rules: str | None = typer.Option(
        None, "--rules", help="Path to trading-rules.yaml (default: $TRADER_RULES_PATH)"
    ),
    from_: str | None = typer.Option(
        None, "--from", help="Report period start date, YYYY-MM-DD (default: unbounded)"
    ),
    to: str | None = typer.Option(
        None, "--to", help="Report period end date, YYYY-MM-DD (default: unbounded)"
    ),
) -> None:
    """Print the performance report: metrics (R-22) and live estimate (R-23)."""
    from datetime import date as date_cls

    from trader.domain.money import Money
    from trader.domain.result import Err
    from trader.ledger.db import open_db
    from trader.ledger.portfolio_repository import (
        list_equity_snapshots,
        list_positions,
        list_trades,
    )
    from trader.ledger.repository import list_all_fills, list_decisions, list_orders
    from trader.report import Period, compute, estimate, filter_trades_by_period, render_report
    from trader.rules import load_rules

    rules_path = Path(rules or os.environ.get("TRADER_RULES_PATH", _DEFAULT_RULES_PATH))
    db_path = Path(db or os.environ.get("TRADER_DB_PATH", _DEFAULT_DB_PATH))

    if not db_path.exists():
        typer.echo("trader report: no database found (run 'trader ingest' first)", err=True)
        raise typer.Exit(code=1)

    rules_result = load_rules(rules_path)
    if isinstance(rules_result, Err):
        typer.echo(f"trader report: {rules_result.error.message}", err=True)
        raise typer.Exit(code=1)
    rule_set = rules_result.value

    try:
        period_start = date_cls.fromisoformat(from_) if from_ else None
        period_end = date_cls.fromisoformat(to) if to else None
    except ValueError:
        typer.echo("trader report: --from/--to must be YYYY-MM-DD", err=True)
        raise typer.Exit(code=1) from None
    period = Period(start=period_start, end=period_end)

    db_result = open_db(db_path)
    if isinstance(db_result, Err):
        typer.echo(f"trader report: {db_result.error.message}", err=True)
        raise typer.Exit(code=1)
    conn = db_result.value

    try:
        trades = list_trades(conn)
        decisions = list_decisions(conn)
        snapshots = list_equity_snapshots(conn)
        positions = list_positions(conn)

        metrics = compute(trades, decisions, snapshots, positions, period)

        fills = tuple(
            fill for fill in list_all_fills(conn) if _date_in_period(fill.filled_at, period)
        )
        order_count = sum(
            1
            for order in list_orders(conn)
            if order.submitted_at is not None and _date_in_period(order.submitted_at, period)
        )

        live_estimate = estimate(
            paper_pnl=metrics.period_pnl,
            fills=fills,
            order_count=order_count,
            capital=Money(rule_set.capital_usd),
            cost=rule_set.cost_assumptions,
        )

        period_trades = filter_trades_by_period(trades, period)
        entry_decision_ids = {t.entry_decision_id for t in period_trades}
        entry_decisions = {
            decision.id: decision for decision in decisions if decision.id in entry_decision_ids
        }

        output = render_report(metrics, live_estimate, period_trades, entry_decisions, period)
    finally:
        conn.close()

    typer.echo(output)


@rules_app.command("approve")
def rules_approve(
    rules: str | None = typer.Option(
        None, "--rules", help="Path to trading-rules.yaml (default: $TRADER_RULES_PATH)"
    ),
    db: str | None = typer.Option(
        None, "--db", help="Path to the ledger database (default: $TRADER_DB_PATH)"
    ),
) -> None:
    """Verify the current rules/trading-rules.yaml, write its lock, and
    record it as an approved rule set (R-10).

    Requires `approved_by` and `approved_at` to already be filled in by a
    human. Re-approving an already-recorded rule set (same SHA-256)
    succeeds without inserting a duplicate row.
    """
    from trader.domain.money import Money
    from trader.domain.result import Err
    from trader.ledger.db import open_db
    from trader.ledger.portfolio_repository import get_rule_set, insert_rule_set
    from trader.rules import RulesLock, load_rules, sha256_of_file, write_lock

    rules_path = Path(rules or os.environ.get("TRADER_RULES_PATH", _DEFAULT_RULES_PATH))
    db_path = Path(db or os.environ.get("TRADER_DB_PATH", _DEFAULT_DB_PATH))

    rules_result = load_rules(rules_path)
    if isinstance(rules_result, Err):
        typer.echo(f"trader rules approve: {rules_result.error.message}", err=True)
        raise typer.Exit(code=1)
    rule_set = rules_result.value

    if not rule_set.approved_by.strip() or not rule_set.approved_at.strip():
        typer.echo(
            "trader rules approve: rules file needs 'approved_by' and 'approved_at' "
            "filled in before approving",
            err=True,
        )
        raise typer.Exit(code=1)

    sha_result = sha256_of_file(rules_path)
    if isinstance(sha_result, Err):
        typer.echo(f"trader rules approve: {sha_result.error.message}", err=True)
        raise typer.Exit(code=1)
    sha256 = sha_result.value

    lock_path = rules_path.parent / "trading-rules.lock"
    lock = RulesLock(
        sha256=sha256, approved_by=rule_set.approved_by, approved_at=rule_set.approved_at
    )
    write_result = write_lock(lock_path, lock)
    if isinstance(write_result, Err):
        typer.echo(f"trader rules approve: {write_result.error.message}", err=True)
        raise typer.Exit(code=1)

    db_result = open_db(db_path)
    if isinstance(db_result, Err):
        typer.echo(f"trader rules approve: {db_result.error.message}", err=True)
        raise typer.Exit(code=1)
    conn = db_result.value

    try:
        if get_rule_set(conn, sha256) is not None:
            typer.echo("trader rules approve: this rule set is already approved")
            return

        try:
            content_yaml = rules_path.read_text(encoding="utf-8")
        except OSError:
            typer.echo("trader rules approve: could not read rules file", err=True)
            raise typer.Exit(code=1) from None

        with conn:
            insert_result = insert_rule_set(
                conn,
                sha256=sha256,
                approved_by=rule_set.approved_by,
                approved_at=rule_set.approved_at,
                capital_usd=Money(rule_set.capital_usd),
                content_yaml=content_yaml,
            )
        if isinstance(insert_result, Err):
            typer.echo(f"trader rules approve: {insert_result.error.message}", err=True)
            raise typer.Exit(code=1)
    finally:
        conn.close()

    typer.echo(f"trader rules approve: approved rule set {sha256[:12]}")


@app.command("resume")
def resume(
    db: str | None = typer.Option(
        None, "--db", help="Path to the ledger database (default: $TRADER_DB_PATH)"
    ),
) -> None:
    """Clear the kill-switch halted state (R-19). Only kyo should run this."""
    from trader.domain.result import Err
    from trader.engine.kill_switch import is_halted
    from trader.engine.kill_switch import resume as clear_halt
    from trader.ledger.db import open_db

    db_path = Path(db or os.environ.get("TRADER_DB_PATH", _DEFAULT_DB_PATH))
    db_result = open_db(db_path)
    if isinstance(db_result, Err):
        typer.echo(f"trader resume: {db_result.error.message}", err=True)
        raise typer.Exit(code=1)
    conn = db_result.value

    try:
        if not is_halted(conn):
            typer.echo("trader resume: engine is not halted")
            return

        result = clear_halt(conn)
        if isinstance(result, Err):
            typer.echo(f"trader resume: {result.error.message}", err=True)
            raise typer.Exit(code=1)
    finally:
        conn.close()

    typer.echo("trader resume: resumed")


@app.command("status")
def status(
    db: str | None = typer.Option(
        None, "--db", help="Path to the ledger database (default: $TRADER_DB_PATH)"
    ),
) -> None:
    """Show halted state, open positions, and the most recent cycle."""
    from trader.domain.result import Err
    from trader.engine.kill_switch import halt_info
    from trader.ledger.db import open_db
    from trader.ledger.portfolio_repository import list_positions

    db_path = Path(db or os.environ.get("TRADER_DB_PATH", _DEFAULT_DB_PATH))
    if not db_path.exists():
        typer.echo("trader status: no database found (run 'trader ingest' first)", err=True)
        raise typer.Exit(code=1)

    db_result = open_db(db_path)
    if isinstance(db_result, Err):
        typer.echo(f"trader status: {db_result.error.message}", err=True)
        raise typer.Exit(code=1)
    conn = db_result.value

    try:
        info = halt_info(conn)
        if info is None:
            typer.echo("engine: running")
        else:
            typer.echo(f"engine: HALTED ({info.reason}) at {info.halted_at.isoformat()}")

        positions = list_positions(conn)
        if positions:
            typer.echo("positions:")
            for position in positions:
                typer.echo(
                    f"  {position.ticker}: {position.qty.shares} shares "
                    f"@ avg {position.avg_cost.amount}"
                )
        else:
            typer.echo("positions: none")

        cycle_row = conn.execute(
            "SELECT id, started_at, finished_at, mode, outcome FROM cycles "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if cycle_row is None:
            typer.echo("last cycle: none")
        else:
            typer.echo(
                f"last cycle: {cycle_row['id']} ({cycle_row['mode']}) "
                f"started {cycle_row['started_at']} "
                f"finished {cycle_row['finished_at'] or '-'} "
                f"outcome {cycle_row['outcome'] or '-'}"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    app()
