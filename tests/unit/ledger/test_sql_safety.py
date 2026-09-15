"""T-4: no SQL string concatenation anywhere under `src/trader/ledger`
(security.md: SQL must use parameterized queries only, no string concatenation).

Mirrors the AC-10 style static grep, but expressed as a portable regex scan
so it runs the same way under pytest as the other verify commands.
"""

from __future__ import annotations

import re
from pathlib import Path

_LEDGER_SRC = Path(__file__).resolve().parents[3] / "src" / "trader" / "ledger"

# f-strings that build SQL keywords, or `+` string concatenation next to a
# quote -- the two concatenation styles the rule forbids.
_FORBIDDEN = re.compile(r'f"(SELECT|INSERT|UPDATE|DELETE)|\+ *"')


def test_no_sql_string_concatenation_in_ledger() -> None:
    offenders = []
    for path in _LEDGER_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")

    assert offenders == [], "\n".join(offenders)
