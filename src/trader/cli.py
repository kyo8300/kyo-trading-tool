"""Typer CLI entrypoint.

This module only wires up subcommands. Command bodies read configuration and
perform work lazily (inside the function), never at import time, so that
`--help` works without any environment variables set (AC-29).

T-1 provides stubs only: every command prints a human-readable "not
implemented yet" message and exits with status 1. Later tasks (T-5, T-7,
T-12, T-13, T-14) replace the bodies with real behavior.
"""

from __future__ import annotations

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


@rules_app.command("approve")
def rules_approve() -> None:
    """Verify and lock the current rules/trading-rules.yaml."""
    _not_implemented("rules approve")


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
