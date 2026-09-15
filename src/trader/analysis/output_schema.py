"""Structured LLM output schema and validation (R-5, R-7).

`LlmProposal` / `LlmResponse` are `frozen=True, extra="forbid"` pydantic
models: an unknown field (e.g. a leftover rule-changing key) is rejected
rather than silently ignored, and neither model has any field that could
change a trading rule. `action` never accepts `"sell"` -- selling is decided
by the rule engine alone (R-13), never by the LLM.

`parse_llm_response` is the only entry point that turns raw LLM text into a
validated `LlmResponse`; any proposal naming a ticker outside the supplied
candidate list makes the whole response unusable (not adopted).
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trader.domain.result import Err, Ok, Result


@dataclass(frozen=True, slots=True)
class AnalysisError:
    """A human-readable analysis error.

    `message` never includes the raw LLM response body (N-6).
    """

    message: str


class LlmProposal(BaseModel):
    """One ticker-level proposal from the LLM (R-5).

    `action` intentionally excludes `"sell"`: rule-driven exits (R-13) are
    the only sell path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    action: Literal["buy", "hold", "skip"]
    confidence: Decimal = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_mention_ids: tuple[str, ...] = ()


class LlmResponse(BaseModel):
    """The whole structured response for one analysis call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposals: tuple[LlmProposal, ...] = ()


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding ```json ... ``` (or plain ``` ... ```) fence, if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    if lines[-1].strip() != "```":
        return stripped
    body_lines = lines[1:-1]
    return "\n".join(body_lines).strip()


def _validation_summary(exc: ValidationError) -> str:
    """A human-readable, value-free summary of a pydantic `ValidationError` (N-6)."""
    fields = sorted({str(error["loc"][-1]) for error in exc.errors() if error["loc"]})
    if not fields:
        return "invalid response"
    return "invalid fields: " + ", ".join(fields)


def parse_llm_response(
    text: str, candidates: Collection[str]
) -> Result[LlmResponse, AnalysisError]:
    """Parse, validate, and candidate-check a raw LLM response (R-5, R-7).

    Returns `Err` (never raises) for invalid JSON, schema violations, or any
    proposal naming a ticker outside `candidates`.
    """
    unfenced = _strip_code_fence(text)
    try:
        payload = json.loads(unfenced)
    except json.JSONDecodeError:
        return Err(AnalysisError("llm response is not valid JSON"))

    try:
        response = LlmResponse.model_validate(payload)
    except ValidationError as exc:
        return Err(AnalysisError(f"invalid llm response: {_validation_summary(exc)}"))

    candidate_set = set(candidates)
    for proposal in response.proposals:
        if proposal.ticker not in candidate_set:
            return Err(AnalysisError("llm response proposes a ticker outside the candidate list"))

    return Ok(response)
