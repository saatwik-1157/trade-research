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
import contextlib
import ctypes
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import mt5_paper
from mt5_paper import RefuseToTrade


#: Consecutive failed passes before a session gives up. At the default 20s
#: interval this is about three minutes of a terminal not answering -- long
#: enough to ride out a reconnect, short enough not to spin all night.
MAX_CONSECUTIVE_MISSES = 10


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake():
    """Hold the machine awake for as long as the session runs.

    A session told to be flat by 06:00 cannot close anything while the laptop
    is asleep. Measured on this machine 2026-09-07: idle standby at 300 minutes
    on AC and 45 on battery, against a session that needed 514 -- so the
    deadline would have arrived with the process suspended, and the morning's
    log would have ended mid-evening with every position still open and no
    error anywhere to explain it.

    Scoped to the run and restored on the way out, which is why this is not
    `powercfg`: changing the machine's power plan for a trading loop leaves the
    machine changed after the loop, and a laptop that never sleeps again is a
    worse bug than the one being fixed.

    It holds the SYSTEM awake and deliberately not the DISPLAY -- the screen
    should still go dark. It does not defeat closing the lid or an explicit
    sleep, neither of which is an idle timeout, so a lid closed at midnight
    still suspends the session. Windows only; elsewhere it is a no-op and says
    so rather than pretending.
    """
    held = 0
    try:
        held = ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except (AttributeError, OSError):
        held = 0
    if not held:
        print("  note: could not hold the machine awake - if it sleeps before the "
              "deadline, nothing closes and the session simply stops")
    try:
        yield bool(held)
    finally:
        if held:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass


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


def flatten(mt5, live: bool) -> list[dict]:
    """Close every own position, whatever it is worth.

    This BOOKS LOSSES. It is the deliberate end of `--flat-by`: a position that
    never came good is closed at what it is worth, because the instruction is
    to be flat by a time and not to be flat only if that is free. Nothing else
    in this file closes a losing position -- `harvest` has a floor and always
    has -- so this is the one call that can realise a loss on purpose, and it
    is why it is a separate function with its own name.
    """
    return mt5_paper.close_own(mt5, live, where=lambda p: True)


def threshold_at(args, remaining: float) -> float:
    """The profit floor to harvest at, `remaining` seconds before flat-by.

    Constant at `--min-profit` until the last `--relax-over` minutes, then
    decaying linearly to zero. The point is to close each position at the best
    moment it is offered rather than dumping all of them at the deadline: a
    floor that never moves means a position 40 cents up at 05:59 gets flushed
    at whatever it is worth at 06:00 instead.

    It decays to 0 and NOT below. Zero is break-even; going negative would be
    this function deciding how much loss is acceptable, and that is `flat_by`'s
    decision to make once, not a slope's to make continuously.
    """
    window = args.relax_over * 60.0
    if window <= 0 or remaining >= window:
        return args.min_profit
    if remaining <= 0:
        return 0.0
    return args.min_profit * (remaining / window)


def seconds_until(hhmm: str) -> float:
    """Seconds to the next local occurrence of `HH:MM`.

    Local, matching `run_overnight.minutes_until` -- the operator said 6am and
    meant the clock on the wall. NOT `server_now`: the broker's clock is right
    for bounding a history query and wrong for a human deadline, which is the
    distinction CLAUDE.md records about `server_day_start`.
    """
    hour, _, minute = hhmm.partition(":")
    now = datetime.now()
    target = now.replace(hour=int(hour), minute=int(minute or 0),
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run(mt5, args) -> dict:
    deadline = time.monotonic() + args.minutes * 60
    # The wind-down clock. `--flat-by` is a wall-clock hour and `--minutes` is
    # a duration, so they are resolved against each other here rather than
    # assumed equal: a session told to stop at 06:00 and be flat by 05:45 is a
    # sensible thing to ask for, and so is one where they coincide.
    flat_at = deadline if args.flat_by is None else (
        time.monotonic() + seconds_until(args.flat_by))
    harvested, opened, passes, flushed = 0, 0, 0, 0
    halted = False
    # WHICH halt, not just that there was one. A risk halt and a dead
    # terminal call for opposite responses from anything supervising this
    # process: one must not be restarted, the other exists to be. Reported
    # as a value rather than left to be matched out of the printed line.
    halt_kind: str | None = None
    # Consecutive failed passes. A terminal that blinks once should not end a
    # session that has hours left to run; one that is genuinely gone should not
    # be retried all night.
    misses = 0
    start_balance = mt5.account_info().balance

    while time.monotonic() < deadline:
        passes += 1
        stamp = datetime.now().strftime("%H:%M:%S")
        remaining = flat_at - time.monotonic()

        if args.flat_by is not None and remaining <= 0:
            # The deadline. Everything still open is closed at what it is
            # worth, and the count is reported separately from `harvested` --
            # a position that was flushed did not reach its target, and
            # pooling the two would hide exactly that.
            left = flatten(mt5, args.live)
            gone = [r for r in left if r.get("status") in ("CLOSED", "DRY_RUN")]
            flushed += len(gone)
            for r in gone:
                print(f"  [{stamp}] FLAT    {r.get('symbol', '?'):<8} #{r['ticket']}")
            still = len(mt5_paper.own_positions(mt5))
            print(f"  [{stamp}] flat-by reached: closed {len(gone)}, {still} still open",
                  flush=True)
            if not still:
                break
            time.sleep(args.interval)
            continue

        # ONE PASS MUST NOT BE ABLE TO END THE SESSION.
        #
        # There was no exception handling here at all, and the flush that makes
        # the account flat runs AFTER this loop -- so a single transient IPC
        # error at 02:00 killed the process and left every position open at
        # 06:00. The instruction was "be flat by six"; an unguarded `except`-less
        # loop quietly converted that into "be flat by six unless anything at
        # all goes wrong overnight".
        try:
            got = harvest(mt5, threshold_at(args, remaining), args.live)
        except Exception as exc:  # noqa: BLE001 - reported, counted, retried
            misses += 1
            print(f"  [{stamp}] PASS FAILED ({misses}/{MAX_CONSECUTIVE_MISSES}): "
                  f"{type(exc).__name__}: {exc}", flush=True)
            if misses >= MAX_CONSECUTIVE_MISSES:
                print(f"  [{stamp}] giving up after {misses} consecutive failures; "
                      "the terminal is not answering and a flush would fail too",
                      flush=True)
                halted = True
                halt_kind = "terminal"
                break
            time.sleep(args.interval)
            continue
        misses = 0

        closed = [r for r in got if r.get("status") in ("CLOSED", "DRY_RUN")]
        harvested += len(closed)
        for r in closed:
            print(f"  [{stamp}] HARVEST {r.get('symbol', '?'):<8} #{r['ticket']}")

        # Nothing new inside the wind-down window. Opening a trade that the
        # flush will close minutes later pays the spread for no observation.
        winding_down = args.flat_by is not None and remaining <= args.relax_over * 60.0
        if not args.harvest_only and not winding_down:
            try:
                res = mt5_paper.cycle(mt5, args)
            except Exception as exc:  # noqa: BLE001 - an entry that failed is
                # not a reason to stop MANAGING what is already open. Skip the
                # entry, keep harvesting, keep the deadline.
                print(f"  [{stamp}] ENTRY FAILED: {type(exc).__name__}: {exc}",
                      flush=True)
                res = {"halted": False, "actions": []}
            if res["halted"]:
                print(f"  [{stamp}] HALTED: {res['reason']}")
                halted = True
                halt_kind = "risk"
                break
            sent = [a for a in res["actions"] if a.get("status") in ("SENT", "DRY_RUN")]
            opened += len(sent)
            for a in sent:
                print(f"  [{stamp}] OPEN    {a['symbol']:<8} {a['side']:<5} @ {a.get('price')}")

        try:
            bal = mt5.account_info()
            print(f"  [{stamp}] pass {passes}: harvested={harvested} opened={opened} "
                  f"balance={bal.balance:,.2f} equity={bal.equity:,.2f} "
                  f"open={len(mt5_paper.own_positions(mt5))}", flush=True)
        except Exception as exc:  # noqa: BLE001 - a line of log is not worth a session
            print(f"  [{stamp}] pass {passes}: could not read the account "
                  f"({type(exc).__name__}); the session continues", flush=True)

        if time.monotonic() + args.interval >= deadline:
            break
        time.sleep(args.interval)

    # THE FLUSH THAT ACTUALLY FIRES.
    #
    # The in-loop branch above only runs on a pass that starts after `flat_at`,
    # and when --flat-by is the same hour the session stops at there is no such
    # pass: the loop breaks one interval BEFORE the deadline. So a wind-down
    # configured the obvious way -- stop at 06:00, be flat by 06:00 -- would
    # have closed nothing and reported success. Caught before it ran, by asking
    # which pass performs the close rather than by reading the flag.
    #
    # Not after a HALT. `--max-daily-loss` firing at 22:00 is a reason to stop
    # trading, not a reason to close every position hours before the operator
    # asked; the deadline is the deadline.
    if args.flat_by is not None and not halted:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            left = flatten(mt5, args.live)
        except Exception as exc:  # noqa: BLE001 - the one failure that must SHOUT
            print(f"  [{stamp}] THE FLUSH FAILED: {type(exc).__name__}: {exc}")
            print(f"  [{stamp}] POSITIONS ARE STILL OPEN AT THE VENUE. "
                  "Close them by hand or start a --harvest-only session.",
                  flush=True)
            left = []
        gone = [r for r in left if r.get("status") in ("CLOSED", "DRY_RUN")]
        flushed += len(gone)
        for r in gone:
            print(f"  [{stamp}] FLAT    {r.get('symbol', '?'):<8} #{r['ticket']}")
        print(f"  [{stamp}] flat-by {args.flat_by}: closed {len(gone)} at the deadline",
              flush=True)

    # The SUMMARY must not be able to kill the session either. This block read
    # the terminal twice, unguarded, so a session that gave up cleanly on a dead
    # terminal still died with a traceback on its way out and reported nothing
    # about what it had done. Found by the test written for the loop guard,
    # which is the argument for writing the test.
    summary = {"passes": passes, "harvested": harvested, "opened": opened,
               "flushed": flushed, "halted": halted, "halt_kind": halt_kind,
               "start_balance": start_balance,
               "end_balance": None, "realised": None, "still_open": None}
    try:
        end = mt5.account_info()
        summary["end_balance"] = end.balance
        summary["realised"] = round(end.balance - start_balance, 2)
    except Exception as exc:  # noqa: BLE001
        print(f"  could not read the closing balance ({type(exc).__name__})")
    try:
        summary["still_open"] = len(mt5_paper.own_positions(mt5))
    except Exception as exc:  # noqa: BLE001
        # NOT zero. "We could not ask" and "nothing is open" are the distinction
        # this whole repository is built on.
        print(f"  could not count open positions ({type(exc).__name__}); "
              "treat the account as UNKNOWN, not flat")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-profit", type=float, default=0.01,
                    help="close a position once net floating P&L reaches this (account currency)")
    ap.add_argument("--minutes", type=float, default=30.0, help="how long to run")
    ap.add_argument("--interval", type=int, default=20, help="seconds between passes")
    ap.add_argument("--harvest-only", action="store_true",
                    help="close winners but open nothing new")
    ap.add_argument("--flat-by", default=None, metavar="HH:MM",
                    help="local time to hold NO position past. Everything still open "
                         "then is closed at what it is worth, losses included; nothing "
                         "new is opened inside the --relax-over window before it")
    ap.add_argument("--relax-over", type=float, default=0.0, metavar="MINUTES",
                    help="decay --min-profit linearly to zero over the final MINUTES "
                         "before --flat-by, so positions close at the best moment "
                         "offered rather than all at the deadline")
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
    sizing = (
        "risk %.2f %s per trade off the stop distance" % (args.risk_usd, acct["currency"])
        if args.risk_usd
        else "fixed %g lots" % args.lot
    )
    print(f"  size: {sizing}")
    print(f"  mode: {'LIVE ORDERS (demo account)' if args.live else 'DRY RUN - no orders sent'}")
    print(f"  running {args.minutes:g} minutes, one pass every {args.interval}s")
    if args.flat_by:
        mins = seconds_until(args.flat_by) / 60.0
        print(f"  FLAT BY {args.flat_by} local, in {mins:.0f} minutes -- everything "
              f"still open then is closed at what it is worth, LOSSES INCLUDED")
        if args.relax_over:
            print(f"  the {args.min_profit} floor decays to 0 over the final "
                  f"{args.relax_over:g} minutes, and nothing new opens inside it")
    print()

    try:
        with keep_awake() as awake:
            if awake and args.flat_by:
                print("  holding the machine awake until the session ends "
                      "(the display may still sleep)\n")
            out = run(mt5, args)
    finally:
        mt5.shutdown()

    print(f"\n  passes {out['passes']}   harvested {out['harvested']}   "
          f"opened {out['opened']}   flushed {out['flushed']}   "
          f"still open {'UNKNOWN' if out['still_open'] is None else out['still_open']}")

    if args.flat_by and out["still_open"] is None:
        # UNKNOWN is the LOUDEST case, not the quietest. `if out["still_open"]`
        # treated None as falsy and skipped this warning exactly when nobody
        # knew whether the account was flat -- the same "missing is never safe"
        # rule `portfolio.decision` states, broken in a print statement.
        print(f"  WARNING: --flat-by {args.flat_by} finished and the account could "
              "NOT be read. Whether anything is still open is UNKNOWN; check the "
              "terminal before assuming it is flat")
    elif out["still_open"] and args.flat_by:
        # Said plainly rather than left to be read off a count. A wind-down
        # that did not finish is the one outcome of this mode that matters.
        print(f"  WARNING: --flat-by {args.flat_by} did not leave the account flat; "
              f"{out['still_open']} position(s) are still open at the venue")

    if out["end_balance"] is None:
        print(f"  balance {out['start_balance']:,.2f} -> UNKNOWN "
              "(the closing balance could not be read)")
    else:
        print(f"  balance {out['start_balance']:,.2f} -> {out['end_balance']:,.2f}  "
              f"({out['realised']:+.2f})")
    print("\n  A harvest count is not a win rate and this balance is not an edge.")
    print("  Read the R-multiple: python tools/track_record.py --merge\n")

    # A halt is not a clean finish, and the two halts are not each other.
    # 2 is the risk limit: whatever supervises this must NOT start another
    # session, because restarting through a daily loss limit is how a limit
    # becomes a speed bump. 3 is a terminal that stopped answering, which is
    # exactly what a supervisor is for. This DOES change the dated session:
    # a run that hit its daily loss limit used to exit 0 like any other, and
    # now exits 2. That is the point -- 'the session ended' and 'the session
    # was stopped by a risk limit' were the same answer, and the launcher
    # printed the same line for both.
    if out["halt_kind"] == "risk":
        return 2
    if out["halt_kind"] == "terminal":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
