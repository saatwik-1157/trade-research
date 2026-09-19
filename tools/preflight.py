#!/usr/bin/env python
"""Can the harness safely run a session tonight? Read-only, sends nothing.

There is already a preflight at `backend/app/live/preflight.py`, and it is not
this one. That checks the PLATFORM path -- Postgres, Redis, the API, the OMS --
and needs the stack up to say anything. The overnight session does not use any
of that. It is `run_overnight.py` driving `take_profit.py` against the
terminal, and every failure it has actually had is in a different place.

`run_overnight.py --dry-run` covers some of this, but it REFUSES EARLY on a
weekend deadline and then reports nothing else -- so on the Friday before a
Sunday session it tells you the one thing you already knew and none of the
things you wanted to check. This runs every check regardless, and answers the
forward-looking question: not "can I start right now" but "will Sunday work".

Every check here traces to something that went wrong on this account:

    on AC power          the execution power request is terminated 5 minutes
                         after the sleep timeout on DC, so an overnight run
                         on battery cannot be relied on however it is written
    modern standby       an S0 machine needs the display held too, and two
                         fixes shipped against this before one worked
    algo trading on      off means every order is refused instantly and the
                         session exits 1 for no visible reason
    demo account         the fence, checked read-only here
    account flat         a book carried into a session eats its position slots
    no session running   two harvest loops on one account double the risk and
                         the guard is a psutil scan that needs psutil
    kill switch clear    a session started over an engaged switch is a session
                         the operator thought they had stopped
    deadline viable      the venue's week, computed forward rather than only
                         for tonight
    disk for logs        a full-length session writes a 150KB+ log

    python tools/preflight.py
    python tools/preflight.py --until-hour 6
    python tools/preflight.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: E402

OK, WARN, FAIL, UNKNOWN = "OK", "WARN", "FAIL", "UNKNOWN"


def _r(name, status, detail):
    return {"check": name, "status": status, "detail": detail}


def check_power():
    """AC or battery. A DC overnight run is not evidence, by Microsoft's rule."""
    try:
        import take_profit
    except Exception as exc:  # noqa: BLE001
        return _r("on_ac_power", UNKNOWN, f"could not import take_profit: {exc}")
    ac = take_profit.on_ac_power()
    if ac is None:
        return _r("on_ac_power", UNKNOWN, "not Windows, or the API did not answer")
    if ac == take_profit.AC_ONLINE:
        return _r("on_ac_power", OK, "on mains, so the power request has no documented expiry")
    if ac == take_profit.AC_OFFLINE:
        return _r("on_ac_power", FAIL,
                  "ON BATTERY. Microsoft terminates system and execution power "
                  "requests 5 minutes after the sleep timeout expires on DC, so "
                  "the hold WILL lapse mid-session. Plug it in.")
    return _r("on_ac_power", UNKNOWN, f"ACLineStatus={ac}")


def check_standby():
    """S0 needs the display held too. Reported, not a failure."""
    try:
        import take_profit
    except Exception as exc:  # noqa: BLE001
        return _r("standby_type", UNKNOWN, f"could not import take_profit: {exc}")
    s0 = take_profit.modern_standby()
    if s0 is None:
        return _r("standby_type", WARN,
                  "could not be determined; treated as S0, so the screen stays lit")
    if s0:
        return _r("standby_type", OK,
                  "S0 modern standby - the session holds an execution power "
                  "request AND the display, which is what this machine needs")
    return _r("standby_type", OK, "S3 - holding the system alone is enough here")


def check_deps():
    missing = []
    for mod in ("numpy", "pandas", "MetaTrader5", "psutil"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return _r("dependencies", OK, "numpy, pandas, MetaTrader5 and psutil all import")
    crit = "psutil" in missing or "MetaTrader5" in missing
    return _r("dependencies", FAIL if crit else WARN,
              "missing: " + ", ".join(missing) +
              (". Without psutil the second-session guard cannot see a running "
               "session and PROCEEDS UNGUARDED." if "psutil" in missing else ""))


def check_kill_switch():
    try:
        import kill_switch
    except Exception as exc:  # noqa: BLE001
        return _r("kill_switch", UNKNOWN, f"could not import: {exc}")
    if kill_switch.engaged():
        return _r("kill_switch", FAIL,
                  f"ENGAGED: {kill_switch.reason() or 'no reason recorded'}. "
                  "Release it deliberately or the session refuses.")
    return _r("kill_switch", OK, "clear")


def check_no_session():
    try:
        import watchdog
    except Exception as exc:  # noqa: BLE001
        return _r("no_session_running", UNKNOWN, f"could not import watchdog: {exc}")
    try:
        alive = watchdog.session_alive()
    except SystemExit as exc:
        return _r("no_session_running", FAIL, str(exc).strip().splitlines()[0])
    except Exception as exc:  # noqa: BLE001
        return _r("no_session_running", UNKNOWN, f"{type(exc).__name__}: {exc}")
    if alive:
        return _r("no_session_running", FAIL,
                  "a session is ALREADY RUNNING on this machine. Two harvest "
                  "loops on one account compete for position slots and double "
                  "the risk per pass.")
    return _r("no_session_running", OK, "nothing is trading on this machine")


def check_disk():
    root = _paths.project_root()
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        return _r("disk_space", UNKNOWN, f"{exc}")
    mb = free / (1024 * 1024)
    if mb < 200:
        return _r("disk_space", FAIL, f"{mb:.0f} MB free - a full session writes logs and a ledger")
    return _r("disk_space", OK, f"{mb / 1024:.1f} GB free")


def check_venue(until_hour: int):
    """Terminal, demo fence, algo toggle and the open book. Read-only."""
    out = []
    try:
        import mt5_paper
    except Exception as exc:  # noqa: BLE001
        out.append(_r("terminal", UNKNOWN, f"could not import mt5_paper: {exc}"))
        return out, None

    try:
        mt5 = mt5_paper.connect()
    except Exception as exc:  # noqa: BLE001
        out.append(_r("terminal", FAIL,
                      f"{type(exc).__name__}: {exc}. Open MetaTrader 5 and log in."))
        return out, None
    out.append(_r("terminal", OK, "connected"))

    try:
        # live=False so the demo fence is checked WITHOUT demanding the algo
        # toggle; the toggle is reported separately below rather than turning
        # a readable account into an unreadable one.
        info = mt5_paper.assert_demo(mt5, live=False)
        out.append(_r("demo_account", OK,
                      f"{info.get('login')} @ {info.get('server')} [DEMO]"))
    except Exception as exc:  # noqa: BLE001
        out.append(_r("demo_account", FAIL, f"{type(exc).__name__}: {exc}"))

    allowed = getattr(mt5.terminal_info(), "trade_allowed", None)
    if allowed is True:
        out.append(_r("algo_trading", OK, "enabled"))
    elif allowed is False:
        out.append(_r("algo_trading", FAIL,
                      "the terminal's Algo Trading toggle is OFF. Every order "
                      "is refused instantly and the session exits 1 with "
                      "nothing in the log to explain it."))
    else:
        out.append(_r("algo_trading", UNKNOWN, "terminal_info() did not report it"))

    try:
        acct = mt5.account_info()
        pos = mt5_paper.own_positions(mt5)
        n = len(pos)
        out.append(_r("account_flat", OK if n == 0 else WARN,
                      f"balance {acct.balance:,.2f}, {n} position(s) open" +
                      ("" if n == 0 else
                       " - they carry into the session's max-positions count "
                       "and another night of swap. Consider --harvest-only first.")))
    except Exception as exc:  # noqa: BLE001
        out.append(_r("account_flat", UNKNOWN, f"{type(exc).__name__}: {exc}"))

    return out, mt5


def check_deadline(mt5, until_hour: int, paper: bool = False):
    """Is tonight's deadline inside the venue's week, and if not, when is it?

    The forward-looking half is the point. `run_overnight --dry-run` refuses
    and stops; this says which evening does work, which is the question being
    asked on the Friday before a Sunday session.
    """
    try:
        import run_overnight
    except Exception as exc:  # noqa: BLE001
        return _r("deadline_viable", UNKNOWN, f"could not import run_overnight: {exc}")

    if paper:
        # `run_overnight` exempts a --paper run from the weekend guard because
        # it sends no orders, so there is no book a weekend close could
        # strand. Reporting FAIL here for a run the launcher will accept
        # would send the operator away from the one session that is safe to
        # start into a shut market.
        return _r("deadline_viable", OK,
                  "a --paper run opens nothing, so the venue's week does not "
                  "bound it; the launcher exempts it for the same reason")

    try:
        _minutes, target = run_overnight.minutes_until(until_hour)
    except Exception as exc:  # noqa: BLE001
        return _r("deadline_viable", UNKNOWN, f"{type(exc).__name__}: {exc}")

    if mt5 is None:
        return _r("deadline_viable", UNKNOWN,
                  f"the next {until_hour:02d}:00 is {target:%a %d %b %H:%M}, but "
                  "the venue clock could not be read to place it in the "
                  "venue's week")

    weekend = run_overnight.deadline_in_the_weekend(target)
    if weekend is None:
        return _r("deadline_viable", OK,
                  f"{target:%a %d %b %H:%M} is inside the venue's week")

    # A 4-tuple, not the 2 its own docstring advertises: (target_server,
    # offset, live, server). Unpacked by position with a slice so an extra
    # field appearing later is not a crash in a tool whose whole job is to
    # run before anything else does.
    target_server, offset = weekend[0], weekend[1]
    viable = run_overnight.next_viable_deadline(target, offset)
    when = (f"; the next one that works is {viable:%a %d %b %H:%M}, so start "
            f"the evening before -- {viable - timedelta(days=1):%a %d %b}"
            if viable else "")
    return _r("deadline_viable", FAIL,
              f"{target:%a %d %b %H:%M} is {target_server:%a} at the venue, so "
              f"--flat-by cannot be honoured{when}")


def run(until_hour: int, paper: bool = False):
    results = [check_deps(), check_power(), check_standby(),
               check_kill_switch(), check_no_session(), check_disk()]
    venue, mt5 = check_venue(until_hour)
    results.extend(venue)
    results.append(check_deadline(mt5, until_hour, paper))
    if mt5 is not None:
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001, S110
            pass

    fails = [r for r in results if r["status"] == FAIL]
    warns = [r for r in results if r["status"] == WARN]
    unknown = [r for r in results if r["status"] == UNKNOWN]
    if fails:
        verdict = "NOT_READY"
    elif unknown:
        verdict = "READY_WITH_UNKNOWNS"
    elif warns:
        verdict = "READY_WITH_WARNINGS"
    else:
        verdict = "READY"
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "until_hour": until_hour, "verdict": verdict, "checks": results,
            "failed": len(fails), "warned": len(warns), "unknown": len(unknown)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--until-hour", type=int, default=6,
                    help="the deadline to test, 0-23 (default 6)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 unless the verdict is READY")
    ap.add_argument("--paper", action="store_true",
                    help="check for a run that sends NO orders: the venue's "
                         "week does not bound it, because it opens nothing")
    args = ap.parse_args()

    report = run(args.until_hour, args.paper)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL ", UNKNOWN: "  ??  "}
        print()
        kind = " (--paper: no orders)" if args.paper else ""
        print(f"  harness preflight for a {args.until_hour:02d}:00 deadline{kind}")
        print("  " + "-" * 68)
        for r in report["checks"]:
            print(f"  [{mark[r['status']]}] {r['check']:<20} {r['detail']}")
        print("  " + "-" * 68)
        print(f"  {report['verdict']}"
              f"   ({report['failed']} failed, {report['warned']} warned, "
              f"{report['unknown']} unknown)")
        print()
        print("  It sent nothing and changed nothing.")
        print()

    if args.strict and report["verdict"] != "READY":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
