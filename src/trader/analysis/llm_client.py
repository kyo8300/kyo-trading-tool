"""`AnthropicClient`: calls the Anthropic Messages API for LLM analysis (R-5, N-4).

This module is a boundary (N-5): the Anthropic SDK client is
constructor-injected (`client_factory`) so tests can supply a fake instead of
talking to a real Anthropic account (N-9). It has no file I/O of its own
(R-11) -- it only sends a `Prompt` and returns the raw response text plus
SHA-256 hashes of both, for the ledger to record (R-20).

The SDK's own retry machinery is disabled (`max_retries=0`) and a small,
explicit retry loop is used instead, following `RetryPolicy(2, 1, 8)`. Only
transient failures are retried: HTTP 429, HTTP 5xx, timeouts, and connection
errors. Any other failure (e.g. HTTP 400 for a malformed request) is
returned as `Err` on the first attempt.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from anthropic import Anthropic, APIConnectionError, APIStatusError, APITimeoutError
from pydantic import SecretStr

from trader.analysis.prompt import Prompt
from trader.domain.result import Err, Ok, Result
from trader.domain.retry import RetryPolicy, backoff_delays

_RETRY_POLICY = RetryPolicy(max_attempts=2, base_delay_s=Decimal("1"), max_delay_s=Decimal("8"))
_MAX_TOKENS = 2048
_TEMPERATURE = 0.2


def _default_sleep(delay_s: Decimal) -> None:
    time.sleep(float(delay_s))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_retryable(exc: Exception) -> bool:
    """Whether `exc` is a transient failure worth retrying (N-4).

    HTTP 429 / 5xx, timeouts, and connection errors are retried. Anything
    else (e.g. 400 for a malformed request) is not.
    """
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, (APITimeoutError, APIConnectionError))


@dataclass(frozen=True, slots=True)
class LlmRaw:
    """A successful raw LLM call result, ready for the ledger to record."""

    text: str
    model: str
    prompt_sha256: str
    response_sha256: str


@dataclass(frozen=True, slots=True)
class LlmError:
    """A human-readable LLM call error (N-6).

    `message` never includes the API key or response body.
    """

    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class AnthropicClient:
    """`complete(prompt)` wraps `Anthropic.messages.create` with retry (R-5)."""

    model: str
    _client: Any
    _sleep: Callable[[Decimal], None]

    @classmethod
    def create(
        cls,
        api_key: SecretStr,
        model: str,
        *,
        client_factory: Callable[..., Any] = Anthropic,
        sleep: Callable[[Decimal], None] = _default_sleep,
        timeout_s: int = 60,
    ) -> AnthropicClient:
        """Build an `AnthropicClient`.

        The SDK's internal retries are disabled (`max_retries=0`); this
        module performs its own retry loop instead so only idempotent,
        transient failures are retried (N-4).
        """
        client = client_factory(
            api_key=api_key.get_secret_value(),
            timeout=float(timeout_s),
            max_retries=0,
        )
        return cls(model=model, _client=client, _sleep=sleep)

    def complete(self, prompt: Prompt) -> Result[LlmRaw, LlmError]:
        """Call the Anthropic Messages API once, retrying only transient failures.

        `_RETRY_POLICY.max_attempts` total tries are made; the delay before
        each retry follows `backoff_delays`. Any non-retryable failure (or
        exhausting all attempts) returns `Err`.
        """
        prompt_text = prompt.system + "\n" + prompt.user
        prompt_hash = _sha256(prompt_text)
        delays = backoff_delays(_RETRY_POLICY)

        last_error: Exception | None = None
        last_retryable = False
        for attempt in range(_RETRY_POLICY.max_attempts):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=_MAX_TOKENS,
                    temperature=_TEMPERATURE,
                    system=prompt.system,
                    messages=[{"role": "user", "content": prompt.user}],
                )
            except Exception as exc:  # boundary: convert to Err (N-6)
                last_error = exc
                last_retryable = _is_retryable(exc)
                if last_retryable and attempt < len(delays):
                    self._sleep(delays[attempt])
                    continue
                break
            else:
                text = self._extract_text(response)
                if text is None:
                    return Err(LlmError("llm response had no text content", retryable=False))
                return Ok(
                    LlmRaw(
                        text=text,
                        model=self.model,
                        prompt_sha256=prompt_hash,
                        response_sha256=_sha256(text),
                    )
                )

        error_type = type(last_error).__name__ if last_error is not None else "unknown error"
        return Err(LlmError(f"llm call failed: {error_type}", retryable=last_retryable))

    @staticmethod
    def _extract_text(response: Any) -> str | None:
        content = getattr(response, "content", None)
        if not content:
            return None
        text = getattr(content[0], "text", None)
        if not isinstance(text, str):
            return None
        return text
