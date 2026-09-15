"""Shared pytest configuration.

Forces TRADER_ENV=test before any application code is imported, and blocks
real network access for the whole test session (spec N-9).
"""

from __future__ import annotations

import os

os.environ["TRADER_ENV"] = "test"

import socket
from collections.abc import Iterator

import pytest


def _blocked_connect(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("network disabled in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    yield
