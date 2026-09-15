"""T-9: `AlpacaBroker` URL selection is fixed by mode, not caller-supplied (AC-14, N-3)."""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import SecretStr

from trader.broker.alpaca_broker import AlpacaBroker
from trader.config.mode import TradingMode
from trader.domain.result import Err, Ok


class _RecordingClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _factory(recorded: list[dict[str, Any]]) -> Any:
    def factory(**kwargs: Any) -> _RecordingClient:
        recorded.append(kwargs)
        return _RecordingClient(**kwargs)

    return factory


def test_paper_mode_gets_the_paper_url() -> None:
    recorded: list[dict[str, Any]] = []
    result = AlpacaBroker.create(
        TradingMode.paper,
        SecretStr("trade-key"),
        SecretStr("trade-secret"),
        client_factory=_factory(recorded),
    )
    assert isinstance(result, Ok)
    assert recorded[0]["url_override"] == "https://paper-api.alpaca.markets"
    assert recorded[0]["paper"] is True


def test_live_mode_gets_the_live_url() -> None:
    recorded: list[dict[str, Any]] = []
    result = AlpacaBroker.create(
        TradingMode.live,
        SecretStr("trade-key"),
        SecretStr("trade-secret"),
        client_factory=_factory(recorded),
    )
    assert isinstance(result, Ok)
    assert recorded[0]["url_override"] == "https://api.alpaca.markets"
    assert recorded[0]["paper"] is False


def test_create_signature_has_no_url_parameter() -> None:
    params = inspect.signature(AlpacaBroker.create).parameters
    assert not any("url" in name.lower() for name in params)


def test_init_signature_has_no_url_parameter() -> None:
    params = inspect.signature(AlpacaBroker.__init__).parameters
    assert not any("url" in name.lower() for name in params)


def test_empty_trade_key_is_rejected() -> None:
    result = AlpacaBroker.create(
        TradingMode.paper,
        SecretStr(""),
        SecretStr("trade-secret"),
        client_factory=_factory([]),
    )
    assert isinstance(result, Err)


def test_empty_trade_secret_is_rejected() -> None:
    result = AlpacaBroker.create(
        TradingMode.paper,
        SecretStr("trade-key"),
        SecretStr(""),
        client_factory=_factory([]),
    )
    assert isinstance(result, Err)
