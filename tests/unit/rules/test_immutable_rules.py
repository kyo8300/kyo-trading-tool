"""AC-10: `RuleSet` and every nested model are frozen."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from trader.domain.result import Ok
from trader.rules import schema as rules_schema
from trader.rules.schema import load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"


def _load_valid() -> object:
    result = load_rules(FIXTURES / "valid.yaml")
    assert isinstance(result, Ok)
    return result.value


def test_rule_set_attribute_assignment_raises() -> None:
    rule_set = _load_valid()
    with pytest.raises(ValidationError):
        rule_set.capital_usd = Decimal("1000")  # type: ignore[misc]


def test_position_rules_attribute_assignment_raises() -> None:
    rule_set = _load_valid()
    with pytest.raises(ValidationError):
        rule_set.position.max_concurrent_positions = 10  # type: ignore[misc]


def test_exit_rules_attribute_assignment_raises() -> None:
    rule_set = _load_valid()
    with pytest.raises(ValidationError):
        rule_set.exit.stop_loss_pct = Decimal("-10")  # type: ignore[misc]


def test_loss_limit_rules_attribute_assignment_raises() -> None:
    rule_set = _load_valid()
    with pytest.raises(ValidationError):
        rule_set.loss_limits.max_daily_loss_pct = Decimal("5")  # type: ignore[misc]


def test_entry_rules_attribute_assignment_raises() -> None:
    rule_set = _load_valid()
    with pytest.raises(ValidationError):
        rule_set.entry.min_price_usd = Decimal("3")  # type: ignore[misc]


def test_cost_assumptions_attribute_assignment_raises() -> None:
    rule_set = _load_valid()
    with pytest.raises(ValidationError):
        rule_set.cost_assumptions.commission_pct = Decimal("1")  # type: ignore[misc]


def test_all_rule_set_models_are_frozen_and_forbid_extra() -> None:
    """Every pydantic model in rules/schema.py must be `frozen=True,
    extra="forbid"` -- not just RuleSet itself. A model that forgot the
    config would silently accept unknown (e.g. absolute `*_usd`) keys or
    allow mutation."""
    models = [
        obj
        for obj in vars(rules_schema).values()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
    ]
    assert models, "expected to find at least one pydantic model in rules/schema.py"

    for model in models:
        config = model.model_config
        assert config.get("frozen") is True, f"{model.__name__} is not frozen"
        assert config.get("extra") == "forbid", f"{model.__name__} does not forbid extra keys"


def test_model_copy_returns_new_object_and_leaves_original_unchanged() -> None:
    rule_set = _load_valid()
    updated = rule_set.model_copy(update={"capital_usd": Decimal("1000")})

    assert updated is not rule_set
    assert updated.capital_usd == Decimal("1000")
    assert rule_set.capital_usd == Decimal("500")
