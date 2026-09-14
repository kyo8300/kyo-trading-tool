"""T-10: rules are embedded as read-only text; the prompt never mentions the
rules file, and evidence/market data both show up (Q4, R-7, R-11, AC-10)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trader.analysis.prompt import build_prompt, rules_summary
from trader.domain.models import Evidence, MentionStats
from trader.domain.money import Price
from trader.domain.result import Ok
from trader.market.data_provider import Bar
from trader.rules.schema import derive_limits, load_rules

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rules"


def _rule_set_and_limits():
    result = load_rules(FIXTURES / "valid.yaml")
    assert isinstance(result, Ok)
    rule_set = result.value
    return rule_set, derive_limits(rule_set)


def _evidence(ticker: str, last14: int, prior14: int) -> Evidence:
    stats = MentionStats(
        ticker=ticker,
        first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
        mention_count=last14 + prior14,
        mention_count_last_14d=last14,
        mention_count_prior_14d=prior14,
    )
    return Evidence(
        ticker=ticker,
        stats=stats,
        excerpts=(f"{ticker} is mooning", f"{ticker} to the moon"),
        mention_ids=("m1", "m2"),
    )


def _bars(ticker: str) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            ticker=ticker,
            date=date(2026, 9, day),
            open=Price(Decimal("10.00")),
            high=Price(Decimal("10.50")),
            low=Price(Decimal("9.50")),
            close=Price(Decimal("10.00")),
            volume=200_000,
        )
        for day in range(1, 6)
    )


def test_rules_summary_is_read_only_text_without_rules_file_name() -> None:
    rule_set, limits = _rule_set_and_limits()

    summary = rules_summary(rule_set, limits)

    assert "500" in summary
    assert "75.00" in summary
    assert "trading-rules" not in summary
    assert ".yaml" not in summary


def test_build_prompt_includes_rules_evidence_and_bars() -> None:
    rule_set, limits = _rule_set_and_limits()
    summary = rules_summary(rule_set, limits)
    evidences = (_evidence("AAPL", last14=5, prior14=0), _evidence("TSLA", last14=2, prior14=3))
    bars_by_ticker = {"AAPL": _bars("AAPL"), "TSLA": _bars("TSLA")}

    prompt = build_prompt(summary, evidences, bars_by_ticker, ["AAPL", "TSLA"])

    assert summary in prompt.user
    assert "AAPL" in prompt.user
    assert "NEW" in prompt.user  # AAPL has prior14d=0, last14d=5
    assert "mooning" in prompt.user
    assert "2026-09-01" in prompt.user
    assert "avg daily volume" in prompt.user
    assert "200000" in prompt.user
    assert "cannot" in prompt.system.lower() or "read-only" in prompt.system.lower()
    assert "sell" in prompt.system.lower()
    assert "JSON" in prompt.system


def test_prompt_never_contains_trading_rules_string() -> None:
    rule_set, limits = _rule_set_and_limits()
    summary = rules_summary(rule_set, limits)
    prompt = build_prompt(summary, (), {}, ["AAPL"])

    assert "trading-rules" not in prompt.system
    assert "trading-rules" not in prompt.user
    assert "trading-rules" not in summary


def test_rules_summary_includes_pct_and_stop_loss_and_excluded_tickers() -> None:
    rule_set, limits = _rule_set_and_limits()

    summary = rules_summary(rule_set, limits)

    assert "15%" in summary
    assert "-15" in summary  # stop_loss_pct
    assert "Excluded tickers" in summary


def test_build_prompt_includes_20_daily_bars_and_average_volume() -> None:
    rule_set, limits = _rule_set_and_limits()
    summary = rules_summary(rule_set, limits)
    bars = tuple(
        Bar(
            ticker="AAPL",
            date=date(2026, 9, day),
            open=Price(Decimal("10.00")),
            high=Price(Decimal("10.50")),
            low=Price(Decimal("9.50")),
            close=Price(Decimal("10.00")),
            volume=200_000,
        )
        for day in range(1, 26)
    )

    prompt = build_prompt(summary, (), {"AAPL": bars}, ["AAPL"])

    assert "most recent 20" in prompt.user
    assert "avg daily volume: 200000" in prompt.user


def test_build_prompt_with_empty_candidates_does_not_raise() -> None:
    rule_set, limits = _rule_set_and_limits()
    summary = rules_summary(rule_set, limits)

    prompt = build_prompt(summary, (), {}, [])

    assert "(none)" in prompt.user


def test_build_prompt_is_deterministic_for_the_same_input() -> None:
    rule_set, limits = _rule_set_and_limits()
    summary = rules_summary(rule_set, limits)
    evidences = (_evidence("AAPL", last14=5, prior14=0),)
    bars_by_ticker = {"AAPL": _bars("AAPL")}

    prompt_a = build_prompt(summary, evidences, bars_by_ticker, ["AAPL"])
    prompt_b = build_prompt(summary, evidences, bars_by_ticker, ["AAPL"])

    assert prompt_a == prompt_b
