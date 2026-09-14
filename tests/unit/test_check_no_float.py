"""Unit tests for scripts/check_no_float_money.py detection logic (AC-23)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_no_float_money import check_file, check_paths  # noqa: E402


def _write(tmp_path: Path, name: str, source: str) -> Path:
    file_path = tmp_path / name
    file_path.write_text(source, encoding="utf-8")
    return file_path


def test_detects_float_variable_annotation(tmp_path: Path) -> None:
    file_path = _write(tmp_path, "a.py", "amount: float = 1.0\n")
    violations = check_file(file_path)
    assert len(violations) == 1
    assert "annotation" in violations[0].message


def test_detects_float_argument_annotation(tmp_path: Path) -> None:
    file_path = _write(tmp_path, "b.py", "def f(x: float) -> None:\n    pass\n")
    violations = check_file(file_path)
    assert len(violations) == 1
    assert "x" in violations[0].message


def test_detects_float_return_annotation(tmp_path: Path) -> None:
    file_path = _write(tmp_path, "c.py", "def f() -> float:\n    return 1.0\n")
    violations = check_file(file_path)
    assert len(violations) == 1
    assert "f" in violations[0].message


def test_detects_float_call(tmp_path: Path) -> None:
    file_path = _write(tmp_path, "d.py", "x = float('1.0')\n")
    violations = check_file(file_path)
    assert len(violations) == 1
    assert "float()" in violations[0].message


def test_no_violations_for_decimal_only_file(tmp_path: Path) -> None:
    file_path = _write(
        tmp_path,
        "e.py",
        "from decimal import Decimal\n\n\ndef f(x: Decimal) -> Decimal:\n    return x\n",
    )
    violations = check_file(file_path)
    assert violations == []


def test_check_paths_returns_empty_for_missing_directories(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert check_paths([missing]) == []


def test_check_paths_scans_directory_recursively(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    _write(nested, "f.py", "x = float(1)\n")
    violations = check_paths([tmp_path])
    assert len(violations) == 1
