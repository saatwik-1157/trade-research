"""Automated trading on a MetaTrader 5 DEMO account.

This is the one module in the project that sends orders. It is separated from
everything else deliberately: `mt5_account.py` reads and can never write, and
this file writes and is fenced so it can only ever reach play money.

The fence, in order of execution:

1. `account_info().trade_mode` must be DEMO. A real or contest account aborts
   before a single order is constructed. This is a check in code, not a rule in
   a prompt, because a prompt cannot fail closed.
2. Orders are dry-run by default. `--live` is required to actually send, and
   even then only after the demo check has passed.
3. Every order carries MAGIC so this tool can identify its own positions and
   will never modify or close one a human opened.
4. Hard caps: lot size, concurrent positions, and a daily loss limit that stops
   trading for the session when breached.

What to expect from it
----------------------
It will lose money on plenty of trades, and roughly break even before costs and
lose after them. That is not a bug to tune away. This repository measured the
composite score at an information coefficient of 0.002 and found zero of 105
tested patterns surviving correction, so none of the rules below has a measured
edge, and the `random` rule exists to make that concrete: if a "real" strategy
cannot separate itself from coin-flipping over a few hundred trades, that is the
finding.

Tuning a rule until the demo looks profitable is the failure mode this whole
project was built to catch. Demo profits justify live money, and the edge that
was never there stops being hypothetical at that point.

Usage:
    python tools/mt5_paper.py --once                       # dry run, no orders
    python tools/mt5_paper.py --rule sma_cross --live --once
    python tools/mt5_paper.py --rule random --live --interval 300
    python tools/mt5_paper.py --close-all --live           # flatten its own positions
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import numpy as np  # noqa: E402

DEFAULT_TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
MAGIC = 770315  # tags orders from this tool so it never touches anything else
TRADE_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "paper_trades.jsonl"
)


class RefuseToTrade(RuntimeError):
    """Raised when the safety fence blocks execution."""


def connect(path: str | None = None):
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:  # optional, Windows-only: keep the rest usable without it
        raise RefuseToTrade(
            "MetaTrader5 is not installed. It is an optional, Windows-only "
            "dependency: pip install MetaTrader5"
        ) from exc

    if not mt5.initialize(path=path or DEFAULT_TERMINAL, timeout=60000):
        if not mt5.initialize(timeout=60000):
            raise RefuseToTrade(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


def assert_demo(mt5, live: bool = False) -> dict:
    """Abort unless this is unambiguously a demo account.

    trade_mode: 0 DEMO, 1 CONTEST, 2 REAL. Anything that is not 0 - including a
    value this build does not recognise - refuses. Failing closed on the unknown
    case is the whole point of a fence.

    The terminal's algo-trading switch is only required for `live`, because a
    dry run sends nothing. Demanding it earlier would push someone to enable
    order sending just to preview signals, which is precisely backwards.
    """
    ai = mt5.account_info()
    if ai is None:
        raise RefuseToTrade("No account is logged in to the terminal.")
    if ai.trade_mode != 0:
        kind = {1: "CONTEST", 2: "REAL"}.get(ai.trade_mode, f"UNKNOWN({ai.trade_mode})")
        raise RefuseToTrade(
            f"Account {ai.login} on {ai.server} is a {kind} account. "
            "This tool only runs on DEMO accounts and will not place an order here."
        )
    if live and not getattr(mt5.terminal_info(), "trade_allowed", False):
        raise RefuseToTrade(
            "Algorithmic trading is disabled in the terminal. Enable it at "
            "Tools -> Options -> Expert Advisors -> Allow Algorithmic Trading."
        )
    return {
        "login": ai.login, "server": ai.server,
        "currency": getattr(ai, "currency", "?"),
        "balance": getattr(ai, "balance", 0.0),
        "equity": getattr(ai, "equity", 0.0),
        "mode": "DEMO",
    }


# ------------------------------------------------------------------ rules
# Each returns "buy", "sell" or None from the closed bars only. Signals never
# read the forming bar, which would be lookahead against live prices.

def rule_sma_cross(rates) -> str | None:
    c = rates["close"][:-1]
    if len(c) < 60:
        return None
    fast_now, slow_now = c[-20:].mean(), c[-50:].mean()
    fast_prev, slow_prev = c[-21:-1].mean(), c[-51:-1].mean()
    if fast_prev <= slow_prev and fast_now > slow_now:
        return "buy"
    if fast_prev >= slow_prev and fast_now < slow_now:
        return "sell"
    return None


def rule_rsi_reversion(rates, n: int = 14) -> str | None:
    c = rates["close"][:-1]
    if len(c) < 3 * n:
        return None
    d = np.diff(c)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag, al = gain[:n].mean(), loss[:n].mean()
    for g, l in zip(gain[n:], loss[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return None
    rsi = 100 - 100 / (1 + ag / al)
    if rsi < 30:
        return "buy"
    if rsi > 70:
        return "sell"
    return None


def rule_random(rates) -> str | None:
    """Coin flip. The benchmark every other rule has to beat to mean anything."""
    return random.choice(["buy", "sell", None, None])


RULES = {"sma_cross": rule_sma_cross, "rsi_reversion": rule_rsi_reversion, "random": rule_random}


def atr_from(rates, n: int = 14) -> float | None:
    h, l, c = rates["high"][:-1], rates["low"][:-1], rates["close"][:-1]
    if len(c) < n + 2:
        return None
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    return float(tr[-n:].mean())


def _log(record: dict) -> None:
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def own_positions(mt5):
    return [p for p in (mt5.positions_get() or []) if p.magic == MAGIC]


def realised_today(mt5) -> float:
    """P&L booked by this tool today, used for the daily loss limit."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(start, datetime.now()) or []
    return sum(d.profit + d.commission + d.swap for d in deals if d.magic == MAGIC)


def place(mt5, symbol: str, side: str, lot: float, sl_atr: float, tp_atr: float, live: bool) -> dict:
    info = mt5.symbol_info(symbol)
    if info is None and not mt5.symbol_select(symbol, True):
        return {"symbol": symbol, "status": "symbol_unavailable"}
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not tick or tick.ask <= 0 or tick.bid <= 0:
        return {"symbol": symbol, "status": "no_quote_market_probably_closed"}

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
    atr = atr_from(rates) if rates is not None and len(rates) > 40 else None
    if not atr:
        return {"symbol": symbol, "status": "insufficient_history_for_atr"}

    is_buy = side == "buy"
    price = tick.ask if is_buy else tick.bid
    sl = price - sl_atr * atr if is_buy else price + sl_atr * atr
    tp = price + tp_atr * atr if is_buy else price - tp_atr * atr

    filling = filling_for(mt5, info)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": round(sl, info.digits),
        "tp": round(tp, info.digits),
        "deviation": 20,
        "magic": MAGIC,
        "comment": "trade-research demo",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    if not live:
        return {"symbol": symbol, "side": side, "status": "DRY_RUN",
                "price": price, "sl": request["sl"], "tp": request["tp"], "lot": lot}

    res = mt5.order_send(request)
    out = {
        "symbol": symbol, "side": side, "lot": lot, "price": price,
        "sl": request["sl"], "tp": request["tp"],
        "retcode": getattr(res, "retcode", None),
        "comment": getattr(res, "comment", None),
        "order": getattr(res, "order", None),
        "status": "SENT" if getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE else "REJECTED",
    }
    _log({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": "order", **out})
    return out



def filling_for(mt5, info):
    """Pick an order filling mode the symbol actually accepts.

    symbol_info().filling_mode is a bitmask over SYMBOL_FILLING_* (FOK=1,
    IOC=2). The ORDER_FILLING_* request constants are a different enumeration
    (FOK=0, IOC=1, RETURN=2), so the bitmask bits must be translated, not used
    directly - conflating them sends IOC to a FOK-only symbol and the server
    rejects it with retcode 10030.
    """
    mask = getattr(info, "filling_mode", 0) or 0
    if mask & 1:
        return mt5.ORDER_FILLING_FOK
    if mask & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def close_own(mt5, live: bool) -> list[dict]:
    out = []
    for p in own_positions(mt5):
        tick = mt5.symbol_info_tick(p.symbol)
        if not tick:
            out.append({"ticket": p.ticket, "status": "no_quote"})
            continue
        is_long = p.type == 0
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "position": p.ticket,
            "price": tick.bid if is_long else tick.ask,
            "deviation": 20,
            "magic": MAGIC,
            "comment": "trade-research close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_for(mt5, mt5.symbol_info(p.symbol)),
        }
        if not live:
            out.append({"ticket": p.ticket, "symbol": p.symbol, "status": "DRY_RUN"})
            continue
        res = mt5.order_send(req)
        rec = {"ticket": p.ticket, "symbol": p.symbol,
               "retcode": getattr(res, "retcode", None),
               "status": "CLOSED" if getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE else "FAILED"}
        _log({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": "close", **rec})
        out.append(rec)
    return out


def cycle(mt5, args) -> dict:
    open_now = own_positions(mt5)
    pnl_today = realised_today(mt5)

    if pnl_today <= -abs(args.max_daily_loss):
        return {"halted": True, "reason": f"daily loss limit hit ({pnl_today:.2f})",
                "realised_today": round(pnl_today, 2)}

    actions = []
    for symbol in args.symbols:
        if len(open_now) + len([a for a in actions if a.get("status") in ("SENT", "DRY_RUN")]) >= args.max_positions:
            actions.append({"symbol": symbol, "status": "skipped_max_positions"})
            continue
        if any(p.symbol == symbol for p in open_now):
            actions.append({"symbol": symbol, "status": "skipped_already_open"})
            continue
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
        if rates is None or len(rates) < 60:
            actions.append({"symbol": symbol, "status": "no_history"})
            continue
        side = RULES[args.rule](rates)
        if side is None:
            actions.append({"symbol": symbol, "status": "no_signal"})
            continue
        actions.append(place(mt5, symbol, side, args.lot, args.sl_atr, args.tp_atr, args.live))

    return {"halted": False, "realised_today": round(pnl_today, 2),
            "open_positions": len(open_now), "actions": actions}


def main() -> int:
    ap = argparse.ArgumentParser(description="Automated trading on a MetaTrader 5 DEMO account.")
    ap.add_argument("--rule", choices=sorted(RULES), default="sma_cross")
    ap.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--sl-atr", type=float, default=1.5, help="stop loss in ATR multiples")
    ap.add_argument("--tp-atr", type=float, default=1.5, help="take profit in ATR multiples")
    ap.add_argument("--max-positions", type=int, default=3)
    ap.add_argument("--max-daily-loss", type=float, default=500.0)
    ap.add_argument("--interval", type=int, default=0, help="seconds between cycles; 0 runs once")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--live", action="store_true", help="actually send orders (demo only)")
    ap.add_argument("--close-all", action="store_true", help="close positions this tool opened")
    ap.add_argument("--path", default=None)
    args = ap.parse_args()
    args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        mt5 = connect(args.path)
        acct = assert_demo(mt5, live=args.live)
    except RefuseToTrade as exc:
        print(f"\n  REFUSED: {exc}\n")
        return 1

    print(f"\n  account {acct['login']} @ {acct['server']}  [{acct['mode']}]  "
          f"balance {acct['balance']:,.2f} {acct['currency']}")
    print(f"  rule={args.rule}  symbols={','.join(args.symbols)}  lot={args.lot}  "
          f"sl={args.sl_atr}xATR  tp={args.tp_atr}xATR")
    print(f"  mode: {'LIVE ORDERS (demo account)' if args.live else 'DRY RUN - no orders sent'}\n")

    try:
        if args.close_all:
            for r in close_own(mt5, args.live):
                print(f"  close {r['symbol']} #{r['ticket']}: {r['status']}")
            return 0

        while True:
            res = cycle(mt5, args)
            stamp = datetime.now().strftime("%H:%M:%S")
            if res["halted"]:
                print(f"  [{stamp}] HALTED: {res['reason']}")
                break
            print(f"  [{stamp}] open={res['open_positions']} "
                  f"realised_today={res['realised_today']:.2f}")
            for a in res["actions"]:
                detail = f"{a.get('side','')} @ {a.get('price','')}" if a.get("side") else ""
                print(f"      {a['symbol']:<10}{a['status']:<32}{detail}")
            if args.once or not args.interval:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  stopped by user")
    finally:
        mt5.shutdown()

    print(f"\n  order log: {TRADE_LOG}")
    print("  Analyse results with: python tools/mt5_account.py --days 7\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
