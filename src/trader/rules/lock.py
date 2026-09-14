"""Rules lock: SHA-256 pinning of `rules/trading-rules.yaml` (R-10).

The lock file is a plain 3-line text file:

    sha256: <hex>
    approved_by: <name>
    approved_at: <date>

`verify_lock` treats a missing lock, a hash mismatch, and a malformed lock
file identically -- all are `Err`, because in every case the current rules
file cannot be trusted to be the one a human approved. `write_lock` is only
ever meant to be called from `trader rules approve`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from trader.domain.result import Err, Ok, Result
from trader.rules.schema import RulesError

_SHA_PREFIX = "sha256: "
_APPROVED_BY_PREFIX = "approved_by: "
_APPROVED_AT_PREFIX = "approved_at: "


@dataclass(frozen=True, slots=True)
class RulesLock:
    """A parsed `trading-rules.lock` file."""

    sha256: str
    approved_by: str
    approved_at: str


def sha256_of_file(path: Path) -> Result[str, RulesError]:
    """Compute the SHA-256 hex digest of the file at `path`."""
    try:
        data = path.read_bytes()
    except OSError:
        return Err(RulesError("could not read rules file"))
    return Ok(hashlib.sha256(data).hexdigest())


def render_lock(lock: RulesLock) -> str:
    """Render a `RulesLock` as the 3-line lock file text."""
    return (
        f"{_SHA_PREFIX}{lock.sha256}\n"
        f"{_APPROVED_BY_PREFIX}{lock.approved_by}\n"
        f"{_APPROVED_AT_PREFIX}{lock.approved_at}\n"
    )


def parse_lock(text: str) -> Result[RulesLock, RulesError]:
    """Parse the 3-line lock file format. Any deviation is `Err`."""
    lines = text.splitlines()
    if len(lines) != 3:
        return Err(RulesError("lock が無い、または形式が不正"))

    sha_line, approved_by_line, approved_at_line = lines
    if (
        not sha_line.startswith(_SHA_PREFIX)
        or not approved_by_line.startswith(_APPROVED_BY_PREFIX)
        or not approved_at_line.startswith(_APPROVED_AT_PREFIX)
    ):
        return Err(RulesError("lock が無い、または形式が不正"))

    sha256 = sha_line[len(_SHA_PREFIX) :]
    approved_by = approved_by_line[len(_APPROVED_BY_PREFIX) :]
    approved_at = approved_at_line[len(_APPROVED_AT_PREFIX) :]
    if not sha256:
        return Err(RulesError("lock が無い、または形式が不正"))

    return Ok(RulesLock(sha256=sha256, approved_by=approved_by, approved_at=approved_at))


def verify_lock(yaml_path: Path, lock_path: Path) -> Result[RulesLock, RulesError]:
    """Verify that `lock_path` records the current hash of `yaml_path`.

    Returns `Err` if the lock file is missing, malformed, or does not match
    the current content of the rules file (承認なしの変更).
    """
    if not lock_path.exists():
        return Err(RulesError("rules lock が無い(trader rules approve を実行してください)"))

    try:
        lock_text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return Err(RulesError("rules lock を読み込めない"))

    parsed = parse_lock(lock_text)
    if isinstance(parsed, Err):
        return parsed

    current_sha = sha256_of_file(yaml_path)
    if isinstance(current_sha, Err):
        return current_sha

    if current_sha.value != parsed.value.sha256:
        return Err(
            RulesError("rules lock が現在のルールファイルと一致しない(承認されていない変更)")
        )

    return Ok(parsed.value)


def write_lock(lock_path: Path, lock: RulesLock) -> Result[None, RulesError]:
    """Write `lock` to `lock_path`. Only `trader rules approve` should call this."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(render_lock(lock), encoding="utf-8")
    except OSError:
        return Err(RulesError("rules lock を書き込めない"))
    return Ok(None)
