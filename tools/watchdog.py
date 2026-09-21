"""Restart a session that died, without losing its deadline. **P7.**

Eight sessions in a row were terminated from outside and nobody was awake to
notice. `crash_report` now records that it happened; this restarts it.

    python tools/watchdog.py --until-hour 6

**It is not `--continuous`.** That mode already runs legs back to back, and it
does so by giving up the stop hour and the wind-down -- the next leg adopts
whatever is open and nothing is ever force-closed. That is the right shape for
a run with no deadline and the wrong shape for an overnight one, because the
positions carried across legs ARE the losing tail. This keeps one fixed
deadline and hands each restart the REMAINING minutes, so a session that dies
at 02:00 is replaced by one that still stops and flushes at 06:00.

## What it refuses to do

A watchdog that restarts anything is worse than none, so it refuses more often
than it acts:

* **A risk halt is never restarted.** `--max-daily-loss` firing is the system
  working. Relaunching past it is how an automated loss limit becomes an
  automated loss.
* **The kill switch is never overridden.** If `STOP` exists the watchdog exits
  rather than fighting the operator.
* **Restarts are bounded and the budget does not refill.** Five in one night,
  then it stops and says so. A worker that keeps dying is a fault to be looked
  at, not retried.
* **A session that ends within seconds did not trade, it failed to start** --
  usually the terminal's Algo Trading toggle. Those count double against the
  budget, because retrying them is pure spin.
* **It never places an order itself.** It has no venue, no credentials and no
  trading code; it starts and watches a process that does.

## What it cannot do

It cannot restart what the machine cannot run. If the terminal is gone, the
laptop suspends, or Windows kills the whole process tree, the watchdog dies
with everything else -- and then `CRASH_REPORTS/` is what tells you, which is
the arrangement that already exists.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import crash_report
import kill_switch
import paths as _paths

#: A session that ends faster than this did not trade; it failed to start.
FAST_EXIT_SECONDS = 90.0
#: Total restarts allowed in one watch. Does not refill.
MAX_RESTARTS = 5
#: A fast exit costs this much of the budget, because retrying it is spin.
FAST_EXIT_COST = 2
#: Between noticing a death and starting the replacement. Long enough for a
#: terminal that is restarting, short enough that nothing sits unmanaged for
#: meaningfully longer than one harvest interval.
RESTART_DELAY = 15.0
#: How often to look. The session ticks every 20s; there is nothing to gain
#: from looking faster than it moves.
POLL_SECONDS = 20.0


def _root() -> str:
    return _paths.project_root()


def session_alive() -> bool:
    """Whether any order-sending session is running on this machine.

    A process scan rather than a PID from the record: the record's PID is the
    process the watchdog started, and an operator who started one by hand
    should not get a second one started beside it.
    """
    try:
        import psutil
    except ImportError:
        # Without psutil this cannot be answered, and a watchdog that guesses
        # "no session" would start a second one on the same account. Refuse.
        raise SystemExit(
            "  psutil is not installed, so a running session cannot be seen.\n"
            "  Starting one blind would put two harvest loops on one account.\n"
            "      pip install psutil"
        ) from None

    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.info["pid"] == me:
            continue
        if _is_session(" ".join(proc.info.get("cmdline") or [])):
            return True
    return False


#: Subcommands of the frozen executable that send orders. `paper` is absent on
#: purpose: `paper --close-all` is a flush, not a session, and treating it as
#: one would stop the watchdog from replacing a session that had really died.
TRADING_SUBCOMMANDS = ("overnight", "harvest")


def _is_session(cmdline: str) -> bool:
    """Whether this command line is an order-sending session.

    Matching only `take_profit.py` / `run_overnight.py` was right from a
    checkout and blind to the frozen executable, where those file names never
    appear -- the session is `trade-research.exe overnight --until-hour 6`.
    Blind in the worse direction, too: `session_alive()` answered False while a
    session WAS running, so the watchdog would have started a second harvest
    loop on the same account, which is the one thing this module says it exists
    to prevent. Observed 2026-09-14.

    The executable's name is checked rather than `sys.executable` so that a
    watchdog running from source still sees a frozen session, and vice versa.
    """
    if "--dry-run" in cmdline or "watchdog" in cmdline:
        return False
    if "take_profit.py" in cmdline or "run_overnight.py" in cmdline:
        return True
    lowered = cmdline.lower()
    if "trade-research.exe" in lowered or "trade-research " in lowered:
        return any(token in TRADING_SUBCOMMANDS for token in cmdline.split())
    return False


def last_record() -> dict | None:
    """The newest session record, finished or not."""
    import glob
    import json

    try:
        files = sorted(glob.glob(os.path.join(crash_report.directory(), "*.json")))
    except OSError:
        return None
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def refuses_restart(record: dict | None) -> str:
    """Why this session must NOT be restarted, or "" if it may be.

    Reads the record rather than the exit code, because a detached session's
    exit code goes nowhere.
    """
    if record is None:
        return ""
    status = str(record.get("status", ""))
    reason = str(record.get("reason") or "")
    if status == "completed":
        return "the session finished normally"
    if status in ("kill_switch", "console_closed", "ctrl_c", "sigint", "sigterm"):
        return f"it was stopped deliberately ({status})"
    if "daily loss" in reason.lower() or "risk" in status.lower():
        return f"a risk limit stopped it ({reason or status})"
    return ""


def start_session(until_hour: int, extra: list[str]) -> subprocess.Popen:
    """Launch one session. Detached, with its output captured to a file.

    Captured rather than discarded: pythonw with no valid stdout handle dies on
    its first print and, having no console, reports that nowhere. Measured
    2026-09-10.
    """
    root = _root()

    logs = os.path.join(root, "logs")
    os.makedirs(logs, exist_ok=True)
    capture = open(
        os.path.join(logs, "watchdog-launch.log"), "a", encoding="utf-8"
    )

    if getattr(sys, "frozen", False):
        # Frozen, `sys.executable` IS the toolkit, and the scripts are its
        # subcommands -- there is no `tools/run_overnight.py` to hand it.
        # Passing the path anyway made the CLI treat it as an unknown command,
        # print its help into watchdog-launch.log and exit 0. The watchdog then
        # saw a process that ended within seconds, counted it double against
        # the restart budget as "failed to start", and never launched anything.
        # Observed 2026-09-14: a full help listing where a session should be.
        cmd = [sys.executable, "overnight", "--until-hour", str(until_hour), *extra]
    else:
        exe = sys.executable
        pythonw = exe.replace("python.exe", "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw
        cmd = [exe, os.path.join(root, "tools", "run_overnight.py"),
               "--until-hour", str(until_hour), *extra]

    return subprocess.Popen(cmd, cwd=root, stdout=capture, stderr=capture)


def build_parser() -> argparse.ArgumentParser:
    """The command line, as a function so the other side can be tested against it.

    `run_overnight.start_watchdog` builds the argv this parses, and on
    2026-09-20 it built one this REFUSED: `--session-arg --paper` exits 2 with
    "expected one argument", because argparse will not take a value beginning
    with "-". The watchdog died on its first line and the session ran
    unsupervised for 3.7 hours. Neither side's tests caught it, because
    neither side's tests crossed the boundary - the argv was built in one
    process and parsed in another, and nothing ever did both.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--until-hour", type=int, default=6)
    ap.add_argument("--max-restarts", type=int, default=MAX_RESTARTS)
    ap.add_argument("--adopt", action="store_true",
                    help="watch a session that is already running instead of "
                         "starting one")
    ap.add_argument("--session-arg", action="append", default=[],
                    metavar="ARG",
                    help="passed through to run_overnight.py; repeatable")
    return ap


def main() -> int:
    args, _ = build_parser().parse_known_args()

    now = datetime.now()
    target = now.replace(hour=args.until_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)

    print(f"  watchdog: watching until {target:%Y-%m-%d %H:%M}", flush=True)
    print(f"  restarts allowed: {args.max_restarts} (the budget does not refill)",
          flush=True)
    print("  it will NOT restart past a risk halt, a deliberate stop, or the "
          "kill switch", flush=True)

    restarts = 0
    if not args.adopt and not session_alive():
        start_session(args.until_hour, args.session_arg)
        print(f"  [{datetime.now():%H:%M:%S}] started the first session", flush=True)
        time.sleep(RESTART_DELAY)

    while datetime.now() < target:
        time.sleep(POLL_SECONDS)

        if kill_switch.engaged():
            print(f"  [{datetime.now():%H:%M:%S}] kill switch engaged; the "
                  "watchdog stands down", flush=True)
            return 0

        if session_alive():
            continue

        record = last_record()
        refusal = refuses_restart(record)
        if refusal:
            print(f"  [{datetime.now():%H:%M:%S}] session ended and will NOT be "
                  f"restarted: {refusal}", flush=True)
            return 0

        # A death that leaves the record on `running` is the shape the eight
        # had. Say what it left behind before replacing it.
        if record is not None:
            print(f"  [{datetime.now():%H:%M:%S}] DIED: "
                  f"{crash_report.summarise(record)}", flush=True)

        started = record.get("started_at") if record else None
        stopped = record.get("stopped_at") if record else None
        cost = 1
        if started and stopped:
            try:
                lived = (datetime.fromisoformat(stopped)
                         - datetime.fromisoformat(started)).total_seconds()
                if lived < FAST_EXIT_SECONDS:
                    cost = FAST_EXIT_COST
                    print(f"  [{datetime.now():%H:%M:%S}] it lived {lived:.0f}s; "
                          "that is a failure to start, not a crash. Check the "
                          "terminal's Algo Trading toggle.", flush=True)
            except ValueError:
                pass

        restarts += cost
        if restarts > args.max_restarts:
            print(f"  [{datetime.now():%H:%M:%S}] restart budget spent "
                  f"({restarts}/{args.max_restarts}); stopping rather than "
                  "spinning. Something is wrong that a restart will not fix.",
                  flush=True)
            return 1

        time.sleep(RESTART_DELAY)
        if kill_switch.engaged() or session_alive():
            continue
        start_session(args.until_hour, args.session_arg)
        print(f"  [{datetime.now():%H:%M:%S}] restarted "
              f"({restarts}/{args.max_restarts} of the budget spent); the new "
              f"session still stops and flushes at {target:%H:%M}", flush=True)

    print(f"  [{datetime.now():%H:%M:%S}] deadline reached; the watchdog is done",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
