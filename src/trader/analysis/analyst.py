"""`propose`: evidence + prices -> validated LLM proposals for one cycle (R-5).

Exactly one `complete` call is made per invocation (spec risk 6: one call per
cycle, covering every candidate at once). Any failure -- the LLM call itself,
or output that fails schema/candidate validation -- results in an empty
proposal tuple with `skipped_reason` set, never an exception.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from trader.analysis.llm_client import LlmError, LlmRaw
from trader.analysis.output_schema import LlmProposal, parse_llm_response
from trader.analysis.prompt import Prompt
from trader.domain.result import Err, Result


class LlmClient(Protocol):
    """The subset of `AnthropicClient` that `propose` depends on."""

    model: str

    def complete(self, prompt: Prompt) -> Result[LlmRaw, LlmError]: ...


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """The outcome of one `propose` call, ready for the ledger to record."""

    proposals: tuple[LlmProposal, ...]
    llm_model: str
    prompt_sha256: str
    response_sha256: str
    skipped_reason: str | None


def propose(client: LlmClient, prompt: Prompt, candidates: Sequence[str]) -> AnalysisResult:
    """Call the LLM once and return validated proposals, or a skip reason.

    Empty `candidates` skips the call entirely (nothing to propose about).
    """
    if not candidates:
        return AnalysisResult(
            proposals=(),
            llm_model=client.model,
            prompt_sha256="",
            response_sha256="",
            skipped_reason="no candidates",
        )

    call_result = client.complete(prompt)
    if isinstance(call_result, Err):
        return AnalysisResult(
            proposals=(),
            llm_model=client.model,
            prompt_sha256="",
            response_sha256="",
            skipped_reason=f"llm call failed: {call_result.error.message}",
        )
    raw = call_result.value

    parsed = parse_llm_response(raw.text, candidates)
    if isinstance(parsed, Err):
        return AnalysisResult(
            proposals=(),
            llm_model=raw.model,
            prompt_sha256=raw.prompt_sha256,
            response_sha256=raw.response_sha256,
            skipped_reason=f"invalid llm output: {parsed.error.message}",
        )

    return AnalysisResult(
        proposals=parsed.value.proposals,
        llm_model=raw.model,
        prompt_sha256=raw.prompt_sha256,
        response_sha256=raw.response_sha256,
        skipped_reason=None,
    )
