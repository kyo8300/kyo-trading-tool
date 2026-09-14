"""AC-25: rules YAML is validated at the boundary with human-readable errors
that name the offending key but never dump values or a stack trace (N-5, N-6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trader.domain.result import Err
from trader.rules.schema import load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"


def _assert_clean_error_message(message: str) -> None:
    assert "Traceback" not in message
    assert "errors.pydantic.dev" not in message


def test_extra_absolute_usd_key_is_rejected() -> None:
    result = load_rules(FIXTURES / "invalid_usd_key.yaml")
    assert isinstance(result, Err)
    assert "max_notional_per_ticker_usd" in result.error.message
    _assert_clean_error_message(result.error.message)


def test_extra_loss_limit_usd_key_is_rejected(tmp_path: Path) -> None:
    text = (FIXTURES / "valid.yaml").read_text(encoding="utf-8")
    text = text.replace(
        'max_daily_loss_pct: "3"',
        'max_daily_loss_pct: "3"\n  max_daily_loss_usd: "15.00"',
    )
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(text, encoding="utf-8")

    result = load_rules(rules_path)

    assert isinstance(result, Err)
    assert "max_daily_loss_usd" in result.error.message
    _assert_clean_error_message(result.error.message)


def test_pct_out_of_range_is_rejected() -> None:
    result = load_rules(FIXTURES / "invalid_pct_range.yaml")
    assert isinstance(result, Err)
    assert "max_notional_per_ticker_pct" in result.error.message
    _assert_clean_error_message(result.error.message)


def test_positive_stop_loss_pct_is_rejected(tmp_path: Path) -> None:
    text = (FIXTURES / "valid.yaml").read_text(encoding="utf-8")
    text = text.replace('stop_loss_pct: "-15"', 'stop_loss_pct: "15"')
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(text, encoding="utf-8")

    result = load_rules(rules_path)

    assert isinstance(result, Err)
    assert "stop_loss_pct" in result.error.message
    _assert_clean_error_message(result.error.message)


def test_capital_zero_is_rejected() -> None:
    result = load_rules(FIXTURES / "invalid_capital_zero.yaml")
    assert isinstance(result, Err)
    assert "capital_usd" in result.error.message
    _assert_clean_error_message(result.error.message)


def test_capital_negative_is_rejected() -> None:
    result = load_rules(FIXTURES / "invalid_capital_negative.yaml")
    assert isinstance(result, Err)
    assert "capital_usd" in result.error.message
    _assert_clean_error_message(result.error.message)


def test_yaml_list_instead_of_mapping_is_rejected(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("- 1\n- 2\n", encoding="utf-8")

    result = load_rules(rules_path)

    assert isinstance(result, Err)
    _assert_clean_error_message(result.error.message)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "invalid_usd_key.yaml",
        "invalid_pct_range.yaml",
        "invalid_capital_zero.yaml",
        "invalid_capital_negative.yaml",
    ],
)
def test_error_message_is_human_readable(fixture_name: str) -> None:
    result = load_rules(FIXTURES / fixture_name)
    assert isinstance(result, Err)
    assert result.error.message
    _assert_clean_error_message(result.error.message)
