"""Typer CLI entrypoint.

This module only wires up subcommands. Command bodies read configuration and
perform work lazily (inside the function), never at import time, so that
`--help` works without any environment variables set (AC-29).

T-1 provides stubs only: every command prints a human-readable "not
implemented yet" message and exits with status 1. Later tasks (T-5, T-7,
T-12, T-13, T-14) replace the bodies with real behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True)
rules_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(rules_app, name="rules")


def _not_implemented(command: str) -> None:
    typer.echo(f"trader {command}: not implemented yet", err=True)
    raise typer.Exit(code=1)


@app.command("ingest")
def ingest() -> None:
    """Import aggregated data (e.g. serenity tweets.json) into the ledger."""
    _not_implemented("ingest")


@app.command("run-cycle")
def run_cycle() -> None:
    """Run a single decision/order cycle."""
    _not_implemented("run-cycle")


@app.command("approve")
def approve(decision_id: str = typer.Argument(..., help="Decision id to approve")) -> None:
    """Approve a pending decision (live mode)."""
    _not_implemented("approve")


@app.command("report")
def report() -> None:
    """Print performance report."""
    _not_implemented("report")


_DEFAULT_RULES_PATH = "rules/trading-rules.yaml"
_DEFAULT_DB_PATH = "var/trader.sqlite3"


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
def resume() -> None:
    """Clear the kill-switch halted state."""
    _not_implemented("resume")


@app.command("status")
def status() -> None:
    """Show current engine state."""
    _not_implemented("status")


if __name__ == "__main__":
    app()
