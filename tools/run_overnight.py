#!/usr/bin/env python
"""Start the harvest session and stop it at a wall-clock hour.

DEMO ACCOUNTS ONLY - the fence is mt5_paper.assert_demo, reached through
take_profit.

This exists so the run can be started with one short command instead of a
twelve-argument line retyped at midnight, and so the settings below are
recorded in a file that can be read and argued with rather than in shell
history. It computes the minutes to the next occurrence of --until-hour on
the LOCAL clock and hands them to take_profit.

What the settings are, and why:

  rule=random        The rule that has a signal on every free symbol every
                     pass, which is what "close one and open another" needs.
                     It is also the honest choice: rule_search.py found no
                     entry rule that beats a permutation null out of sample,
                     so a named rule here would imply a selection that has
                     not been demonstrated.
  risk-usd 5         Constant risk per trade, sized off each symbol's stop
                     distance by mt5_paper.lot_for_risk. It equalises the
                     bet across symbols; it does not improve expectancy, and
                     cannot - volume is a positive multiplier on the result
                     and leaves the sign alone.
  min-profit 0.50    Harvest at 0.1R. Harvesting at a cent is what produced
                     the 92% win rate against a 0.17 payoff ratio already in
                     the ledger: a win rate manufactured by bracket geometry
                     is not a measurement of anything.
  max-daily-loss 200 About 40 stops at this risk. A fence, not a forecast.

The balance line this prints at the end is not a result. Read the
R-multiple and the date-clustered t:

    python tools/track_record.py --merge

A session refuses to start while another one is running, and everything it
prints is copied to logs/overnight-<timestamp>.log. Both exist because the
Desktop launcher takes no arguments and leaves no record: double-clicking it
twice puts two harvest loops on one account, competing for the same seven
position slots and doubling the risk per pass, and the only account of what a
session did lived in a console window that closes with it.

Every session ends FLAT. `--flat-by` is passed for the same hour the session
stops at, so whatever is still open then is closed at what it is worth, losses
included. That is deliberate and it is the harvest loop's own doing: closing at
the first sign of profit books winners and holds losers, so the positions still
open at the deadline ARE the losing tail. Leaving them meant carrying them into
the weekend, into the next session's position count and into another night of
swap. The profit floor decays to zero over the final `--relax-over` minutes so
each one closes at the best moment it is offered rather than all at 06:00, and
nothing new is opened inside that window.

Usage:
    python tools/run_overnight.py                  # run until 06:00, live
    python tools/run_overnight.py --harvest-only   # wind down: close, open nothing
    python tools/run_overnight.py --until-hour 9   # run until 09:00
    python tools/run_overnight.py --relax-over 90  # start easing the floor earlier
    python tools/run_overnight.py --dry-run        # show the command only
    python tools/run_overnight.py --paper          # no orders sent
    python tools/run_overnight.py --force          # a second session, meant
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Imported for the rule NAMES only, so `--rule` refuses a typo at the command
# line instead of at the first pass. `mt5_paper` imports MetaTrader5 inside
# `connect()` rather than at module scope, so this costs no terminal.
import mt5_paper  # noqa: E402

SETTINGS = [
    "--rule", "random",
    "--symbols", "EURUSD,GBPUSD,USDJPY,USDCAD,AUDUSD,USDCHF,NZDUSD",
    "--risk-usd", "5",
    "--sl-atr", "1.5",
    "--tp-atr", "1.5",
    "--min-profit", "0.50",
    "--max-positions", "7",
    "--max-daily-loss", "200",
    "--interval", "20",
]

SESSION_SCRIPTS = ("run_overnight.py", "take_profit.py")


def override(argv: list[str], flag: str, value: str | None) -> list[str]:
    """Replace one flag's value in a settings list, or leave it alone.

    The settings above are the session's defaults and every figure in the live
    record was taken under them, so they are not edited casually -- but a
    default nobody can override from the command line is a default somebody
    eventually edits in the file, and then the record silently describes two
    different configurations under one name.

    Refuses a flag that is not already present rather than appending it. An
    override is a change to a stated default; appending an unknown flag would
    be adding a setting under the guise of changing one.
    """
    if value is None:
        return argv
    if flag not in argv:
        raise SystemExit(f"{flag} is not one of this session's settings, so it cannot be overridden")
    out = list(argv)
    out[out.index(flag) + 1] = value
    return out


class _Tee:
    """Write to the console and to the log file at once.

    Flushed on every write. A session that is killed - which is how most of
    them end, by closing the window - never reaches a clean shutdown, and a
    buffered log of a killed session is an empty one.
    """

    def __init__(self, stream, handle):
        self._stream = stream
        self._handle = handle

    def write(self, text):
        self._stream.write(text)
        self._handle.write(text)
        self._handle.flush()
        return len(text)

    def flush(self):
        self._stream.flush()
        self._handle.flush()

    def isatty(self):
        return self._stream.isatty()

    def reconfigure(self, **kwargs):
        """Absorb take_profit's UTF-8 reconfigure at import.

        The real stream is already set that way in start_logging and the log
        file is opened as UTF-8, so there is nothing left to do here.
        """
        return None

    def __getattr__(self, name):
        return getattr(self._stream, name)


def other_sessions():
    """Order-sending sessions other than this process, as (pid, started, cmd).

    Matched on what each process is running rather than on a lock file,
    because a lock file outlives the process that wrote it: a killed session
    leaves one behind and every later run then refuses on behalf of a session
    that ended hours ago. Of the two failures a stale refusal is the worse,
    since clearing it means deleting a file nobody documented.

    A --paper or --dry-run process is not a session. It sends no orders and
    holds no position slot, so it is not something to refuse for.
    """
    try:
        import psutil
    except ImportError:
        print("  note: psutil not installed, cannot check for a running "
              "session - proceeding unguarded")
        return []

    me = os.getpid()
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            if info["pid"] == me or not info["cmdline"]:
                continue
            if not (info["name"] or "").lower().startswith("python"):
                continue
            cmdline = " ".join(info["cmdline"])
            if not any(s in cmdline for s in SESSION_SCRIPTS):
                continue
            if "--paper" in cmdline or "--dry-run" in cmdline:
                continue
            found.append((info["pid"],
                          datetime.fromtimestamp(info["create_time"]),
                          cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(found)


def start_logging():
    """Copy everything printed to logs/overnight-<timestamp>.log.

    Returns (path, restore). The launcher shows its output in a console
    window and that window closes with the session, so until now the only
    record a run left was MetaTrader's. That record is the trades; this one
    is the refusals, the skipped symbols and the reason the loop stopped,
    none of which reach the ledger.

    The file matches the *.log rule already in .gitignore, which it needs to:
    a session log names the account and its balance.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir,
                        f"overnight-{datetime.now():%Y%m%d-%H%M%S}.log")

    handle = open(path, "a", encoding="utf-8", errors="replace")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(saved_out, handle)
    sys.stderr = _Tee(saved_err, handle)

    def restore():
        sys.stdout, sys.stderr = saved_out, saved_err
        handle.close()

    handle.write(f"# trade-research overnight session, started "
                 f"{datetime.now():%Y-%m-%d %H:%M:%S}\n")
    return path, restore


def minutes_until(hour: int) -> tuple[float, datetime]:
    """Minutes from now to the next occurrence of `hour`:00 on the local clock.

    Rolls to tomorrow when the hour has already passed today, which is the
    normal case for an overnight run started in the evening.
    """
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() / 60.0, target


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--until-hour", type=int, default=6,
                    help="local hour to stop at (default 6, i.e. 06:00)")
    ap.add_argument("--rule", default=None, choices=sorted(mt5_paper.RULES),
                    help="override the session's entry rule. NONE of them has a measured "
                         "edge; see CLAUDE.md for what each was worth per trade")
    ap.add_argument("--max-positions", default=None, metavar="N",
                    help="override how many positions may be open at once. Fewer means "
                         "less spread paid, and frequency is the one lever with a "
                         "measured sign")
    ap.add_argument("--relax-over", type=float, default=45.0, metavar="MINUTES",
                    help="minutes before the stop hour over which the profit floor "
                         "decays to zero, and inside which nothing new is opened "
                         "(default 45; 0 turns the ramp off and flushes at the hour)")
    ap.add_argument("--harvest-only", action="store_true",
                    help="close positions but open NOTHING new, for winding an "
                         "existing session down")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved command and exit without connecting")
    ap.add_argument("--paper", action="store_true",
                    help="run without sending orders")
    ap.add_argument("--force", action="store_true",
                    help="start even though another session is running")
    args = ap.parse_args()

    if not 0 <= args.until_hour <= 23:
        print(f"  --until-hour must be 0-23, got {args.until_hour}")
        return 1

    if not (args.dry_run or args.paper or args.force):
        running = other_sessions()
        if running:
            print("\n  REFUSED: a session is already running.\n")
            for pid, started, cmdline in running:
                print(f"    pid {pid}  started {started:%H:%M:%S}  {cmdline}")
            print("\n  Two harvest loops on one account compete for the same")
            print("  position slots and double the risk per pass. Stop that")
            print("  session first, or pass --force if a second one is what")
            print("  you meant.\n")
            return 1

    minutes, target = minutes_until(args.until_hour)
    argv = override(SETTINGS, "--rule", args.rule)
    argv = override(argv, "--max-positions", args.max_positions)
    argv = argv + ["--minutes", f"{minutes:.0f}"]
    # Flat by the same hour the session stops at. A session that stops while
    # holding positions leaves them to the weekend, the next session's
    # `max-positions` count and the swap -- which is what happened on
    # 2026-09-07, when 7 positions were still open at -9.25 float when the
    # loop ended. Being flat is the default here BECAUSE the loop's own
    # harvest floor guarantees the opposite: it books winners and holds
    # losers, so whatever is left at the deadline is the losing tail.
    argv += ["--flat-by", f"{args.until_hour:02d}:00",
             "--relax-over", f"{args.relax_over:g}"]
    if args.harvest_only:
        argv.append("--harvest-only")
    if not args.paper:
        argv.append("--live")

    log_path, restore = (None, None) if args.dry_run else start_logging()

    try:
        print(f"\n  now {datetime.now():%H:%M} -> stop {target:%H:%M} "
              f"({minutes:.0f} minutes)")
        print("  take_profit.py " + " ".join(argv) + "\n")
        if log_path:
            print(f"  log {log_path}\n")

        if args.dry_run:
            return 0

        import take_profit
        sys.argv = ["take_profit.py"] + argv
        return take_profit.main()
    finally:
        if log_path:
            print(f"\n  session log: {log_path}")
        if restore is not None:
            restore()


if __name__ == "__main__":
    raise SystemExit(main())
