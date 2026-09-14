"""T-10: retry only transient failures, never retry hard 400s, timeouts and
hashes wired up correctly (AC-24, N-4)."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

import httpx
from anthropic import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import SecretStr

from trader.analysis.analyst import propose
from trader.analysis.llm_client import AnthropicClient
from trader.analysis.prompt import Prompt
from trader.domain.result import Err, Ok

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(status_code: int) -> APIStatusError:
    response = httpx.Response(status_code=status_code, request=_REQUEST, json={"error": {}})
    return APIStatusError(f"status {status_code}", response=response, body={"error": {}})


class _FakeContentBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContentBlock(text)]


class _FlakyMessages:
    """Raises each entry in `results` in order, then returns the last forever."""

    def __init__(self, results: list[Any]) -> None:
        self._results = results
        self.calls = 0

    def create(self, **_kwargs: object) -> Any:
        self.calls += 1
        result = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class _FakeAnthropic:
    def __init__(self, messages: _FlakyMessages, **kwargs: Any) -> None:
        self.messages = messages
        self.init_kwargs = kwargs


def _client(
    messages: _FlakyMessages,
    sleeps: list[Decimal] | None = None,
    captured_kwargs: dict[str, Any] | None = None,
) -> AnthropicClient:
    kwargs_sink: dict[str, Any] = captured_kwargs if captured_kwargs is not None else {}

    def factory(**kwargs: Any) -> _FakeAnthropic:
        kwargs_sink.update(kwargs)
        return _FakeAnthropic(messages, **kwargs)

    return AnthropicClient.create(
        SecretStr("sk-test"),
        "claude-test-model",
        client_factory=factory,
        sleep=(sleeps.append if sleeps is not None else (lambda _delay: None)),
        timeout_s=42,
    )


def _prompt() -> Prompt:
    return Prompt(system="be a helpful analyst", user="here is evidence")


def test_rate_limit_is_retried_and_then_succeeds() -> None:
    messages = _FlakyMessages([_status_error(429), _FakeMessage('{"proposals": []}')])
    sleeps: list[Decimal] = []
    client = _client(messages, sleeps=sleeps)

    result = client.complete(_prompt())

    assert isinstance(result, Ok)
    assert messages.calls == 2
    assert sleeps == [Decimal("1")]


def test_bad_request_is_not_retried() -> None:
    messages = _FlakyMessages([_status_error(400), _FakeMessage('{"proposals": []}')])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Err)
    assert result.error.retryable is False
    assert messages.calls == 1


def test_timeout_is_retried() -> None:
    timeout_error = APITimeoutError(request=_REQUEST)
    messages = _FlakyMessages([timeout_error, _FakeMessage('{"proposals": []}')])
    sleeps: list[Decimal] = []
    client = _client(messages, sleeps=sleeps)

    result = client.complete(_prompt())

    assert isinstance(result, Ok)
    assert messages.calls == 2
    assert sleeps == [Decimal("1")]


def test_connection_error_is_retried() -> None:
    conn_error = APIConnectionError(request=_REQUEST)
    messages = _FlakyMessages([conn_error, _FakeMessage('{"proposals": []}')])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Ok)
    assert messages.calls == 2


def test_repeated_failures_exhaust_retries_and_return_err() -> None:
    messages = _FlakyMessages([_status_error(503), _status_error(503), _status_error(503)])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Err)
    assert result.error.retryable is True
    assert messages.calls == 2  # RetryPolicy(max_attempts=2): at most 2 tries total


def test_timeout_and_max_retries_are_passed_to_client_factory() -> None:
    messages = _FlakyMessages([_FakeMessage('{"proposals": []}')])
    captured_kwargs: dict[str, Any] = {}
    _client(messages, captured_kwargs=captured_kwargs)

    assert captured_kwargs["timeout"] == 42.0
    assert captured_kwargs["max_retries"] == 0


def test_hashes_are_deterministic() -> None:
    messages_a = _FlakyMessages([_FakeMessage('{"proposals": []}')])
    messages_b = _FlakyMessages([_FakeMessage('{"proposals": []}')])
    client_a = _client(messages_a)
    client_b = _client(messages_b)

    result_a = client_a.complete(_prompt())
    result_b = client_b.complete(_prompt())

    assert isinstance(result_a, Ok)
    assert isinstance(result_b, Ok)
    assert result_a.value.prompt_sha256 == result_b.value.prompt_sha256
    assert result_a.value.response_sha256 == result_b.value.response_sha256
    assert len(result_a.value.prompt_sha256) == 64


def test_analyst_propose_skips_with_reason_when_llm_call_fails() -> None:
    messages = _FlakyMessages([_status_error(400)])
    client = _client(messages)

    result = propose(client, _prompt(), ["AAPL"])

    assert result.proposals == ()
    assert result.skipped_reason is not None
    assert "llm call failed" in result.skipped_reason


def test_analyst_propose_skips_with_reason_when_output_is_invalid() -> None:
    messages = _FlakyMessages([_FakeMessage("not json")])
    client = _client(messages)

    result = propose(client, _prompt(), ["AAPL"])

    assert result.proposals == ()
    assert result.skipped_reason is not None
    assert "invalid llm output" in result.skipped_reason
    # hashes are still recorded even though the output was unusable
    assert result.prompt_sha256 != ""


def test_analyst_propose_skips_without_calling_llm_when_no_candidates() -> None:
    messages = _FlakyMessages([_FakeMessage('{"proposals": []}')])
    client = _client(messages)

    result = propose(client, _prompt(), [])

    assert result.proposals == ()
    assert result.skipped_reason == "no candidates"
    assert messages.calls == 0


def test_server_error_500_is_retried() -> None:
    messages = _FlakyMessages([_status_error(500), _FakeMessage('{"proposals": []}')])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Ok)
    assert messages.calls == 2


def test_unauthorized_401_is_not_retried() -> None:
    messages = _FlakyMessages([_status_error(401), _FakeMessage('{"proposals": []}')])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Err)
    assert result.error.retryable is False
    assert messages.calls == 1


def test_not_found_404_is_not_retried() -> None:
    messages = _FlakyMessages([_status_error(404), _FakeMessage('{"proposals": []}')])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Err)
    assert result.error.retryable is False
    assert messages.calls == 1


def test_empty_content_returns_err() -> None:
    class _EmptyMessage:
        def __init__(self) -> None:
            self.content: list[Any] = []

    messages = _FlakyMessages([_EmptyMessage()])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Err)
    assert messages.calls == 1


def test_content_without_text_block_returns_err() -> None:
    class _NoTextBlock:
        pass

    class _MessageWithNoTextBlock:
        def __init__(self) -> None:
            self.content = [_NoTextBlock()]

    messages = _FlakyMessages([_MessageWithNoTextBlock()])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Err)
    assert messages.calls == 1


def test_hash_matches_hashlib_sha256_of_response_text() -> None:
    response_text = '{"proposals": []}'
    messages = _FlakyMessages([_FakeMessage(response_text)])
    client = _client(messages)

    result = client.complete(_prompt())

    assert isinstance(result, Ok)
    expected_prompt_hash = hashlib.sha256(
        (_prompt().system + "\n" + _prompt().user).encode("utf-8")
    ).hexdigest()
    expected_response_hash = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    assert result.value.prompt_sha256 == expected_prompt_hash
    assert result.value.response_sha256 == expected_response_hash


def test_error_message_never_includes_api_key_or_response_body() -> None:
    secret_key = "sk-super-secret-value-12345"  # noqa: S105 (test-only fake credential)
    response_body = "SENSITIVE_RESPONSE_BODY_CONTENT"
    response = httpx.Response(
        status_code=503, request=_REQUEST, json={"error": {"message": response_body}}
    )
    status_error = APIStatusError(response_body, response=response, body={"error": {}})
    messages = _FlakyMessages([status_error, status_error])
    kwargs_sink: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _FakeAnthropic:
        kwargs_sink.update(kwargs)
        return _FakeAnthropic(messages, **kwargs)

    client = AnthropicClient.create(
        SecretStr(secret_key),
        "claude-test-model",
        client_factory=factory,
        sleep=lambda _delay: None,
    )

    result = client.complete(_prompt())

    assert isinstance(result, Err)
    assert secret_key not in result.error.message
    assert response_body not in result.error.message


def test_analyst_propose_returns_proposals_on_success() -> None:
    payload = (
        '{"proposals": [{"ticker": "AAPL", "action": "buy", "confidence": "0.6", '
        '"rationale": "good momentum", "evidence_mention_ids": ["m1"]}]}'
    )
    messages = _FlakyMessages([_FakeMessage(payload)])
    client = _client(messages)

    result = propose(client, _prompt(), ["AAPL"])

    assert result.skipped_reason is None
    assert len(result.proposals) == 1
    assert result.proposals[0].ticker == "AAPL"
    assert result.llm_model == "claude-test-model"
