"""Daily/weekly loss and drawdown breach detection (R-19).

Daily and weekly loss are judged on "realized + unrealized" totals (spec
"ドローダウン判定の計算"). The trading day/week boundary is the human's
calendar in America/New_York, week starting Monday (plan.md リスク 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from trader.domain.money import Money
from trader.rules.schema import DerivedLimits

_NY = ZoneInfo("America/New_York")

LossKind = Literal["daily", "weekly", "drawdown"]


@dataclass(frozen=True, slots=True)
class LossLimitBreach:
    """A single loss/drawdown limit that has been reached.

    `observed` and `limit` are `Money` for the daily/weekly kinds and
    `Decimal` (a percentage) for `drawdown`. `reason` always carries both
    the derived amount and the underlying ratio so a human can recompute it
    (spec "rule_check_reason には導出した絶対額...を含める").
    """

    kind: LossKind
    observed: Money | Decimal
    limit: Money | Decimal
    reason: str


# Alias used by `entry_checks.check_entry`'s `loss_state` parameter: an
# already-reached breach, if any, computed once per cycle by `evaluate`.
type LossState = LossLimitBreach


def trading_day(now_utc: datetime) -> date:
    """The America/New_York calendar date for `now_utc` (spec リスク 4)."""
    return now_utc.astimezone(_NY).date()


def trading_week_start(now_utc: datetime) -> date:
    """The Monday that starts the America/New_York trading week of `now_utc`."""
    day = trading_day(now_utc)
    return day - timedelta(days=day.weekday())


def evaluate(
    limits: DerivedLimits,
    realized_today: Money,
    unrealized_now: Money,
    realized_week: Money,
    equity: Money,
    peak_equity: Money,
) -> LossLimitBreach | None:
    """Return the first loss/drawdown limit reached, or `None` (R-19).

    Checked in order: daily, weekly, drawdown. `peak_equity <= 0` skips the
    drawdown check (spec "peak <= 0 なら DD 判定しない") since a percentage
    of a non-positive peak is meaningless.
    """
    daily_total = realized_today + unrealized_now
    if daily_total.amount <= -limits.max_daily_loss.amount:
        return LossLimitBreach(
            kind="daily",
            observed=daily_total,
            limit=limits.max_daily_loss,
            reason=(
                f"daily P&L {daily_total.amount} <= -{limits.max_daily_loss.amount} "
                f"(realized {realized_today.amount} + unrealized {unrealized_now.amount})"
            ),
        )

    weekly_total = realized_week + unrealized_now
    if weekly_total.amount <= -limits.max_weekly_loss.amount:
        return LossLimitBreach(
            kind="weekly",
            observed=weekly_total,
            limit=limits.max_weekly_loss,
            reason=(
                f"weekly P&L {weekly_total.amount} <= -{limits.max_weekly_loss.amount} "
                f"(realized {realized_week.amount} + unrealized {unrealized_now.amount})"
            ),
        )

    if peak_equity.amount > 0:
        drawdown_pct = (peak_equity.amount - equity.amount) / peak_equity.amount * Decimal(100)
        if drawdown_pct >= limits.max_drawdown_pct:
            return LossLimitBreach(
                kind="drawdown",
                observed=drawdown_pct,
                limit=limits.max_drawdown_pct,
                reason=(
                    f"drawdown {drawdown_pct}% >= {limits.max_drawdown_pct}% "
                    f"(peak {peak_equity.amount} equity {equity.amount})"
                ),
            )

    return None
