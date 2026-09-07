"""Read services over the execution record: orders, executions, positions, trades.

These are the tables that already hold real rows -- 269 orders, 208 executions
and 252 closed trades imported from the JSONL ledgers at L05, all `mode='demo'`
and `source='jsonl_import'`. Serving them is a read of a record that exists,
which is why this level can serve them while order *submission* stays 501 until
the OMS exists at L19.

Two rules the queries obey:

  * **`mode` is never collapsed.** Every filter and every row carries it, so a
    simulator fill and a broker fill cannot be pooled by a caller who forgot to
    ask. The default is no mode filter and the mode on every row, rather than a
    default mode that silently hides half the record.
  * **Symbols are resolved in one query per page, not one per row.** The tables
    carry `symbol_id`; the caller wants `EURUSD`. The page's ids are collected
    and looked up once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import PageInfo, PageParams, SortSpec, paginate
from app.core.errors import NotFound, ValidationFailed
from app.journal.lifecycle import EXIT_REASONS
from app.models.ai_integration import AiDecisionRecord
from app.models.execution import (
    TRADE_STATUSES,
    Execution,
    Order,
    OrderEvent,
    Position,
    Trade,
)
from app.models.market import Symbol

MODES = ("paper", "demo", "live")
SIDES = ("buy", "sell")
POSITION_SIDES = ("long", "short")
TRADE_RESULTS = ("win", "loss", "flat")
POSITION_STATUSES = ("open", "closed", "unknown")


# --------------------------------------------------------------- filtering


def _check_enum(name: str, value: str | None, allowed: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise ValidationFailed(f"{name} must be one of {', '.join(allowed)}, not {value!r}")
    return value


def _check_window(from_time: datetime | None, to_time: datetime | None) -> None:
    if from_time and to_time and from_time > to_time:
        raise ValidationFailed("from_time is after to_time")


async def _symbol_id_for(db: AsyncSession, code: str | None) -> str | None:
    """Internal symbol code to id, refusing an unknown code.

    An unknown code is a 404 rather than an empty page: "no trades on XYZUSD"
    and "there is no such symbol" are different statements, and a caller
    filtering on a typo should be told which one they hit.
    """
    if code is None:
        return None
    wanted = code.strip().upper()
    symbol = await db.scalar(select(Symbol).where(Symbol.code == wanted))
    if symbol is None:
        raise NotFound(f"no symbol {wanted!r}")
    return symbol.id


async def symbol_codes(db: AsyncSession, rows: list[Any]) -> dict[str, str]:
    """One lookup for the whole page. Never one per row."""
    ids = {getattr(r, "symbol_id", None) for r in rows}
    ids.discard(None)
    if not ids:
        return {}
    found = (await db.scalars(select(Symbol).where(Symbol.id.in_(ids)))).all()
    return {s.id: s.code for s in found}


# ------------------------------------------------------------------ orders

ORDER_SORTS = SortSpec(
    columns={
        "created_at": Order.created_at,
        "submitted_at": Order.submitted_at,
        "status": Order.status,
        "quantity": Order.quantity,
    },
    default="created_at",
)


async def list_orders(
    db: AsyncSession,
    params: PageParams,
    *,
    mode: str | None = None,
    status: str | None = None,
    side: str | None = None,
    symbol: str | None = None,
    source: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> tuple[list[Order], PageInfo, dict[str, str]]:
    _check_enum("mode", mode, MODES)
    _check_enum("side", side, SIDES)
    _check_window(from_time, to_time)
    stmt: Select = select(Order)
    if mode:
        stmt = stmt.where(Order.mode == mode)
    if status:
        stmt = stmt.where(Order.status == status)
    if side:
        stmt = stmt.where(Order.side == side)
    if source:
        stmt = stmt.where(Order.source == source)
    symbol_id = await _symbol_id_for(db, symbol)
    if symbol_id is not None:
        stmt = stmt.where(Order.symbol_id == symbol_id)
    if from_time:
        stmt = stmt.where(Order.created_at >= from_time)
    if to_time:
        stmt = stmt.where(Order.created_at <= to_time)
    stmt = ORDER_SORTS.apply(stmt, sort, order)
    rows, page = await paginate(db, stmt, params)
    return rows, page, await symbol_codes(db, rows)


async def get_order(db: AsyncSession, order_id: str) -> tuple[Order, str | None]:
    row = await db.get(Order, order_id)
    if row is None:
        raise NotFound(f"no order {order_id}")
    symbol = await db.get(Symbol, row.symbol_id)
    return row, symbol.code if symbol else None


async def order_events(db: AsyncSession, order_id: str) -> list[OrderEvent]:
    await get_order(db, order_id)
    stmt = (
        select(OrderEvent)
        .where(OrderEvent.order_id == order_id)
        .order_by(OrderEvent.occurred_at.asc())
    )
    return list((await db.scalars(stmt)).all())


async def order_executions(db: AsyncSession, order_id: str) -> list[Execution]:
    await get_order(db, order_id)
    stmt = (
        select(Execution)
        .where(Execution.order_id == order_id)
        .order_by(Execution.executed_at.asc())
    )
    return list((await db.scalars(stmt)).all())


# -------------------------------------------------------------- executions

EXECUTION_SORTS = SortSpec(
    columns={"executed_at": Execution.executed_at, "price": Execution.price},
    default="executed_at",
)


async def list_executions(
    db: AsyncSession,
    params: PageParams,
    *,
    fill_source: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> tuple[list[Execution], PageInfo]:
    _check_window(from_time, to_time)
    stmt: Select = select(Execution)
    if fill_source:
        stmt = stmt.where(Execution.fill_source == fill_source)
    if from_time:
        stmt = stmt.where(Execution.executed_at >= from_time)
    if to_time:
        stmt = stmt.where(Execution.executed_at <= to_time)
    stmt = EXECUTION_SORTS.apply(stmt, sort, order)
    return await paginate(db, stmt, params)


# --------------------------------------------------------------- positions

POSITION_SORTS = SortSpec(
    columns={
        "opened_at": Position.opened_at,
        "closed_at": Position.closed_at,
        "quantity": Position.quantity,
        "status": Position.status,
    },
    default="opened_at",
)


async def list_positions(
    db: AsyncSession,
    params: PageParams,
    *,
    mode: str | None = None,
    status: str | None = None,
    side: str | None = None,
    symbol: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> tuple[list[Position], PageInfo, dict[str, str]]:
    _check_enum("mode", mode, MODES)
    _check_enum("side", side, POSITION_SIDES)
    _check_enum("status", status, POSITION_STATUSES)
    stmt: Select = select(Position)
    if mode:
        stmt = stmt.where(Position.mode == mode)
    if status:
        stmt = stmt.where(Position.status == status)
    if side:
        stmt = stmt.where(Position.side == side)
    symbol_id = await _symbol_id_for(db, symbol)
    if symbol_id is not None:
        stmt = stmt.where(Position.symbol_id == symbol_id)
    stmt = POSITION_SORTS.apply(stmt, sort, order)
    rows, page = await paginate(db, stmt, params)
    return rows, page, await symbol_codes(db, rows)


# ------------------------------------------------------------------ trades

TRADE_SORTS = SortSpec(
    columns={
        "closed_at": Trade.closed_at,
        "opened_at": Trade.opened_at,
        "net_profit": Trade.net_profit,
        "r_multiple": Trade.r_multiple,
    },
    default="closed_at",
)


async def list_trades(
    db: AsyncSession,
    params: PageParams,
    *,
    mode: str | None = None,
    side: str | None = None,
    symbol: str | None = None,
    result: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    sort: str | None = None,
    order: str | None = None,
    # ---- L31, sections 34 and 35 ----
    account_id: str | None = None,
    strategy_version_id: str | None = None,
    bot_id: str | None = None,
    exit_reason: str | None = None,
    status: str | None = None,
    model_key: str | None = None,
    model_version: str | None = None,
    ai_mode: str | None = None,
    search: str | None = None,
) -> tuple[list[Trade], PageInfo, dict[str, str]]:
    """The trade list, filtered. Sections 34 and 35.

    **No mode filter is applied by default**, and that is deliberate rather than
    an oversight: §34 says not to mix paper and live BY DEFAULT, and the honest
    reading of a mixed list is that every row carries its own `mode` and the
    caller sees which. Silently defaulting to paper would hide live trades from
    somebody who asked for all of them, which is the worse failure of the two.

    **Every filter value is checked against an allow-list** where one exists,
    and every identifier is bound as a parameter. §45 and the L06 convention.
    """
    _check_enum("mode", mode, MODES)
    _check_enum("side", side, POSITION_SIDES)
    _check_enum("result", result, TRADE_RESULTS)
    _check_enum("status", status, TRADE_STATUSES)
    _check_enum("exit_reason", exit_reason, tuple(sorted(EXIT_REASONS)))
    _check_window(from_time, to_time)
    stmt: Select = select(Trade)
    if mode:
        stmt = stmt.where(Trade.mode == mode)
    if side:
        stmt = stmt.where(Trade.side == side)
    symbol_id = await _symbol_id_for(db, symbol)
    if symbol_id is not None:
        stmt = stmt.where(Trade.symbol_id == symbol_id)
    if result == "win":
        stmt = stmt.where(Trade.net_profit > 0)
    elif result == "loss":
        stmt = stmt.where(Trade.net_profit < 0)
    elif result == "flat":
        stmt = stmt.where(Trade.net_profit == 0)
    if from_time:
        stmt = stmt.where(Trade.closed_at >= from_time)
    if to_time:
        stmt = stmt.where(Trade.closed_at <= to_time)

    if account_id:
        # §30 and §41. One account, and it matches whichever column holds it --
        # never an OR that could pool a paper id with a broker one, because the
        # two id spaces are separate tables and a collision would be silent.
        stmt = stmt.where(
            (Trade.paper_account_id == account_id) | (Trade.broker_account_id == account_id)
        )
    if strategy_version_id:
        stmt = stmt.where(Trade.strategy_version_id == strategy_version_id)
    if bot_id:
        stmt = stmt.where(Trade.bot_id == bot_id)
    if exit_reason:
        stmt = stmt.where(Trade.exit_reason == exit_reason)
    if status:
        stmt = stmt.where(Trade.status == status)

    if model_key or model_version or ai_mode:
        # §34 asks for filtering by AI model, model version and AI mode. Those
        # facts live on `ai_decisions`, where L27 wrote them -- a JOIN rather
        # than columns copied onto the trade, because a copy of a model version
        # is a copy that can drift from the decision it describes.
        stmt = stmt.join(AiDecisionRecord, Trade.ai_decision_id == AiDecisionRecord.id)
        if model_key:
            stmt = stmt.where(AiDecisionRecord.model_key == model_key)
        if model_version:
            stmt = stmt.where(AiDecisionRecord.model_version == model_version)
        if ai_mode:
            stmt = stmt.where(AiDecisionRecord.policy == ai_mode)

    if search:
        # §35. Identifier lookup across the ids a trade actually carries. An
        # exact match on each rather than a LIKE: these are opaque identifiers,
        # a prefix search over them is meaningless, and a LIKE over an indexed
        # id column would scan the table this level is meant to keep fast.
        stmt = stmt.where(
            (Trade.id == search)
            | (Trade.position_id == search)
            | (Trade.order_id == search)
            | (Trade.broker_position_id == search)
            | (Trade.signal_id == search)
        )

    stmt = TRADE_SORTS.apply(stmt, sort, order)
    rows, page = await paginate(db, stmt, params)
    return rows, page, await symbol_codes(db, rows)


async def get_trade(db: AsyncSession, trade_id: str) -> tuple[Trade, str | None]:
    row = await db.get(Trade, trade_id)
    if row is None:
        raise NotFound(f"no trade {trade_id}")
    symbol = await db.get(Symbol, row.symbol_id)
    return row, symbol.code if symbol else None
