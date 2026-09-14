"""T-10: LLM output is validated by pydantic; invalid/unadopted shapes never
become decisions (AC-7, R-5, R-7)."""

from __future__ import annotations

import json

from trader.analysis.output_schema import parse_llm_response
from trader.domain.result import Err, Ok

CANDIDATES = ["AAPL", "TSLA"]


def _valid_payload() -> dict[str, object]:
    return {
        "proposals": [
            {
                "ticker": "AAPL",
                "action": "buy",
                "confidence": "0.7",
                "rationale": "Strong recent mentions and rising volume.",
                "evidence_mention_ids": ["m1", "m2"],
            }
        ]
    }


def test_valid_json_is_parsed() -> None:
    result = parse_llm_response(json.dumps(_valid_payload()), CANDIDATES)

    assert isinstance(result, Ok)
    assert len(result.value.proposals) == 1
    proposal = result.value.proposals[0]
    assert proposal.ticker == "AAPL"
    assert proposal.action == "buy"


def test_fenced_json_is_parsed() -> None:
    fenced = "```json\n" + json.dumps(_valid_payload()) + "\n```"

    result = parse_llm_response(fenced, CANDIDATES)

    assert isinstance(result, Ok)
    assert len(result.value.proposals) == 1


def test_plain_fence_without_json_language_tag_is_parsed() -> None:
    fenced = "```\n" + json.dumps(_valid_payload()) + "\n```"

    result = parse_llm_response(fenced, CANDIDATES)

    assert isinstance(result, Ok)


def test_not_json_is_rejected() -> None:
    result = parse_llm_response("not json at all", CANDIDATES)

    assert isinstance(result, Err)


def test_confidence_out_of_range_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["confidence"] = "1.5"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_unknown_action_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["action"] = "strong_buy"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_sell_action_is_not_adopted() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["action"] = "sell"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_unknown_field_resembling_a_rule_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["max_notional_per_ticker_pct"] = "50"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_ticker_outside_candidates_is_not_adopted() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["ticker"] = "UNKNOWN"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_missing_rationale_is_rejected() -> None:
    payload = _valid_payload()
    del payload["proposals"][0]["rationale"]  # type: ignore[call-overload]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_empty_proposals_list_is_valid() -> None:
    result = parse_llm_response(json.dumps({"proposals": []}), CANDIDATES)

    assert isinstance(result, Ok)
    assert result.value.proposals == ()
