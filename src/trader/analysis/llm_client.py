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
# No `temperature` / `top_p` / `top_k`: current models (Sonnet 5, Opus 5,
# Fable 5) reject sampling parameters with HTTP 400. Output determinism is
# handled by the prompt + schema validation, not by sampling knobs.


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


def _api_error_message(exc: APIStatusError) -> str | None:
    """The API's own `error.message` (e.g. "temperature: Extra inputs are not
    permitted"), or `None`. Only that one field -- never the whole body."""
    body = exc.body
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    return message if isinstance(message, str) and message else None


def _describe_error(exc: Exception | None) -> str:
    """A human-readable failure summary for the ledger (N-6): exception type,
    HTTP status, and the API's message. The API key is never part of any of
    these. Without the status and message, a 400 is undebuggable from the
    ledger alone (every candidate just reads "BadRequestError")."""
    if exc is None:
        return "llm call failed: unknown error"
    summary = f"llm call failed: {type(exc).__name__}"
    if isinstance(exc, APIStatusError):
        summary += f" (HTTP {exc.status_code})"
        api_message = _api_error_message(exc)
        if api_message is not None:
            summary += f": {api_message}"
    return summary


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

    `message` never includes the API key or the raw response body -- at most
    the API's own `error.message` field.
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

        return Err(LlmError(_describe_error(last_error), retryable=last_retryable))

    @staticmethod
    def _extract_text(response: Any) -> str | None:
        """The first `text` content block. Adaptive thinking (on by default
        on Sonnet 5 / Opus 5) puts a `thinking` block first, so `content[0]`
        is not necessarily the answer."""
        content = getattr(response, "content", None)
        if not content:
            return None
        for block in content:
            if getattr(block, "type", None) != "text":
                continue
            text = getattr(block, "text", None)
            if isinstance(text, str):
                return text
        return None
