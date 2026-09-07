"""Read contract specs from a running MetaTrader 5 terminal.

    python -m app.symbols.sync_mt5              # every mapped mt5 symbol
    python -m app.symbols.sync_mt5 --symbols EURUSD,DE40

Read-only with respect to trading: it calls `symbol_info` and
`symbol_info_session_*` and nothing that mutates. If the terminal is not
running the command fails and writes nothing. There is no offline fallback
and no default spec, because a guessed tick value produces a real order for
the wrong size.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.session import make_engine, make_session_factory
from app.models.market import Symbol, SymbolMapping
from app.symbols.service import upsert_mapping

DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


class TerminalUnavailable(RuntimeError):
    pass


def connect(path: str | None = None):
    """Open the terminal. Mirrors tools/mt5_paper.connect rather than replacing it."""
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise TerminalUnavailable(
            "MetaTrader5 is not installed (Windows-only, optional): pip install MetaTrader5"
        ) from exc
    ok = mt5.initialize(path=path, timeout=60000) if path else mt5.initialize(timeout=60000)
    if not ok:
        raise TerminalUnavailable(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    d = Decimal(str(value))
    return d if d != 0 else None


def _precision_of(step: object) -> int | None:
    if step is None:
        return None
    text = format(Decimal(str(step)).normalize(), "f")
    return len(text.split(".")[1]) if "." in text else 0


def sessions_api_available(mt5) -> bool:
    """Whether this MetaTrader5 build exposes per-weekday trading sessions.

    Build 5.0.6090 does not: it has no `symbol_info_session_quote`, and the
    `session_*` fields on `symbol_info` are today's turnover and volume
    statistics, not a schedule. Checking once and reporting the absence beats
    catching an AttributeError per weekday, which would make "this package
    cannot tell us" indistinguishable from "this symbol has no session".
    """
    return hasattr(mt5, "symbol_info_session_quote")


def read_sessions(mt5, broker_symbol: str) -> dict | None:
    """Quote sessions per weekday, or None when they cannot be read.

    None means "no schedule is known", never "trades around the clock".
    Nothing downstream may treat a missing schedule as an open market.
    """
    if not sessions_api_available(mt5):
        return None

    out: dict[str, list[dict[str, str]]] = {}
    for index, name in enumerate(DAYS):
        sessions = []
        for slot in range(8):
            got = mt5.symbol_info_session_quote(broker_symbol, index, slot)
            if not got:
                break
            start, end = getattr(got, "from", None), getattr(got, "to", None)
            if start is None or end is None:
                break
            sessions.append(
                {
                    "from": time(int(start // 3600) % 24, int(start % 3600 // 60)).isoformat(
                        timespec="minutes"
                    ),
                    "to": time(int(end // 3600) % 24, int(end % 3600 // 60)).isoformat(
                        timespec="minutes"
                    ),
                }
            )
        if sessions:
            out[name] = sessions
    return {"timezone": "broker_server", "quote_sessions": out} if out else None


def spec_from_terminal(mt5, broker_symbol: str) -> dict:
    """Contract spec straight off the terminal. Raises when the symbol is absent."""
    if not mt5.symbol_select(broker_symbol, True):
        raise TerminalUnavailable(f"broker does not list {broker_symbol!r}")
    info = mt5.symbol_info(broker_symbol)
    if info is None:
        raise TerminalUnavailable(f"no symbol_info for {broker_symbol!r}")
    step = _decimal(getattr(info, "volume_step", None))
    return {
        "contract_size": _decimal(getattr(info, "trade_contract_size", None)),
        "tick_size": _decimal(getattr(info, "trade_tick_size", None)),
        "tick_value": _decimal(getattr(info, "trade_tick_value", None)),
        "minimum_volume": _decimal(getattr(info, "volume_min", None)),
        "maximum_volume": _decimal(getattr(info, "volume_max", None)),
        "volume_step": step,
        "price_precision": getattr(info, "digits", None),
        "volume_precision": _precision_of(step),
        "trading_hours": read_sessions(mt5, broker_symbol),
        "spec_source": "mt5_terminal",
    }


async def sync(db: AsyncSession, codes: list[str] | None, path: str | None) -> dict[str, object]:
    rows = (
        await db.execute(
            select(Symbol.code, SymbolMapping.provider_symbol)
            .join(SymbolMapping, SymbolMapping.symbol_id == Symbol.id)
            .where(SymbolMapping.provider == "mt5")
            .order_by(Symbol.code)
        )
    ).all()
    if codes:
        wanted = {c.strip().upper() for c in codes}
        rows = [r for r in rows if r[0] in wanted]

    mt5 = connect(path)
    updated: list[str] = []
    failed: dict[str, str] = {}
    notes: list[str] = []
    if not sessions_api_available(mt5):
        notes.append(
            "this MetaTrader5 build exposes no per-weekday session API, so "
            "trading_hours stays null; nothing may read a missing schedule as "
            "an open market"
        )
    try:
        for code, broker_symbol in rows:
            try:
                spec = spec_from_terminal(mt5, broker_symbol)
            except TerminalUnavailable as exc:
                failed[code] = str(exc)
                continue
            missing = [k for k, v in spec.items() if v is None and k != "trading_hours"]
            if missing:
                failed[code] = f"terminal returned no {', '.join(missing)}"
                continue
            await upsert_mapping(db, code, "mt5", broker_symbol, **spec)
            updated.append(code)
        await db.commit()
    finally:
        mt5.shutdown()
    return {"updated": updated, "failed": failed, "checked": len(rows), "notes": notes}


async def _run(codes: list[str] | None, path: str | None) -> int:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as db:
            result = await sync(db, codes, path)
        updated: list[str] = result["updated"]  # type: ignore[assignment]
        failed: dict[str, str] = result["failed"]  # type: ignore[assignment]
        print(f"checked {result['checked']}  updated {len(updated)}  refused {len(failed)}")
        for code in updated:
            print(f"  ok      {code}")
        for code, why in failed.items():
            print(f"  REFUSED {code}: {why}")
        notes: list[str] = result["notes"]  # type: ignore[assignment]
        for note in notes:
            print(f"  note: {note}")
        # A refusal is a non-zero exit. A spec this tool could not read is a
        # spec nothing downstream may size an order with.
        return 1 if failed else 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", help="comma-separated internal codes; default every mapped one")
    ap.add_argument("--path", help="terminal64.exe path")
    args = ap.parse_args(argv)
    codes = args.symbols.split(",") if args.symbols else None
    try:
        return asyncio.run(_run(codes, args.path))
    except TerminalUnavailable as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        print("Nothing was written. Specs are read from a live terminal only.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
