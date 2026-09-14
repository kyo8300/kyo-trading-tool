"""CLI smoke tests (AC-29): every subcommand's --help exits 0."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trader.cli import app

runner = CliRunner()

COMMANDS: list[list[str]] = [
    ["ingest", "--help"],
    ["run-cycle", "--help"],
    ["approve", "--help"],
    ["report", "--help"],
    ["rules", "approve", "--help"],
    ["resume", "--help"],
    ["status", "--help"],
]


@pytest.mark.parametrize("args", COMMANDS, ids=[" ".join(c[:-1]) for c in COMMANDS])
def test_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output


def test_ingest_without_a_data_dir_reports_a_human_readable_error() -> None:
    # T-7 replaced the stub body with a real implementation (see
    # tests/unit/sources/test_serenity_adapter.py for full coverage). With
    # no --data-dir and no data/sources/serenity/ present, it should fail
    # with a human-readable message rather than a stack trace.
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 1
    assert "not implemented" not in result.output
    assert "trader ingest:" in result.output
