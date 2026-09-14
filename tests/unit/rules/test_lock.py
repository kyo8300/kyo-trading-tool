"""AC-9: the rules lock guards against unapproved edits to trading-rules.yaml."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trader.cli import app
from trader.domain.result import Err, Ok
from trader.rules.lock import verify_lock

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"

runner = CliRunner()


def _copy_valid_rules(tmp_path: Path) -> Path:
    rules_path = tmp_path / "trading-rules.yaml"
    rules_path.write_text((FIXTURES / "valid.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return rules_path


def test_verify_lock_fails_when_lock_is_missing(tmp_path: Path) -> None:
    rules_path = _copy_valid_rules(tmp_path)
    lock_path = tmp_path / "trading-rules.lock"

    result = verify_lock(rules_path, lock_path)

    assert isinstance(result, Err)


def test_verify_lock_fails_when_sha_does_not_match(tmp_path: Path) -> None:
    rules_path = _copy_valid_rules(tmp_path)
    lock_path = tmp_path / "trading-rules.lock"
    lock_path.write_text(
        "sha256: 0000000000000000000000000000000000000000000000000000000000000000\n"
        "approved_by: kyo\n"
        "approved_at: 2026-09-14\n",
        encoding="utf-8",
    )

    result = verify_lock(rules_path, lock_path)

    assert isinstance(result, Err)


def test_rules_approve_writes_a_lock_that_verifies(tmp_path: Path) -> None:
    rules_path = _copy_valid_rules(tmp_path)
    db_path = tmp_path / "trader.sqlite3"
    lock_path = tmp_path / "trading-rules.lock"

    result = runner.invoke(
        app, ["rules", "approve", "--rules", str(rules_path), "--db", str(db_path)]
    )

    assert result.exit_code == 0, result.output
    verified = verify_lock(rules_path, lock_path)
    assert isinstance(verified, Ok)
    assert verified.value.approved_by == "kyo"


def test_rules_approve_is_idempotent_for_the_same_content(tmp_path: Path) -> None:
    rules_path = _copy_valid_rules(tmp_path)
    db_path = tmp_path / "trader.sqlite3"

    first = runner.invoke(
        app, ["rules", "approve", "--rules", str(rules_path), "--db", str(db_path)]
    )
    second = runner.invoke(
        app, ["rules", "approve", "--rules", str(rules_path), "--db", str(db_path)]
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output


def test_editing_the_rules_file_after_approval_invalidates_the_lock(tmp_path: Path) -> None:
    rules_path = _copy_valid_rules(tmp_path)
    db_path = tmp_path / "trader.sqlite3"
    lock_path = tmp_path / "trading-rules.lock"

    approve_result = runner.invoke(
        app, ["rules", "approve", "--rules", str(rules_path), "--db", str(db_path)]
    )
    assert approve_result.exit_code == 0, approve_result.output

    text = rules_path.read_text(encoding="utf-8")
    edited = text.replace('capital_usd: "500"', 'capital_usd: "501"')
    rules_path.write_text(edited, encoding="utf-8")

    result = verify_lock(rules_path, lock_path)

    assert isinstance(result, Err)


def test_rules_approve_fails_without_approved_by(tmp_path: Path) -> None:
    rules_path = tmp_path / "trading-rules.yaml"
    text = (FIXTURES / "valid.yaml").read_text(encoding="utf-8")
    text = text.replace('approved_by: "kyo"', 'approved_by: ""')
    rules_path.write_text(text, encoding="utf-8")
    db_path = tmp_path / "trader.sqlite3"
    lock_path = tmp_path / "trading-rules.lock"

    result = runner.invoke(
        app, ["rules", "approve", "--rules", str(rules_path), "--db", str(db_path)]
    )

    assert result.exit_code == 1
    assert not lock_path.exists()
