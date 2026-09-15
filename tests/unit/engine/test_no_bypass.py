"""T-12 / AC-8: the rule engine cannot be bypassed on the way to a broker.

1. `Broker.submit_market_order(...)` is called nowhere in `src/trader`
   except `engine/order_executor.py` (static AST scan).
2. `resolve_approval` refuses a `rule_check=rejected` decision.
3. `ApprovedOrderRequest(...)` cannot be constructed directly.
4. `order_executor.execute`'s first parameter is annotated `ApprovedOrderRequest`.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import trader
from trader.config.mode import TradingMode
from trader.domain.clock import FixedClock
from trader.domain.models import Action, Approval, Approver, Decision, Origin, RuleCheck, Side
from trader.domain.money import Money, Price, Quantity
from trader.domain.result import Err
from trader.engine import order_executor
from trader.engine.approval import ApprovedOrderRequest, _make, resolve_approval
from trader.ledger.db import open_db
from trader.market.data_provider import MarketClock

_SRC_ROOT = Path(trader.__file__).resolve().parent
_ALLOWED_CALLER = _SRC_ROOT / "engine" / "order_executor.py"
_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_MARKET_CLOCK = MarketClock(is_open=True, next_open=_NOW, next_close=_NOW)


def _files_calling_submit_market_order() -> list[Path]:
    offenders: list[Path] = []
    for path in _SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit_market_order"
            ):
                offenders.append(path)
                break
    return offenders


def test_submit_market_order_is_only_called_from_order_executor() -> None:
    offenders = _files_calling_submit_market_order()
    assert offenders == [_ALLOWED_CALLER]


def test_rejected_decision_cannot_produce_an_approved_order_request(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value
    decision = Decision(
        id="dec-1",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=(),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.rejected,
        rule_check_reason="notional 80.00 > limit 75.00 (15% of 500)",
        proposed_notional=Money(Decimal("80.00")),
        reference_price=Price(Decimal("10.0000")),
    )

    result = resolve_approval(decision, TradingMode.paper, conn, FixedClock(_NOW), _MARKET_CLOCK)

    assert isinstance(result, Err)
    conn.close()


def test_approved_order_request_cannot_be_constructed_directly() -> None:
    decision = Decision(
        id="dec-1",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=(),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="notional 75.00 <= limit 75.00 (15% of 500)",
        proposed_notional=Money(Decimal("75.00")),
        reference_price=Price(Decimal("10.0000")),
    )
    approval = Approval(
        id="appr-1",
        decision_id=decision.id,
        approver=Approver.system,
        approved_at=_NOW,
        expires_at=_NOW,
    )

    with pytest.raises(TypeError):
        ApprovedOrderRequest(
            decision=decision,
            approval=approval,
            side=Side.buy,
            qty=Quantity(1),
            client_order_id="order_dec-1",
            _token=object(),
        )


def test_execute_requires_an_approved_order_request_type() -> None:
    signature = inspect.signature(order_executor.execute)
    first_param = next(iter(signature.parameters.values()))
    assert first_param.annotation == "ApprovedOrderRequest"


def _hold_or_skip_decision(action: Action) -> Decision:
    return Decision(
        id="dec-hold-skip",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=action,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="n/a",
        evidence_mention_ids=(),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="n/a",
        proposed_notional=None,
        reference_price=None,
    )


def test_hold_action_cannot_produce_an_approved_order_request(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value
    decision = _hold_or_skip_decision(Action.hold)
    result = resolve_approval(decision, TradingMode.paper, conn, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Err)
    conn.close()


def test_skip_action_cannot_produce_an_approved_order_request(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "trader.sqlite3").value
    decision = _hold_or_skip_decision(Action.skip)
    result = resolve_approval(decision, TradingMode.paper, conn, FixedClock(_NOW), _MARKET_CLOCK)
    assert isinstance(result, Err)
    conn.close()


def test_dataclasses_replace_on_an_approved_order_request_reruns_validation() -> None:
    """`dataclasses.replace` re-invokes `__post_init__`, which re-checks the
    private `_token` sentinel. Since `replace` copies unspecified fields
    (including `_token`) from the original instance, a legitimately-created
    `ApprovedOrderRequest` stays valid after `replace` -- the validation
    genuinely runs (it is not skipped), and it passes only because the
    token is still the one only this module can mint. External code still
    has no way to obtain `_TOKEN` to fabricate a *new* one from scratch
    (see `test_approved_order_request_cannot_be_constructed_directly`)."""
    decision = Decision(
        id="dec-replace",
        cycle_id="cycle-1",
        decided_at=_NOW,
        mode="paper",
        ticker="ABCD",
        action=Action.buy,
        origin=Origin.llm,
        confidence=Decimal("0.8"),
        rationale="rising mentions",
        evidence_mention_ids=(),
        llm_model="claude-sonnet-5",
        prompt_sha256="p" * 64,
        response_sha256="r" * 64,
        rule_set_sha256="ruleset-sha",
        rule_check=RuleCheck.passed,
        rule_check_reason="notional 75.00 <= limit 75.00 (15% of 500)",
        proposed_notional=Money(Decimal("75.00")),
        reference_price=Price(Decimal("10.0000")),
    )
    approval = Approval(
        id="appr-1",
        decision_id=decision.id,
        approver=Approver.system,
        approved_at=_NOW,
        expires_at=_NOW,
    )
    original = _make(decision, approval, Side.buy, Quantity(7))

    replaced = replace(original, qty=Quantity(3))

    assert replaced._token is original._token
    assert replaced.qty.shares == 3


def test_ast_scan_detects_a_planted_submit_market_order_call(tmp_path: Path) -> None:
    """Self-test of `_files_calling_submit_market_order`'s detection logic:
    plant a fake `.submit_market_order(...)` call in a throwaway module and
    confirm the scanner flags it, so a false-negative scanner (e.g. one that
    never actually matches anything) cannot silently pass the real test
    above."""
    planted = tmp_path / "planted_bypass.py"
    planted.write_text(
        "def sneaky(broker, req):\n    return broker.submit_market_order(req)\n",
        encoding="utf-8",
    )

    offenders: list[Path] = []
    for path in tmp_path.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit_market_order"
            ):
                offenders.append(path)
                break

    assert offenders == [planted]
