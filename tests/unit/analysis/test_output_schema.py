"""T-10: LLM output is validated by pydantic; invalid/unadopted shapes never
become decisions (AC-7, R-5, R-7)."""

from __future__ import annotations

import json
from decimal import Decimal

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


def test_confidence_exactly_zero_and_one_are_accepted() -> None:
    low = _valid_payload()
    low["proposals"][0]["confidence"] = "0"  # type: ignore[index]
    high = _valid_payload()
    high["proposals"][0]["confidence"] = "1"  # type: ignore[index]

    result_low = parse_llm_response(json.dumps(low), CANDIDATES)
    result_high = parse_llm_response(json.dumps(high), CANDIDATES)

    assert isinstance(result_low, Ok)
    assert result_low.value.proposals[0].confidence == Decimal("0")
    assert isinstance(result_high, Ok)
    assert result_high.value.proposals[0].confidence == Decimal("1")


def test_confidence_just_below_zero_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["confidence"] = "-0.01"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_confidence_just_above_one_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["confidence"] = "1.01"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_confidence_string_is_converted_to_decimal() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["confidence"] = "0.5"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Ok)
    proposal = result.value.proposals[0]
    assert proposal.confidence == Decimal("0.5")
    assert isinstance(proposal.confidence, Decimal)


def test_empty_rationale_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["rationale"] = ""  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_non_string_evidence_mention_ids_are_rejected() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["evidence_mention_ids"] = [1, 2]  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_unknown_top_level_field_is_rejected() -> None:
    for key, value in (
        ("rules", {}),
        ("max_notional_per_ticker_pct", "15"),
        ("capital_usd", "500"),
    ):
        payload = _valid_payload()
        payload[key] = value

        result = parse_llm_response(json.dumps(payload), CANDIDATES)

        assert isinstance(result, Err), f"expected Err for top-level key {key!r}"


def test_quantity_specifying_keys_in_proposal_are_rejected() -> None:
    for key, value in (("qty", 10), ("notional", "75.00"), ("shares", 5)):
        payload = _valid_payload()
        payload["proposals"][0][key] = value  # type: ignore[index]

        result = parse_llm_response(json.dumps(payload), CANDIDATES)

        assert isinstance(result, Err), f"expected Err for proposal key {key!r}"


def test_non_json_body_error_message_excludes_the_body() -> None:
    body = "definitely not json: secret-looking-content-xyz"

    result = parse_llm_response(body, CANDIDATES)

    assert isinstance(result, Err)
    assert body not in result.error.message
    assert "secret-looking-content-xyz" not in result.error.message


def test_one_candidate_outside_list_rejects_the_whole_response() -> None:
    payload = _valid_payload()
    payload["proposals"].append(  # type: ignore[attr-defined]
        {
            "ticker": "UNKNOWN",
            "action": "hold",
            "confidence": "0.2",
            "rationale": "not in candidates",
            "evidence_mention_ids": [],
        }
    )

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)


def test_lowercase_ticker_does_not_match_uppercase_candidate() -> None:
    payload = _valid_payload()
    payload["proposals"][0]["ticker"] = "aapl"  # type: ignore[index]

    result = parse_llm_response(json.dumps(payload), CANDIDATES)

    assert isinstance(result, Err)
