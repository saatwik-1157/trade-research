"""The JSONL importer is idempotent and reconciles against the reference report.

The fixture rows are copied verbatim from the real ledgers, so they are long
lines on purpose: reformatting them would stop them being what the file holds.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.db import import_ledgers as imp
from app.db.base import Base
from app.models import Execution, Order, OrderEvent, Trade, seed_roles
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

ORDER_LINES = [
    {
        "time": "2026-08-24T11:38:27+00:00",
        "event": "order",
        "symbol": "USDCAD",
        "side": "sell",
        "lot": 0.01,
        "price": 1.38411,
        "sl": 1.38626,
        "tp": 1.38196,
        "retcode": 10030,
        "comment": "Unsupported filling mode",
        "order": 0,
        "status": "REJECTED",
    },
    {
        "time": "2026-08-24T20:09:26+00:00",
        "event": "order",
        "symbol": "USDCAD",
        "side": "sell",
        "lot": 0.01,
        "price": 1.38426,
        "fill_price": 1.38425,
        "slippage_points": -1.0,
        "sl": 1.38641,
        "tp": 1.38211,
        "retcode": 10009,
        "comment": "Request executed",
        "order": 10167310954,
        "status": "SENT",
    },
    {
        "time": "2026-09-02T04:34:22+00:00",
        "event": "order",
        "symbol": "NZDUSD",
        "side": "sell",
        "lot": 0.02,
        "price": 0.58313,
        "fill_price": 0.58314,
        "slippage_points": 1.0,
        "sl": 0.58481,
        "tp": 0.58145,
        "retcode": 10009,
        "comment": "Request executed",
        "order": 10313269960,
        "status": "SENT",
        "sizing": {"lot": 0.02, "risk_requested": 5.0},
    },
    {
        "time": "2026-08-24T20:43:41+00:00",
        "event": "close",
        "symbol": "USDCAD",
        "ticket": 10167310954,
        "retcode": 10009,
        "status": "CLOSED",
    },
]
TRADE_LINES = [
    {
        "position_id": 10167310954,
        "symbol": "USDCAD",
        "direction": "short",
        "volume": 0.01,
        "open_time": "2026-08-24T20:09:26",
        "close_time": "2026-08-24T20:43:41",
        "entry_price": 1.38425,
        "exit_price": 1.38506,
        "gross_profit": -0.58,
        "commission": 0.0,
        "swap": 0.0,
        "net_profit": -0.58,
        "close_reason": 3,
        "bracket": {"reward_risk": 1.0, "sl_distance": 0.00215},
        "r_multiple": -0.3767,
        "bracket_inverted_at_fill": False,
    },
    {
        "position_id": 10313269960,
        "symbol": "NZDUSD",
        "direction": "short",
        "volume": 0.02,
        "open_time": "2026-09-02T04:34:22",
        "close_time": "2026-09-02T05:00:00",
        "entry_price": 0.58314,
        "exit_price": 0.58300,
        "gross_profit": 0.28,
        "commission": 0.0,
        "swap": 0.0,
        "net_profit": 0.28,
        "close_reason": 0,
        "bracket": {"reward_risk": 1.0},
        "r_multiple": 0.0833,
        "bracket_inverted_at_fill": False,
    },
    {
        "position_id": 99,
        "symbol": "EURUSD",
        "direction": "long",
        "volume": 0.01,
        "open_time": "2026-09-02T06:00:00",
        "close_time": "2026-09-02T07:00:00",
        "entry_price": 1.1,
        "exit_price": 1.1,
        "gross_profit": 0.0,
        "commission": 0.0,
        "swap": 0.0,
        "net_profit": 0.0,
        "close_reason": 0,
        "bracket": None,
        "r_multiple": None,
        "bracket_inverted_at_fill": False,
    },
]
REFERENCE = {
    "trades": 3,
    "net_currency_total": -0.3,
    "pooled_r_multiple": {"trades": 2, "mean": round((-0.3767 + 0.0833) / 2, 4)},
}


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    o = tmp_path / "paper_trades.jsonl"
    t = tmp_path / "track_record.jsonl"
    r = tmp_path / "track_record.json"
    o.write_text("\n".join(json.dumps(x) for x in ORDER_LINES) + "\n", encoding="utf-8")
    t.write_text("\n".join(json.dumps(x) for x in TRADE_LINES) + "\n", encoding="utf-8")
    r.write_text(json.dumps(REFERENCE), encoding="utf-8")
    return {"orders": o, "trades": t, "reference": r}


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(eng, expire_on_commit=False)() as session:
        await seed_roles(session)
        yield session
    await eng.dispose()


async def _counts(db: AsyncSession) -> dict[str, int]:
    out = {}
    for name, model in (
        ("orders", Order),
        ("events", OrderEvent),
        ("executions", Execution),
        ("trades", Trade),
    ):
        out[name] = int(await db.scalar(select(func.count()).select_from(model)) or 0)
    return out


async def test_import_maps_rows_and_is_idempotent(db: AsyncSession, files: dict[str, Path]) -> None:
    first_o = await imp.import_orders(db, files["orders"])
    first_t = await imp.import_trades(db, files["trades"])
    assert first_o == {"orders": 3, "executions": 2, "events": 4, "skipped": 0}
    assert first_t == {"trades": 3, "skipped": 0}
    before = await _counts(db)

    second_o = await imp.import_orders(db, files["orders"])
    second_t = await imp.import_trades(db, files["trades"])
    assert second_o["orders"] == 0 and second_o["skipped"] == 3 and second_o["events"] == 0
    assert second_t == {"trades": 0, "skipped": 3}
    assert await _counts(db) == before

    rejected = await db.scalar(select(Order).where(Order.intent_id == "jsonl:order:line1"))
    assert (
        rejected is not None and rejected.status == "rejected" and rejected.broker_order_id is None
    )
    filled = await db.scalar(select(Order).where(Order.broker_order_id == "10313269960"))
    assert filled is not None and filled.status == "filled" and filled.mode == "demo"
    assert filled.source == "jsonl_import" and filled.sizing == {"lot": 0.02, "risk_requested": 5.0}
    fill = await db.scalar(select(Execution).where(Execution.order_id == filled.id))
    assert fill is not None and str(fill.price) == "0.58314000" and fill.fill_source == "import"

    closed = await db.scalar(select(Order).where(Order.broker_order_id == "10167310954"))
    assert closed is not None
    kinds = [
        e.event_type
        for e in (
            await db.scalars(select(OrderEvent).where(OrderEvent.order_id == closed.id))
        ).all()
    ]
    assert kinds == ["submitted", "close"]

    trade = await db.scalar(select(Trade).where(Trade.broker_position_id == "10167310954"))
    assert trade is not None and trade.side == "short" and str(trade.r_multiple) == "-0.376700"
    assert trade.bracket == {"reward_risk": 1.0, "sl_distance": 0.00215, "inverted_at_fill": False}
    untracked = await db.scalar(select(Trade).where(Trade.broker_position_id == "99"))
    assert untracked is not None and untracked.r_multiple is None


async def test_reconcile_passes_on_matching_reference_and_fails_otherwise(
    db: AsyncSession, files: dict[str, Path]
) -> None:
    await imp.import_trades(db, files["trades"])
    ok, detail = await imp.reconcile(db, REFERENCE)
    assert ok, detail
    bad = dict(REFERENCE, trades=4)
    ok, detail = await imp.reconcile(db, bad)
    assert not ok and detail["got"]["trades"] == 3 and detail["want"]["trades"] == 4
