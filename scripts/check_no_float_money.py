#!/usr/bin/env python3
"""AST-based static check that forbids `float` in money-sensitive code (N-1, AC-23).

Scans `src/trader/{domain,broker,ledger,rules}` (any directory that doesn't
exist is skipped) for:

  - `float` used in a type annotation (variable, argument, or return type)
  - a direct call to the builtin `float(...)`

Any violation is reported as `path:line: <message>` and the script exits 1.
With zero violations it exits 0 (including when none of the target
directories exist).

The detection logic is exposed as `check_paths` / `check_file` so tests can
exercise it against temporary files without depending on the real `src`
tree.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from pathlib import Path

DEFAULT_TARGET_DIRS: tuple[str, ...] = (
    "src/trader/domain",
    "src/trader/broker",
    "src/trader/ledger",
    "src/trader/rules",
)


class Violation:
    __slots__ = ("line", "message", "path")

    def __init__(self, path: Path, line: int, message: str) -> None:
        self.path = path
        self.line = line
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _annotation_contains_float(node: ast.expr | None) -> bool:
    if node is None:
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "float":
            return True
    return False


def _check_tree(tree: ast.AST, path: Path) -> list[Violation]:
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            if _annotation_contains_float(node.annotation):
                violations.append(Violation(path, node.lineno, "float used in variable annotation"))
        elif isinstance(node, ast.arg):
            if _annotation_contains_float(node.annotation):
                violations.append(
                    Violation(path, node.lineno, f"float used in annotation of '{node.arg}'")
                )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if _annotation_contains_float(node.returns):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        f"float used as return annotation of '{node.name}'",
                    )
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "float":
                violations.append(Violation(path, node.lineno, "call to builtin float()"))

    return violations


def check_file(path: Path) -> list[Violation]:
    """Return the violations found in a single Python file."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    return _check_tree(tree, path)


def check_paths(target_dirs: Iterable[Path]) -> list[Violation]:
    """Return all violations found under the given directories (recursively)."""
    violations: list[Violation] = []
    for target_dir in target_dirs:
        if not target_dir.exists():
            continue
        for py_file in sorted(target_dir.rglob("*.py")):
            violations.extend(check_file(py_file))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else DEFAULT_TARGET_DIRS
    target_dirs = [Path(arg) for arg in args]
    violations = check_paths(target_dirs)
    for violation in violations:
        print(str(violation))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
