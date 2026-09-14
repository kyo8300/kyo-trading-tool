"""Aggregated data ingestion: adapters, evidence derivation (R-1〜R-4)."""

from __future__ import annotations

from trader.sources.adapter import AdapterRegistry, IngestError, SourceAdapter, default_registry
from trader.sources.evidence import build_evidence

__all__ = [
    "AdapterRegistry",
    "IngestError",
    "SourceAdapter",
    "build_evidence",
    "default_registry",
]
