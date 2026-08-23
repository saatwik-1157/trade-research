"""Read-only analysis of a MetaTrader 5 account.

Connects to a locally running MT5 terminal, reconstructs closed round-trip
trades from the raw deal log, and reports execution statistics.

READ ONLY BY CONSTRUCTION. This module never calls `order_send`, `order_check`,
`order_calc_margin` or any other mutating endpoint - the only MT5 functions it
touches are the `*_get` / `*_info` readers. Analysing a trading record and
placing orders are different activities, and mixing them into one tool is how
an analysis script turns into an execution script by accident.

MT5 records *deals*, not trades: opening and closing a position produces two
separate deals sharing a `position_id`. A win rate computed over deals rather
than round trips is roughly double-counted, so this pairs them first.

Usage:
    python tools/mt5_account.py                     # last 2 years
    python tools/mt5_account.py --days 3650         # last 10 years
    python tools/mt5_account.py --out reports/mt5.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import trade_stats

DEFAULT_TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# mt5 deal entry / type constants, named here so the pairing logic reads clearly
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3
DEAL_BUY, DEAL_SELL = 0, 1
TRADE_MODES = {0: "DEMO", 1: "CONTEST", 2: "REAL"}


def connect(path: str | None = None, timeout: int = 60000):
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:  # optional, Windows-only: keep the rest usable without it
        raise RuntimeError(
            "MetaTrader5 is not installed. It is an optional, Windows-only "
            "dependency: pip install MetaTrader5"
        ) from exc

    # An explicit terminal path is materially more reliable than letting the
    # package discover a running instance; bare initialize() returns IPC
    # timeout when the terminal is already up under another session.
    ok = mt5.initialize(path=path or DEFAULT_TERMINAL, timeout=timeout)
    if not ok:
        ok = mt5.initialize(timeout=timeout)
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


def account_block(mt5) -> dict:
    ti, ai = mt5.terminal_info(), mt5.account_info()
    out = {
        "terminal": {
            "build": getattr(ti, "build", None),
            "connected": getattr(ti, "connected", None),
            "data_path": getattr(ti, "data_path", None),
            "trade_allowed": getattr(ti, "trade_allowed", None),
        } if ti else None,
        "account": None,
    }
    if ai:
        out["account"] = {
            "login": ai.login,
            "server": ai.server,
            "name": ai.name,
            "company": ai.company,
            "type": TRADE_MODES.get(ai.trade_mode, ai.trade_mode),
            "currency": ai.currency,
            "balance": ai.balance,
            "equity": ai.equity,
            "margin": ai.margin,
            "margin_free": ai.margin_free,
            "floating_profit": ai.profit,
            "leverage": f"1:{ai.leverage}",
        }
    return out


def open_positions(mt5) -> list[dict]:
    rows = mt5.positions_get()
    out = []
    for p in rows or []:
        out.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "direction": "long" if p.type == DEAL_BUY else "short",
            "volume": p.volume,
            "open_time": datetime.fromtimestamp(p.time).isoformat(timespec="seconds"),
            "open_price": p.price_open,
            "current_price": p.price_current,
            "stop_loss": p.sl or None,
            "take_profit": p.tp or None,
            "floating_profit": round(p.profit, 2),
            "swap": round(p.swap, 2),
            "comment": p.comment,
        })
    return out


def pending_orders(mt5) -> list[dict]:
    rows = mt5.orders_get()
    return [{
        "ticket": o.ticket,
        "symbol": o.symbol,
        "volume": o.volume_current,
        "price_open": o.price_open,
        "stop_loss": o.sl or None,
        "take_profit": o.tp or None,
        "setup_time": datetime.fromtimestamp(o.time_setup).isoformat(timespec="seconds"),
    } for o in rows or []]


def closed_trades(mt5, since: datetime, until: datetime) -> tuple[list[dict], list[dict]]:
    """Pair deals into round-trip trades. Returns (trades, cash_movements)."""
    deals = mt5.history_deals_get(since, until)
    if deals is None:
        raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")

    by_position: dict[int, list] = {}
    cash: list[dict] = []
    for d in deals:
        # type >= 2 is a balance operation: deposit, withdrawal, credit, bonus.
        # Folding these into trade statistics would corrupt every ratio.
        if d.type >= 2:
            cash.append({
                "time": datetime.fromtimestamp(d.time).isoformat(timespec="seconds"),
                "type": int(d.type),
                "amount": round(d.profit, 2),
                "comment": d.comment,
            })
            continue
        by_position.setdefault(d.position_id, []).append(d)

    trades = []
    for pid, ds in by_position.items():
        ds.sort(key=lambda x: (x.time_msc, x.ticket))
        entries = [d for d in ds if d.entry in (ENTRY_IN, ENTRY_INOUT)]
        exits = [d for d in ds if d.entry in (ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY)]
        if not entries or not exits:
            continue  # still open, or history window clipped the other leg

        first, last = entries[0], exits[-1]
        volume = sum(d.volume for d in entries)
        # Volume-weighted prices, so partial fills and scale-ins are handled
        entry_px = sum(d.price * d.volume for d in entries) / volume if volume else first.price
        exit_vol = sum(d.volume for d in exits)
        exit_px = sum(d.price * d.volume for d in exits) / exit_vol if exit_vol else last.price

        net = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in ds)
        held = last.time - first.time
        trades.append({
            "position_id": pid,
            "symbol": first.symbol,
            "direction": "long" if first.type == DEAL_BUY else "short",
            "volume": round(volume, 4),
            "open_time": datetime.fromtimestamp(first.time).isoformat(timespec="seconds"),
            "close_time": datetime.fromtimestamp(last.time).isoformat(timespec="seconds"),
            "hold_seconds": int(held),
            "hold_hours": round(held / 3600, 2),
            "entry_price": round(entry_px, 6),
            "exit_price": round(exit_px, 6),
            "gross_profit": round(sum(d.profit for d in ds), 2),
            "commission": round(sum(d.commission for d in ds), 2),
            "swap": round(sum(d.swap for d in ds), 2),
            "net_profit": round(net, 2),
            "deal_count": len(ds),
            "close_reason": int(getattr(last, "reason", -1)),
        })

    trades.sort(key=lambda t: t["close_time"])
    return trades, cash


def analyse(days: int = 730, path: str | None = None) -> dict:
    mt5 = connect(path)
    try:
        until = datetime.now()
        since = until - timedelta(days=days)
        trades, cash = closed_trades(mt5, since, until)
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window": {"from": since.date().isoformat(), "to": until.date().isoformat(), "days": days},
            "access": "read-only (no order or modification calls are made by this tool)",
            **account_block(mt5),
            "open_positions": open_positions(mt5),
            "pending_orders": pending_orders(mt5),
            "closed_trades": trades,
            "cash_movements": cash,
            "statistics": trade_stats.statistics(trades, source="MetaTrader 5"),
        }
    finally:
        mt5.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only analysis of a MetaTrader 5 account.")
    ap.add_argument("--days", type=int, default=730, help="history window in days (default 730)")
    ap.add_argument("--path", default=None, help="path to terminal64.exe")
    ap.add_argument("--out", help="write the full JSON here")
    ap.add_argument("--json", action="store_true", help="print the full JSON to stdout")
    args = ap.parse_args()

    try:
        result = analyse(days=args.days, path=args.path)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}")
        print("\nChecks: is the MT5 terminal running and logged in? Is the terminal path correct?")
        return 1

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"Wrote {args.out}")

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    acc = result.get("account")
    if acc:
        print(f"\n{acc['login']}  {acc['server']}  [{acc['type']}]  "
              f"balance {acc['balance']:,.2f} {acc['currency']}  equity {acc['equity']:,.2f}")
    else:
        print("\nNo account is logged in to the terminal.")

    print(f"open positions: {len(result['open_positions'])}   "
          f"pending orders: {len(result['pending_orders'])}   "
          f"window: {result['window']['from']} to {result['window']['to']}")

    print(trade_stats.render(result["statistics"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
