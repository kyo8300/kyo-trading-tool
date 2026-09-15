"""AC-5: information sources are added via the `SourceAdapter` protocol --
a dummy adapter (not `serenity`) can be registered and used to ingest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trader.domain.models import Mention
from trader.domain.result import Err, Ok, Result
from trader.sources.adapter import AdapterRegistry, IngestError, default_registry


@dataclass(frozen=True, slots=True)
class DummyAdapter:
    """A minimal `SourceAdapter` used only to exercise the registry."""

    @property
    def source_id(self) -> str:
        return "dummy"

    def load(self, data_dir: Path) -> Result[tuple[Mention, ...], IngestError]:
        return Ok(
            (
                Mention(
                    id="dummy:1:ABCD",
                    source_id="dummy",
                    external_id="1",
                    ticker="ABCD",
                    posted_at=datetime(2026, 1, 1, tzinfo=UTC),
                    text_excerpt="dummy mention",
                    url=None,
                    raw_sha256="deadbeef",
                ),
            )
        )


def test_default_registry_only_has_serenity() -> None:
    registry = default_registry()
    result = registry.get("serenity")
    assert isinstance(result, Ok)
    assert result.value.source_id == "serenity"


def test_unknown_source_is_err() -> None:
    registry = default_registry()
    result = registry.get("nope")
    assert isinstance(result, Err)


def test_register_returns_new_registry_and_does_not_mutate_original() -> None:
    original = AdapterRegistry(adapters={})
    updated = original.register(DummyAdapter())

    assert isinstance(original.get("dummy"), Err)
    result = updated.get("dummy")
    assert isinstance(result, Ok)
    assert result.value.source_id == "dummy"


def test_dummy_adapter_can_be_used_to_load_mentions(tmp_path: Path) -> None:
    registry = AdapterRegistry(adapters={}).register(DummyAdapter())
    adapter_result = registry.get("dummy")
    assert isinstance(adapter_result, Ok)

    load_result = adapter_result.value.load(tmp_path)
    assert isinstance(load_result, Ok)
    assert load_result.value[0].ticker == "ABCD"


def test_ingest_cli_can_use_a_dummy_adapter(tmp_path: Path) -> None:
    from trader.cli import run_ingest

    db_path = tmp_path / "trader.sqlite3"
    registry = AdapterRegistry(adapters={}).register(DummyAdapter())

    result = run_ingest(
        registry=registry,
        source_id="dummy",
        data_dir=tmp_path / "does-not-need-to-exist",
        db_path=db_path,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert isinstance(result, Ok)
    assert "1" in result.value
