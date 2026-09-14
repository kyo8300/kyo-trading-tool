"""Trading rule schema, loading, and derived absolute limits (R-9, R-11).

Every model here is `frozen=True, extra="forbid"`: a `RuleSet` cannot be
mutated after construction and an unknown key (e.g. a leftover absolute
`*_usd` field) is rejected rather than silently ignored. Percentages are
stored as `Decimal` and combined with `capital_usd` only at `derive_limits`
time -- the YAML file itself never contains an absolute dollar limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trader.domain.money import Money, pct_of
from trader.domain.result import Err, Ok, Result

# `0 < x <= 100`: the shape shared by most percentage fields in the rules
# (position/loss-limit/exit percentages that must be a strictly positive
# fraction of capital or price).
_Pct = Annotated[Decimal, Field(gt=0, le=100)]


@dataclass(frozen=True, slots=True)
class RulesError:
    """A human-readable rules error.

    `message` names the offending key(s) but never dumps the invalid value
    or a stack trace (N-6).
    """

    message: str


class PositionRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_notional_per_ticker_pct: _Pct
    max_concurrent_positions: int = Field(ge=1)
    max_holding_days: int = Field(ge=1)


class ExitRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_loss_pct: Annotated[Decimal, Field(gt=-100, lt=0)]
    partial_take_profit_pct: _Pct
    partial_take_profit_fraction: Annotated[Decimal, Field(gt=0, lt=1)]
    trailing_stop_pct: _Pct


class LossLimitRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_daily_loss_pct: _Pct
    max_weekly_loss_pct: _Pct
    max_drawdown_pct: _Pct


class EntryRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_price_usd: Annotated[Decimal, Field(gt=0)]
    min_avg_daily_volume: int = Field(ge=0)
    excluded_tickers: tuple[str, ...] = ()


class CostAssumptions(BaseModel):
    """R-23 coefficients. Percentages here are cost estimates, not trading
    limits, so `0` is a legitimate value (e.g. Alpaca's `commission_pct`)
    and the strict `> 0` rule for trading-rule percentages does not apply.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slippage_pct_per_side: Annotated[Decimal, Field(ge=0, le=100)]
    commission_pct: Annotated[Decimal, Field(ge=0, le=100)]
    commission_cap_usd: Annotated[Decimal, Field(gt=0)] | None = None
    commission_usd_per_order: Annotated[Decimal, Field(ge=0)]
    fx_cost_pct_one_way: Annotated[Decimal, Field(ge=0, le=100)]

    @field_validator("commission_cap_usd", mode="before")
    @classmethod
    def _empty_string_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class RuleSet(BaseModel):
    """The whole of `rules/trading-rules.yaml` (R-9).

    `approved_by` / `approved_at` may be empty at load time -- only
    `trader rules approve` requires them to be filled in (R-10).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    approved_by: str
    approved_at: str
    capital_usd: Annotated[Decimal, Field(gt=0)]
    position: PositionRules
    exit: ExitRules
    loss_limits: LossLimitRules
    entry: EntryRules
    cost_assumptions: CostAssumptions


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return "; ".join(parts) if parts else "invalid rules"


def load_rules(path: Path) -> Result[RuleSet, RulesError]:
    """Read, parse, and validate the rules YAML at `path` (R-9, R-11).

    Returns `Err` (never raises) for missing/unreadable files, YAML that is
    not a mapping, and schema violations.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Err(RulesError("could not read rules file"))

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return Err(RulesError("rules file is not valid YAML"))

    if not isinstance(data, dict):
        return Err(RulesError("rules file must contain a mapping of keys to values"))

    try:
        rule_set = RuleSet.model_validate(data)
    except ValidationError as exc:
        return Err(RulesError(f"invalid rules: {_format_validation_error(exc)}"))

    return Ok(rule_set)


@dataclass(frozen=True, slots=True)
class DerivedLimits:
    """Absolute USD/percentage limits derived from `capital_usd * pct` (R-9)."""

    max_notional_per_ticker: Money
    max_daily_loss: Money
    max_weekly_loss: Money
    max_drawdown_pct: Decimal


def derive_limits(rules: RuleSet) -> DerivedLimits:
    """Derive absolute limits from `rules.capital_usd` and the configured
    percentages, quantized to the cent (spec "導出される絶対額")."""
    capital = Money(rules.capital_usd)
    return DerivedLimits(
        max_notional_per_ticker=pct_of(capital, rules.position.max_notional_per_ticker_pct),
        max_daily_loss=pct_of(capital, rules.loss_limits.max_daily_loss_pct),
        max_weekly_loss=pct_of(capital, rules.loss_limits.max_weekly_loss_pct),
        max_drawdown_pct=rules.loss_limits.max_drawdown_pct,
    )
