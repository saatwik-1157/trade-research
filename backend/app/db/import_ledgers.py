"""Import the JSONL ledgers into the database, idempotently, and reconcile.

    python -m app.db.import_ledgers --orders ../data/paper_trades.jsonl \
        --trades ../data/track_record.jsonl --reference ../reports/track_record.json

The JSONL files are never modified. Rows are keyed so a second run changes
nothing: orders by `intent_id = jsonl:order:<ticket>` (or the line number for
rejected orders, which have no ticket), trades by `broker_position_id`.
Every imported row carries `source='jsonl_import'` and `mode='demo'`,
because that is what they are: MT5 demo-account trades.

Reconciliation compares the imported trades against the reference report
that `tools/track_record.py` produced from the same ledger. A mismatch is a
non-zero exit, not a warning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.core.settings import get_settings
from app.db.session import make_engine, make_session_factory
from app.models import Execution, Order, OrderEvent, Symbol, Trade

MODE = "demo"
SOURCE = "jsonl_import"
MAJORS = {"EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD"}


def _dt(value: str) -> datetime:
    """ISO string (with or without offset) -> naive UTC.

    The ledgers mix both forms: order rows carry +00:00, trade rows do not
    (they come from MT5 stamps that are already the server's wall clock).
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _digits(price: object) -> int | None:
    s = str(price)
    return len(s.split(".")[1]) if "." in s else None


def _read_jsonl(path: Path) -> list[tuple[int, dict]]:
    rows: list[tuple[int, dict]] = []
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if line:
                rows.append((n, json.loads(line)))
    return rows


async def ensure_symbol(
    db: AsyncSession, cache: dict[str, Symbol], code: str, price: object
) -> Symbol:
    if code in cache:
        return cache[code]
    sym = await db.scalar(select(Symbol).where(Symbol.code == code))
    if sym is None:
        digits = _digits(price)
        sym = Symbol(
            code=code,
            asset_class="fx" if code in MAJORS or len(code) == 6 else "other",
            base_currency=code[:3] if len(code) == 6 else None,
            quote_currency=code[3:] if len(code) == 6 else None,
            digits=digits,
            point_size=Decimal(1).scaleb(-digits) if digits else None,
            unit_class="points",
        )
        db.add(sym)
        await db.flush()
    cache[code] = sym
    return sym


async def import_orders(db: AsyncSession, path: Path) -> dict[str, int]:
    counts = {"orders": 0, "executions": 0, "events": 0, "skipped": 0}
    cache: dict[str, Symbol] = {}
    by_ticket: dict[str, Order] = {}

    for line_no, r in _read_jsonl(path):
        if r.get("event") != "order":
            continue
        ticket = r.get("order") or 0
        intent_id = f"jsonl:order:{ticket}" if ticket else f"jsonl:order:line{line_no}"
        existing = await db.scalar(select(Order).where(Order.intent_id == intent_id))
        if existing is not None:
            counts["skipped"] += 1
            if ticket:
                by_ticket[str(ticket)] = existing
            continue
        sym = await ensure_symbol(db, cache, r["symbol"], r["price"])
        when = _dt(r["time"])
        rejected = r.get("status") == "REJECTED"
        fill = _dec(r.get("fill_price"))
        order = Order(
            intent_id=intent_id,
            mode=MODE,
            symbol_id=sym.id,
            side=r["side"],
            order_type="market",
            quantity=_dec(r["lot"]),
            requested_price=_dec(r["price"]),
            stop_loss=_dec(r.get("sl")),
            take_profit=_dec(r.get("tp")),
            status="rejected" if rejected else ("filled" if fill else "accepted"),
            broker_order_id=str(ticket) if ticket else None,
            magic=770315,
            sizing=r.get("sizing"),
            source=SOURCE,
            submitted_at=when,
            created_at=when,
            updated_at=when,
        )
        db.add(order)
        await db.flush()
        counts["orders"] += 1
        if ticket:
            by_ticket[str(ticket)] = order

        db.add(
            OrderEvent(
                order_id=order.id,
                event_type="rejected" if rejected else "submitted",
                occurred_at=when,
                retcode=r.get("retcode"),
                comment=r.get("comment"),
                payload={k: v for k, v in r.items() if k not in ("event",)},
            )
        )
        counts["events"] += 1
        if r.get("bracket_repaired"):
            db.add(
                OrderEvent(
                    order_id=order.id,
                    event_type="bracket_repaired",
                    occurred_at=when,
                    retcode=r.get("bracket_repair_retcode"),
                    payload={"failed_closed": bool(r.get("bracket_repair_failed_closed"))},
                )
            )
            counts["events"] += 1
        if fill is not None:
            db.add(
                Execution(
                    order_id=order.id,
                    executed_at=when,
                    price=fill,
                    quantity=_dec(r["lot"]),
                    slippage_points=_dec(r.get("slippage_points")),
                    fill_source="import",
                )
            )
            counts["executions"] += 1

    # Close events reference the position ticket, which for a market order
    # equals the order ticket on this broker.
    for _line_no, r in _read_jsonl(path):
        if r.get("event") != "close":
            continue
        closed_order = by_ticket.get(str(r.get("ticket")))
        if closed_order is None:
            continue
        when = _dt(r["time"])
        dup = await db.scalar(
            select(func.count())
            .select_from(OrderEvent)
            .where(
                OrderEvent.order_id == closed_order.id,
                OrderEvent.event_type == "close",
                OrderEvent.occurred_at == when,
            )
        )
        if dup:
            continue
        db.add(
            OrderEvent(
                order_id=closed_order.id,
                event_type="close",
                occurred_at=when,
                retcode=r.get("retcode"),
                payload={"status": r.get("status")},
            )
        )
        counts["events"] += 1

    await db.commit()
    return counts


async def import_trades(db: AsyncSession, path: Path) -> dict[str, int]:
    counts = {"trades": 0, "skipped": 0}
    cache: dict[str, Symbol] = {}
    now = utcnow()
    for _line_no, r in _read_jsonl(path):
        pid = str(r["position_id"])
        if await db.scalar(select(Trade.id).where(Trade.broker_position_id == pid)):
            counts["skipped"] += 1
            continue
        sym = await ensure_symbol(db, cache, r["symbol"], r["entry_price"])
        bracket = dict(r.get("bracket") or {})
        bracket["inverted_at_fill"] = bool(r.get("bracket_inverted_at_fill"))
        db.add(
            Trade(
                mode=MODE,
                broker_position_id=pid,
                symbol_id=sym.id,
                side=r["direction"],
                volume=_dec(r["volume"]),
                entry_price=_dec(r["entry_price"]),
                exit_price=_dec(r["exit_price"]),
                opened_at=_dt(r["open_time"]),
                closed_at=_dt(r["close_time"]),
                gross_profit=_dec(r["gross_profit"]),
                commission=_dec(r.get("commission") or 0),
                swap=_dec(r.get("swap") or 0),
                net_profit=_dec(r["net_profit"]),
                r_multiple=_dec(r.get("r_multiple")),
                bracket=bracket,
                close_reason=r.get("close_reason"),
                source=SOURCE,
                imported_at=now,
            )
        )
        counts["trades"] += 1
    await db.commit()
    return counts


async def reconcile(db: AsyncSession, reference: dict) -> tuple[bool, dict]:
    rows = (
        await db.execute(
            select(Trade.r_multiple, Trade.net_profit).where(
                Trade.source == SOURCE, Trade.mode == MODE
            )
        )
    ).all()
    n_trades = len(rows)
    rs = [float(r) for r, _ in rows if r is not None]
    got = {
        "trades": n_trades,
        "r_trades": len(rs),
        "r_mean": round(mean(rs), 4) if rs else None,
        "net_currency_total": round(sum(float(n) for _, n in rows), 2),
    }
    ref = reference.get("pooled_r_multiple") or {}
    want = {
        "trades": reference.get("trades"),
        "r_trades": ref.get("trades"),
        "r_mean": ref.get("mean"),
        "net_currency_total": reference.get("net_currency_total"),
    }
    ok = all(got[k] == want[k] for k in want)
    return ok, {"got": got, "want": want}


async def run(orders: Path | None, trades: Path | None, reference: Path | None) -> int:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as db:
            if orders:
                print("orders:", await import_orders(db, orders))
            if trades:
                print("trades:", await import_trades(db, trades))
            if reference:
                ok, detail = await reconcile(db, json.loads(reference.read_text(encoding="utf-8")))
                print("reconcile:", "OK" if ok else "MISMATCH", json.dumps(detail))
                return 0 if ok else 1
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orders", type=Path, help="paper_trades.jsonl")
    ap.add_argument("--trades", type=Path, help="track_record.jsonl")
    ap.add_argument("--reference", type=Path, help="track_record.json to reconcile against")
    args = ap.parse_args(argv)
    for p in (args.orders, args.trades, args.reference):
        if p and not p.exists():
            print(f"missing: {p}", file=sys.stderr)
            return 2
    return asyncio.run(run(args.orders, args.trades, args.reference))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    raise SystemExit(main())
