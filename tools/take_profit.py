#!/usr/bin/env python
"""Harvest positions as soon as they show a floating profit, and keep trading.

DEMO ACCOUNTS ONLY - the same fence as mt5_paper, enforced by assert_demo.

What this does, stated plainly, because the win rate it produces is the most
misleading number this repository can generate:

Closing at the first sign of profit caps every winner at roughly the threshold
while leaving every loser at its full stop distance. That is the 3.0xATR /
0.5xATR bracket taken to its limit - a reward:risk of about 0.01 rather than
0.17 - and it manufactures a win rate above 90% by construction. The balance
rises for as long as no stop is hit and gives it back when one is. Expectancy
is the spread, paid on every round trip, and it is negative.

So the harvest count is not a result. The figure to read is the R-multiple in
`track_record.py`, which divides each outcome by the money that was actually at
risk: a harvested win is worth about +0.01R and a stop is -1.00R, and no number
of the former pays for one of the latter. Every order and close is written to
the same log mt5_paper uses, so the record accumulates whether or not anyone
looks at it.

Usage:
    python tools/take_profit.py --minutes 60 --live
    python tools/take_profit.py --min-profit 0.05 --minutes 30 --live
    python tools/take_profit.py --harvest-only --live      # no new entries
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import mt5_paper
from mt5_paper import RefuseToTrade


def net_floating(position) -> float:
    """Floating P&L including carry.

    `profit` excludes swap, and a position held overnight can read positive on
    `profit` while being negative once financing is charged. Harvesting on the
    gross figure would book those as wins.
    """
    return float(position.profit) + float(getattr(position, "swap", 0.0) or 0.0)


def harvest(mt5, min_profit: float, live: bool) -> list[dict]:
    """Close every own position whose net floating P&L has reached min_profit."""
    return mt5_paper.close_own(mt5, live, where=lambda p: net_floating(p) >= min_profit)


def run(mt5, args) -> dict:
    deadline = time.monotonic() + args.minutes * 60
    harvested, opened, passes = 0, 0, 0
    start_balance = mt5.account_info().balance

    while time.monotonic() < deadline:
        passes += 1
        stamp = datetime.now().strftime("%H:%M:%S")

        got = harvest(mt5, args.min_profit, args.live)
        closed = [r for r in got if r.get("status") in ("CLOSED", "DRY_RUN")]
        harvested += len(closed)
        for r in closed:
            print(f"  [{stamp}] HARVEST {r.get('symbol', '?'):<8} #{r['ticket']}")

        if not args.harvest_only:
            res = mt5_paper.cycle(mt5, args)
            if res["halted"]:
                print(f"  [{stamp}] HALTED: {res['reason']}")
                break
            sent = [a for a in res["actions"] if a.get("status") in ("SENT", "DRY_RUN")]
            opened += len(sent)
            for a in sent:
                print(f"  [{stamp}] OPEN    {a['symbol']:<8} {a['side']:<5} @ {a.get('price')}")

        bal = mt5.account_info()
        print(f"  [{stamp}] pass {passes}: harvested={harvested} opened={opened} "
              f"balance={bal.balance:,.2f} equity={bal.equity:,.2f} "
              f"open={len(mt5_paper.own_positions(mt5))}", flush=True)

        if time.monotonic() + args.interval >= deadline:
            break
        time.sleep(args.interval)

    end = mt5.account_info()
    return {"passes": passes, "harvested": harvested, "opened": opened,
            "start_balance": start_balance, "end_balance": end.balance,
            "realised": round(end.balance - start_balance, 2),
            "still_open": len(mt5_paper.own_positions(mt5))}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-profit", type=float, default=0.01,
                    help="close a position once net floating P&L reaches this (account currency)")
    ap.add_argument("--minutes", type=float, default=30.0, help="how long to run")
    ap.add_argument("--interval", type=int, default=20, help="seconds between passes")
    ap.add_argument("--harvest-only", action="store_true",
                    help="close winners but open nothing new")
    ap.add_argument("--rule", choices=sorted(mt5_paper.RULES), default="random")
    ap.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY,USDCAD,AUDUSD,USDCHF,NZDUSD")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--risk-usd", type=float, default=None,
                    help="size each trade so its stop costs this much; "
                         "overrides --lot. Does not change expectancy.")
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--tp-atr", type=float, default=1.5)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--max-daily-loss", type=float, default=50.0)
    ap.add_argument("--live", action="store_true", help="actually send orders (demo only)")
    ap.add_argument("--path", default=None)
    args = ap.parse_args()
    args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        mt5 = mt5_paper.connect(args.path)
        acct = mt5_paper.assert_demo(mt5, live=args.live)
    except RefuseToTrade as exc:
        print(f"\n  REFUSED: {exc}\n")
        return 1

    print(f"\n  account {acct['login']} @ {acct['server']}  [{acct['mode']}]  "
          f"balance {acct['balance']:,.2f} {acct['currency']}")
    print(f"  harvest at >= {args.min_profit} {acct['currency']}  "
          f"entries={'off' if args.harvest_only else args.rule}  "
          f"sl={args.sl_atr}xATR tp={args.tp_atr}xATR")
    print(f"  size: {'risk %.2f %s per trade off the stop distance' % (args.risk_usd, acct['currency'])
                     if args.risk_usd else 'fixed %g lots' % args.lot}")
    print(f"  mode: {'LIVE ORDERS (demo account)' if args.live else 'DRY RUN - no orders sent'}")
    print(f"  running {args.minutes:g} minutes, one pass every {args.interval}s\n")

    try:
        out = run(mt5, args)
    finally:
        mt5.shutdown()

    print(f"\n  passes {out['passes']}   harvested {out['harvested']}   "
          f"opened {out['opened']}   still open {out['still_open']}")
    print(f"  balance {out['start_balance']:,.2f} -> {out['end_balance']:,.2f}  "
          f"({out['realised']:+.2f})")
    print("\n  A harvest count is not a win rate and this balance is not an edge.")
    print("  Read the R-multiple: python tools/track_record.py --merge\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
