"""Trades and executions: the closed record.

252 closed round trips and 208 fills, imported from the MT5 demo ledger and
reconciled against the report `tools/track_record.py` produced from the same
file. This is the journal's raw material; the journal *service* (notes, tags,
review) is L31 and is not here.

One thing this module refuses to do: aggregate. A total P&L over these rows
would pool trades sized by stop distances that differ 5x, which is the
metals-points error wearing a lot size. `r_multiple` is the poolable figure
and it is on every row; the statistics that use it belong to L32, which has
the significance machinery. Serving a mean here would be the wrong number
computed in the wrong place.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, page_params
from app.api.v1.schemas import ExecutionOut, TradeOut
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.journal import quality
from app.journal.lifecycle import Holding
from app.journal.service import TradeJournalService
from app.services import execution as svc
from app.services.journal import export as journal_export
from app.services.journal import statistics as journal_stats

#: One instance. It holds no state, so a per-request one would allocate for
#: nothing -- the same reasoning the portfolio router records.
_JOURNAL_SERVICE = TradeJournalService()

#: Section 47. The export's columns, fixed and ordered. `mode` is second so a
#: paper row and a live row can never be confused in a spreadsheet.
EXPORT_COLUMNS = (
    "trade_id",
    "mode",
    "status",
    "symbol",
    "side",
    "volume",
    "entry_price",
    "exit_price",
    "opened_at",
    "closed_at",
    "holding_seconds",
    "gross_profit",
    "commission",
    "swap",
    "fees",
    "net_profit",
    "currency",
    "r_multiple",
    "exit_reason",
    "strategy_version_id",
    "bot_id",
    "position_id",
    "order_id",
    "broker_position_id",
    "source",
)


def export_row(row: Any, symbol: str | None) -> list[Any]:
    """One CSV row. No identifier that is a credential, and no payload."""
    span = Holding.between(row.opened_at, row.closed_at)
    return [
        row.id,
        row.mode,
        row.status,
        symbol or row.symbol_id,
        row.side,
        row.volume,
        row.entry_price,
        row.exit_price,
        row.opened_at.isoformat(),
        row.closed_at.isoformat(),
        None if span is None else span.total_seconds(),
        row.gross_profit,
        row.commission,
        row.swap,
        row.fees,
        row.net_profit,
        row.currency,
        row.r_multiple,
        row.exit_reason,
        row.strategy_version_id,
        row.bot_id,
        row.position_id,
        row.order_id,
        row.broker_position_id,
        row.source,
    ]


router = APIRouter(tags=["trades"])

_JOURNAL = Depends(require_permission(Permission.view_journal))
_PORTFOLIO = Depends(require_permission(Permission.view_portfolio))


@router.get(
    "/trades",
    response_model=Page[TradeOut],
    summary="Closed round trips",
    description=(
        "Newest close first. Read `r_multiple`, not `net_profit`: net currency "
        "cannot be pooled across trades whose stops differ in distance. Every "
        "row carries its `mode`, and no mode filter is applied by default."
    ),
)
async def list_trades(
    params: PageParams = Depends(page_params),
    mode: str | None = Query(None, description="paper | demo | live"),
    side: str | None = Query(None, description="long | short"),
    symbol: str | None = Query(None, max_length=32),
    result: str | None = Query(None, description="win | loss | flat"),
    from_time: datetime | None = Query(None, description="closed_at lower bound, UTC."),
    to_time: datetime | None = Query(None, description="closed_at upper bound, UTC."),
    sort: str | None = Query(None, description="closed_at | opened_at | net_profit | r_multiple"),
    order: str | None = Query(None, description="asc | desc"),
    account_id: str | None = Query(
        None, max_length=36, description="One account, paper or broker."
    ),
    strategy_version_id: str | None = Query(None, max_length=36),
    bot_id: str | None = Query(None, max_length=36),
    exit_reason: str | None = Query(None, max_length=32),
    status: str | None = Query(None, description="closed | reconciliation_required | unknown"),
    model_key: str | None = Query(None, max_length=64, description="AI model this trade used."),
    model_version: str | None = Query(None, max_length=32, description="Exact model version."),
    ai_mode: str | None = Query(None, max_length=32, description="AI_DISABLED | ADVISORY | ..."),
    search: str | None = Query(
        None, max_length=64, description="Exact match on trade, position, order or broker id."
    ),
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> Page[TradeOut]:
    rows, page, codes = await svc.list_trades(
        db,
        params,
        mode=mode,
        side=side,
        symbol=symbol,
        result=result,
        from_time=from_time,
        to_time=to_time,
        sort=sort,
        order=order,
        account_id=account_id,
        strategy_version_id=strategy_version_id,
        bot_id=bot_id,
        exit_reason=exit_reason,
        status=status,
        model_key=model_key,
        model_version=model_version,
        ai_mode=ai_mode,
        search=search,
    )
    items = [
        TradeOut.model_validate(r, from_attributes=True).model_copy(
            update={"symbol": codes.get(r.symbol_id)}
        )
        for r in rows
    ]
    return Page[TradeOut](items=items, page=page)


# Section 33. The STATIC paths are registered before `/trades/{trade_id}`,
# because Starlette matches in registration order and the parameterised
# route would otherwise capture `statistics` and `export` as trade ids and
# answer 404. A test asserts both still resolve.
@router.get(
    "/trades/statistics",
    summary="Counts and totals over the filtered set",
    description=(
        "Descriptive only. Read `r_multiple` rather than net currency: net "
        "cannot be pooled across trades sized by different stop distances, "
        "which is the arithmetic error this repository's own live record "
        "documents. Sharpe, Sortino and the rest are L32's, deliberately."
    ),
)
async def trade_statistics(
    mode: str | None = Query(None, description="paper | demo | live"),
    account_id: str | None = Query(None, max_length=36),
    from_time: datetime | None = Query(None),
    to_time: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> dict[str, Any]:
    return await journal_stats(
        db, mode=mode, account_id=account_id, from_time=from_time, to_time=to_time
    )


@router.get(
    "/trades/export",
    summary="The filtered set as CSV",
    description=(
        "Respects every filter, the account scope and the environment scope. "
        "One row per trade, with `mode` in the first columns so a paper row "
        "and a live row can never be confused in a spreadsheet."
    ),
)
async def export_trades(
    mode: str | None = Query(None, description="paper | demo | live"),
    account_id: str | None = Query(None, max_length=36),
    symbol: str | None = Query(None, max_length=32),
    from_time: datetime | None = Query(None),
    to_time: datetime | None = Query(None),
    limit: int = Query(5000, ge=1, le=50000, description="Hard ceiling; never the whole table."),
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> StreamingResponse:
    rows, codes = await journal_export(
        db,
        mode=mode,
        account_id=account_id,
        symbol=symbol,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow(export_row(row, codes.get(row.symbol_id)))
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trades.csv"'},
    )


@router.get("/trades/{trade_id}", response_model=TradeOut, summary="One closed trade")
async def get_trade(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> TradeOut:
    row, code = await svc.get_trade(db, trade_id)
    return TradeOut.model_validate(row, from_attributes=True).model_copy(update={"symbol": code})


@router.get(
    "/trades/{trade_id}/timeline",
    summary="Everything that happened, chronologically",
    description=(
        "Derived at read time from the rows each system already wrote -- the "
        "webhook, the signal, the risk decision, the AI decision, the order and "
        "its events, every fill, and the position lifecycle. Nothing is stored: "
        "a second copy could fall behind, and when it disagreed there would be "
        "no way to tell which was right. `gaps` names what is missing."
    ),
)
async def trade_timeline(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> dict[str, Any]:
    row, _code = await svc.get_trade(db, trade_id)
    return await _JOURNAL_SERVICE.timeline_for(db, row)


@router.get(
    "/trades/{trade_id}/decisions",
    summary="What the system knew when the trade was opened",
    description=(
        "Strategy, AI, risk, sizing and execution context, read from the rows "
        "written at the time. Never recalculated with today's values, which "
        "would produce a number describing the platform now and claiming to "
        "describe the trade then. An absent block says which kind of absent."
    ),
)
async def trade_decisions(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> dict[str, Any]:
    row, _code = await svc.get_trade(db, trade_id)
    return await _JOURNAL_SERVICE.context_for(db, row)


@router.get(
    "/trades/{trade_id}/executions",
    summary="The fills behind one trade",
    description=(
        "Each confirmed fill and each partial close. The exit price on the "
        "trade is the volume-weighted average of the closes; realized P&L is "
        "summed from them and never recomputed from that average, because "
        "doing both is how a partial close gets counted twice."
    ),
)
async def trade_executions(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> dict[str, Any]:
    row, _code = await svc.get_trade(db, trade_id)
    return await _JOURNAL_SERVICE.closes_for(db, row)


@router.get(
    "/trades/{trade_id}/analysis",
    summary="Data-quality findings and the holding period",
    description=(
        "What did not add up, detected and flagged rather than corrected: a "
        "completed trade is a historical fact, and a silently repaired one "
        "looks clean and is wrong. `checked: false` means the checks have not "
        "run, which is not the same as nothing being wrong."
    ),
)
async def trade_analysis(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = _JOURNAL,
) -> dict[str, Any]:
    row, _code = await svc.get_trade(db, trade_id)
    closes = await _JOURNAL_SERVICE.closes_for(db, row)
    live = quality.check(row, closes=len(closes.get("closes") or []) or None)
    return {
        "trade_id": row.id,
        "status": row.status,
        "recorded": row.data_quality,
        "recomputed": quality.summarise(live),
        "holding": Holding.as_dict(row.opened_at, row.closed_at),
        "note": (
            "`recorded` is what was written when the trade was journalled; "
            "`recomputed` is the same checks run against the row as it stands "
            "now. They should agree, and a difference means the row was edited."
        ),
    }


@router.get(
    "/executions",
    response_model=Page[ExecutionOut],
    summary="Recorded fills",
    description=(
        "A fill is what the venue reported, never what was requested. "
        "`fill_source` distinguishes a simulator fill from a broker fill and "
        "the two are never merged. `slippage_points` is the gap between the "
        "price the tool read and the price that came back."
    ),
)
async def list_executions(
    params: PageParams = Depends(page_params),
    fill_source: str | None = Query(None, description="simulator | broker"),
    from_time: datetime | None = Query(None),
    to_time: datetime | None = Query(None),
    sort: str | None = Query(None, description="executed_at | price"),
    order: str | None = Query(None, description="asc | desc"),
    db: AsyncSession = Depends(get_db),
    _: User = _PORTFOLIO,
) -> Page[ExecutionOut]:
    rows, page = await svc.list_executions(
        db,
        params,
        fill_source=fill_source,
        from_time=from_time,
        to_time=to_time,
        sort=sort,
        order=order,
    )
    items = [ExecutionOut.model_validate(r, from_attributes=True) for r in rows]
    return Page[ExecutionOut](items=items, page=page)
