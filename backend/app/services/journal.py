"""Journal queries: statistics and export. Sections 42, 46 and 47.

**Descriptive, never predictive.** §42 says L32 owns analytics and this level
must not implement it. So `statistics` counts, sums and averages the filtered
set and stops there — no Sharpe, no Sortino, no expectancy curve, no equity
series. The line is not arbitrary: those are the figures somebody quotes as a
track record, and this repository's own measured position is that a t-statistic
on a small live sample is the most misleading number it has ever produced.
`CLAUDE.md` records a live t of 9.33 that meant nothing.

**Read R, not net currency.** The same file records why: a trade's size is set
by its stop distance, and pooling net currency across trades sized differently
is structurally the metals-points error wearing a lot size. Both are returned,
and the note says which to read.

**Never load the whole table.** §46. Every query here is aggregated in the
database or hard-limited, and the export takes a ceiling it cannot be asked to
exceed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import Trade
from app.models.market import Symbol

#: §47. A hard ceiling on an export, so a filter that matches everything cannot
#: turn into a full table scan streamed to a browser.
MAX_EXPORT = 50_000


def _scoped(
    stmt: Select,
    *,
    mode: str | None,
    account_id: str | None,
    symbol_id: str | None = None,
    from_time: datetime | None,
    to_time: datetime | None,
) -> Select:
    """The filters both callers share, applied identically.

    One function rather than two copies: statistics computed over one filter set
    and an export produced over a slightly different one is a discrepancy nobody
    would notice until they added the CSV up by hand.
    """
    if mode:
        stmt = stmt.where(Trade.mode == mode)
    if account_id:
        stmt = stmt.where(
            (Trade.paper_account_id == account_id) | (Trade.broker_account_id == account_id)
        )
    if symbol_id:
        stmt = stmt.where(Trade.symbol_id == symbol_id)
    if from_time:
        stmt = stmt.where(Trade.closed_at >= from_time)
    if to_time:
        stmt = stmt.where(Trade.closed_at <= to_time)
    return stmt


async def statistics(
    db: AsyncSession,
    *,
    mode: str | None = None,
    account_id: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> dict[str, Any]:
    """Counts and totals over the filtered set. Aggregated in the database."""
    base = _scoped(
        select(Trade),
        mode=mode,
        account_id=account_id,
        from_time=from_time,
        to_time=to_time,
    )
    counted = base.with_only_columns(
        func.count(Trade.id),
        func.sum(Trade.net_profit),
        func.sum(Trade.gross_profit),
        func.sum(Trade.commission),
        func.sum(Trade.swap),
        func.avg(Trade.r_multiple),
        func.sum(Trade.r_multiple),
        func.count(Trade.r_multiple),
    ).order_by(None)
    row = (await db.execute(counted)).one()
    total, net, gross, commission, swap, avg_r, sum_r, r_count = row

    wins = await db.scalar(
        base.with_only_columns(func.count(Trade.id)).where(Trade.net_profit > 0).order_by(None)
    )
    losses = await db.scalar(
        base.with_only_columns(func.count(Trade.id)).where(Trade.net_profit < 0).order_by(None)
    )
    flags = await db.scalar(
        base.with_only_columns(func.count(Trade.id))
        .where(Trade.status == "reconciliation_required")
        .order_by(None)
    )

    by_reason = (
        await db.execute(
            base.with_only_columns(Trade.exit_reason, func.count(Trade.id))
            .group_by(Trade.exit_reason)
            .order_by(None)
        )
    ).all()
    by_mode = (
        await db.execute(
            base.with_only_columns(Trade.mode, func.count(Trade.id))
            .group_by(Trade.mode)
            .order_by(None)
        )
    ).all()

    total = int(total or 0)
    return {
        "trades": total,
        "wins": int(wins or 0),
        "losses": int(losses or 0),
        # `win_rate` is None on an empty set, never 0: nought wins from nought
        # trades is not a nought percent win rate, it is no measurement.
        "win_rate": (int(wins or 0) / total) if total else None,
        "net_profit": _s(net),
        "gross_profit": _s(gross),
        "commission": _s(commission),
        "swap": _s(swap),
        "r_multiple": {
            "sum": _s(sum_r),
            "mean": _s(avg_r),
            "trades_with_r": int(r_count or 0),
            "note": (
                "the figure to pool. Net currency cannot be pooled across trades "
                "sized by different stop distances -- dividing by the money at risk "
                "is what removes the regime."
            ),
        },
        "by_exit_reason": {(reason or "unrecorded"): int(count) for reason, count in by_reason},
        "by_mode": {name: int(count) for name, count in by_mode},
        "reconciliation_required": int(flags or 0),
        "scope": {
            "mode": mode,
            "account_id": account_id,
            "from": from_time.isoformat() if from_time else None,
            "to": to_time.isoformat() if to_time else None,
            "note": (
                "no mode filter is applied unless one is asked for. Every row carries "
                "its own `mode`, and silently defaulting to paper would hide live "
                "trades from somebody who asked for all of them."
            ),
        },
        "not_computed": {
            "sharpe": "L32. Analytics owns it.",
            "sortino": "L32.",
            "expectancy_curve": "L32.",
            "significance": (
                "deliberately absent. A t-statistic on a small live sample is the most "
                "misleading number this repository has produced -- CLAUDE.md records a "
                "live t of 9.33 that was arithmetic rather than evidence."
            ),
        },
    }


def _s(value: Decimal | int | float | None) -> str | None:
    return None if value is None else str(value)


async def export(
    db: AsyncSession,
    *,
    mode: str | None = None,
    account_id: str | None = None,
    symbol: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = 5000,
) -> tuple[list[Trade], dict[str, str]]:
    """The filtered set, capped. Section 47."""
    symbol_id: str | None = None
    if symbol:
        symbol_id = await db.scalar(select(Symbol.id).where(Symbol.code == symbol.upper()))
        if symbol_id is None:
            # An unknown symbol returns nothing rather than everything. A filter
            # that silently stops applying is worse than one that matches no
            # rows, because the caller reads the result as the filtered set.
            return [], {}

    stmt = _scoped(
        select(Trade),
        mode=mode,
        account_id=account_id,
        symbol_id=symbol_id,
        from_time=from_time,
        to_time=to_time,
    ).order_by(Trade.closed_at.desc())

    rows = list((await db.scalars(stmt.limit(min(limit, MAX_EXPORT)))).all())
    codes: dict[str, str] = {}
    if rows:
        found = await db.execute(
            select(Symbol.id, Symbol.code).where(Symbol.id.in_({row.symbol_id for row in rows}))
        )
        codes = {symbol_id_: code for symbol_id_, code in found.all()}
    return rows, codes


__all__ = ["MAX_EXPORT", "export", "statistics"]
