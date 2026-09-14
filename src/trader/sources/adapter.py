"""`SourceAdapter` protocol and `AdapterRegistry` (R-3).

An adapter turns whatever a `data_dir` holds for one information source into
validated `Mention` records. The registry maps `source_id` -> adapter and is
itself immutable: `register` returns a new registry rather than mutating the
existing one (N-7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from trader.domain.models import Mention
from trader.domain.result import Err, Ok, Result


@dataclass(frozen=True, slots=True)
class IngestError:
    """A human-readable ingest failure (N-6).

    Never includes filesystem paths or raw record data -- only a summary
    message, the count of invalid records, and a handful of short
    `"index N: <field>: <msg>"` strings identifying what was wrong.
    """

    message: str
    invalid_count: int
    first_errors: tuple[str, ...]


@runtime_checkable
class SourceAdapter(Protocol):
    """Something that can load one information source's data as `Mention`s."""

    @property
    def source_id(self) -> str: ...

    def load(self, data_dir: Path) -> Result[tuple[Mention, ...], IngestError]: ...


@dataclass(frozen=True, slots=True)
class AdapterRegistry:
    """An immutable `source_id -> SourceAdapter` lookup table."""

    adapters: Mapping[str, SourceAdapter]

    def register(self, adapter: SourceAdapter) -> AdapterRegistry:
        """Return a new registry with `adapter` added (or replacing an
        existing adapter with the same `source_id`)."""
        return AdapterRegistry({**self.adapters, adapter.source_id: adapter})

    def get(self, source_id: str) -> Result[SourceAdapter, IngestError]:
        """Look up the adapter registered for `source_id`."""
        adapter = self.adapters.get(source_id)
        if adapter is None:
            return Err(
                IngestError(
                    message=f"unknown source '{source_id}'",
                    invalid_count=0,
                    first_errors=(),
                )
            )
        return Ok(adapter)


def default_registry() -> AdapterRegistry:
    """The registry shipped by default: `serenity` only (R-3)."""
    from trader.sources.serenity.adapter import SerenityAdapter

    return AdapterRegistry(adapters={}).register(SerenityAdapter())
