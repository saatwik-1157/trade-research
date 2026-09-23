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
    python tools/run_overnight.py --continuous     # NO stop hour, NO wind-down

The last one is not a longer session, it is a different one. Every other mode
here is flat by its stop hour because the harvest floor books winners and holds
losers, so what is open at the end IS the losing tail; --continuous keeps that
tail and carries it across legs. It exists because it was asked for. Read
run_continuous before using it.
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
import console_guard
import crash_report
import mt5_paper
import paths as _paths

SETTINGS = [
    "--rule", "random",
    # THE THREE CHEAPEST PAIRS, changed 2026-09-23 at the operator's request.
    # Was EURUSD,GBPUSD,USDJPY,USDCAD,AUDUSD,USDCHF,NZDUSD -- every figure in
    # the live record up to 1,003 trades was measured on those seven.
    #
    # Cost per trade, in R, from cost_hurdle's measured spread via
    # s/R = 2(w - 0.5), and independent of any outcome:
    #   USDJPY 0.0096  EURUSD 0.0168  GBPUSD 0.0174   <- kept
    #   USDCAD 0.0224  AUDUSD 0.0384  USDCHF 0.0502  NZDUSD 0.0548  <- dropped
    # That moves the drag from 0.0299R a trade to 0.0146R, which is about 65%
    # of the measured loss. It is the ONLY change this repository can defend,
    # because the saving is a property of the broker rather than of these
    # outcomes: the per-symbol RANKING was measured indistinguishable from
    # shuffled labels at p = 0.62, so nothing here is "keeping the winners".
    #
    # IT IS STILL NEGATIVE. Expected -0.0082R a trade against -0.0235R
    # observed. This makes a loser lose more slowly; it does not make a
    # winner, and no reading of the record should imply otherwise.
    #
    # And it BREAKS COMPARABILITY: trades from here are a three-pair sample
    # and everything before is a seven-pair one. track_record.py raises a
    # data gap when bracket regimes are mixed for the same reason, and the
    # same caution applies to the symbol set.
    "--symbols", "USDJPY,EURUSD,GBPUSD",
    "--risk-usd", "5",
    "--sl-atr", "1.5",
    "--tp-atr", "1.5",
    "--min-profit", "0.50",
    # One position per symbol, so three symbols can hold at most three. Left
    # at 7 this would never bind and the banner would claim a cap that does
    # not exist. It also cuts money at risk per pass from 35 USD to 15.
    "--max-positions", "3",
    "--max-daily-loss", "200",
    # OFF by default, deliberately, for the same reason --risk-usd and
    # --cost-swap are off: every figure in the live record was taken without
    # it, and a default that silently restated them would make the history
    # unreadable.
    #
    # Choose N from the arithmetic, not from a round number. The venue record
    # over 476 trades runs an 80.5% win rate, so a loss is p=0.195 and three
    # in a row is p=0.0074 -- about 3.5 occurrences in six days, a real pause
    # several times a week. Five is p=0.00028, roughly one per 3,500 trades,
    # against a longest observed run of 7. So 3 reacts to ordinary variance
    # and 5 reacts to an outlier.
    "--max-consecutive-losses", "0",
    "--interval", "20",
]

SESSION_SCRIPTS = ("run_overnight.py", "take_profit.py")

#: A leg that ends in far less time than it was ASKED to run did not trade, it
#: failed to start. The usual cause is the terminal's Algo Trading toggle being
#: off, which refuses every order and exits 1 at once. Restarting on that spins.
#: Compared against half the requested leg as well as this ceiling, because a
#: short --leg-minutes otherwise makes every healthy leg look like a failure --
#: which it did, on the first run of this supervisor at --leg-minutes 0.3.
FAST_EXIT_SECONDS = 90.0
#: How many of those in a row before the supervisor stops rather than spins.
MAX_FAST_EXITS = 5
#: Pause between legs. Long enough that a terminal restarting has a moment,
#: short enough that no position sits unmanaged for meaningfully longer than
#: one harvest interval.
RESTART_DELAY_SECONDS = 10.0
#: `take_profit` exit codes the supervisor acts on. A risk halt must not be
#: restarted; a terminal that stopped answering is the thing legs exist for.
RISK_HALT = 2
TERMINAL_HALT = 3


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


def is_rival_session(pid: int, name: str, cmdline: str, mine: set[int]) -> bool:
    """Whether one process row is an order-sending session OTHER than this one.

    Pulled out of `other_sessions` so it can be tested against a fabricated
    process table. It could not be before, and that mattered: the test written
    for the launcher-stub bug passed WITH the bug present, because the test
    process runs `test_crash_report.py` and so its own ancestors never matched
    `SESSION_SCRIPTS` in the first place. Only a source-text assertion caught
    the revert, which is the same weakness that let the `--session-arg` bug
    live in a green suite.
    """
    if pid in mine or not cmdline:
        return False
    if not (name or "").lower().startswith("python"):
        return False
    if not any(s in cmdline for s in SESSION_SCRIPTS):
        return False
    # A paper or dry run sends no orders and holds no position slot.
    return not ("--paper" in cmdline or "--dry-run" in cmdline)


def other_sessions():
    """Order-sending sessions other than this process, as (pid, started, cmd).

    Matched on what each process is running rather than on a lock file,
    because a lock file outlives the process that wrote it: a killed session
    leaves one behind and every later run then refuses on behalf of a session
    that ended hours ago. Of the two failures a stale refusal is the worse,
    since clearing it means deleting a file nobody documented.

    A --paper or --dry-run process is not a session. It sends no orders and
    holds no position slot, so it is not something to refuse for.

    **This process's ANCESTORS are not rivals either, and excluding only
    `os.getpid()` is not enough to say so.** On Windows a virtualenv's
    `Scripts\\pythonw.exe` is a launcher stub: it spawns the base interpreter
    as a CHILD and stays alive with a byte-identical command line. Measured
    2026-09-21 - `Start-Process` reported pid 18944 for the stub while the
    Python code ran as pid 11520, both visible to psutil running
    `run_overnight.py`. So a live session launched that way scanned, found its
    own parent, and refused to start, naming the pid the operator had just
    been handed.

    It went unnoticed because `--paper` masks it: a paper run's cmdline
    contains `--paper`, so the stub is filtered by the clause above and the
    pid check never has to be right. Every unattended launch since the
    scheduled task was written has been a paper one.
    """
    try:
        import psutil
    except ImportError:
        print("  note: psutil not installed, cannot check for a running "
              "session - proceeding unguarded")
        return []

    me = os.getpid()
    # Walk up rather than taking just the parent: a stub may itself be
    # launched by a shell that carries the same command line on its own.
    mine = {me}
    try:
        for ancestor in psutil.Process(me).parents():
            mine.add(ancestor.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            cmdline = " ".join(info["cmdline"] or [])
            if not is_rival_session(info["pid"], info["name"] or "", cmdline, mine):
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
    root = _paths.project_root()
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


def weekend_deadline(target, server_now, local_now):
    """Does `target` (local) fall on a venue weekend? The decision, with no I/O.

    Separated from the venue read so it can be tested against a Friday night in
    Tokyo and a Friday night in Chicago without a terminal, which is the only
    way to know the offset is applied in the right direction.

    Returns (target_in_server_time, offset) or None.
    """
    offset = server_now - local_now
    target_server = target + offset
    if target_server.weekday() >= 5:  # Saturday, Sunday
        return target_server, offset
    return None


def next_viable_deadline(target, offset, limit_days=7):
    """The first deadline after `target` that lands inside the venue's week.

    A refusal that says only "not tonight" reads as a permanent no, and the
    operator's next move is --force -- which is the one option here that ends
    with a book nobody can close. Saying WHEN the answer changes turns the
    refusal into a plan.

    Same arithmetic as `weekend_deadline` and deliberately so: one definition
    of the venue's week, walked forward a day at a time.
    """
    candidate = target
    for _ in range(limit_days):
        candidate += timedelta(days=1)
        if (candidate + offset).weekday() < 5:
            return candidate
    return None


def finishing_status(summary, code):
    """How a session that returned should be recorded. (status, reason).

    `completed` is not the same as `finished as it was asked to`. A session
    that requested --flat-by and ended holding positions failed its last
    obligation, and the record could not say so: the 2026-09-12 run reported
    `completed, exit code 0` with seven positions still at the venue.

    An account that could not be COUNTED is not flat either. It is unknown, and
    unknown is never the good case -- the whole journal exists because a figure
    nobody could read must not be written down as a zero.
    """
    summary = summary or {}
    still = summary.get("still_open")
    flat_by = summary.get("flat_by")
    if flat_by is None:
        return "completed", f"exit code {code}"
    if still is None:
        return "ended_not_flat", (
            f"exit code {code}; --flat-by {flat_by} ran but the open book "
            "could not be counted")
    if still > 0:
        return "ended_not_flat", (
            f"exit code {code}; --flat-by {flat_by} left {still} position(s) "
            "open at the venue")
    return "completed", f"exit code {code}"


def deadline_in_the_weekend(target):
    """The venue's weekday at the deadline, when the deadline is a weekend one.

    Returns `(target_in_server_time, offset)` for a deadline that falls on a
    Saturday or Sunday at the VENUE, and None otherwise -- including when the
    question could not be answered, because refusing a session over a clock we
    could not read would be its own failure.

    **Why this exists.** `--flat-by` promises the account is flat at the
    deadline, and on a Friday-night session that promise cannot be kept: the FX
    week closes before the deadline arrives and every close is refused with
    10018. Measured 2026-09-12 -- a session ran to 06:00 on a Saturday, tried
    seven positions six times, closed none, and left the book to the weekend
    carrying financing and the Monday gap. Nothing was wrong with the session;
    it was asked for something the calendar would not allow.

    **Two assumptions, both stated rather than buried.**

    The venue's week runs from Sunday evening to Friday night in SERVER time,
    so a deadline landing on a Saturday or a Sunday cannot be met. MetaTrader's
    Python API exposes no session schedule, so this is the calendar rather than
    a reading from the venue.

    `server_now()` is the last TICK's timestamp, not a live clock, so the
    offset it yields is the server's only while quotes are arriving. That holds
    at launch, which is when this runs -- a session started into a closed
    market has nothing to trade anyway. When the market IS already shut the
    tick is stale and the offset is wrong, but wrong in the direction that
    makes a weekend deadline look like a weekend deadline, so the guard errs
    toward refusing. That is the right direction to be wrong in.
    """
    import time as _time
    from datetime import datetime as _dt

    PROBES = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD")

    def _stamps(mt5):
        out = {}
        for sym in PROBES:
            tick = mt5.symbol_info_tick(sym)
            if tick and getattr(tick, "time_msc", 0):
                out[sym] = tick.time_msc
        return out

    try:
        mt5 = mt5_paper.connect(None)
        server = mt5_paper.server_now(mt5)
        # Are quotes actually ARRIVING? Sampled twice rather than assumed,
        # because `server_now` is the last tick's stamp and a stale one looks
        # exactly like a live one. Without this the guard printed "server is
        # -5.6h from this clock" on a Saturday -- a plausible figure, measured
        # from a tick seven hours dead, and quoting it as the venue's offset
        # would be inventing a number rather than reporting a gap.
        first = _stamps(mt5)
        _time.sleep(1.5)
        live = _stamps(mt5) != first or not first
    except Exception:
        return None
    decided = weekend_deadline(target, server, _dt.now())
    if decided is None:
        return None
    target_server, offset = decided
    return target_server, offset, live, server


def leg_argv(args) -> list[str]:
    """The take_profit arguments for one continuous leg.

    Deliberately WITHOUT `--flat-by` and `--relax-over`. That pair is what the
    dated session appends, and appending it here would close every position at
    the end of each leg, which is the opposite of what continuous means.
    """
    argv = override(SETTINGS, "--rule", args.rule)
    argv = override(argv, "--max-positions", args.max_positions)
    argv = override(argv, "--max-consecutive-losses",
                    args.max_consecutive_losses)
    argv = argv + ["--minutes", f"{args.leg_minutes:g}"]
    if args.harvest_only:
        argv = argv + ["--harvest-only"]
    if not args.paper:
        argv = argv + ["--live"]
    return argv


def run_leg(command: list[str]) -> int:
    """Run one leg as a CHILD PROCESS and forward its output.

    A child rather than the in-process call the dated session makes, because
    the point of a leg is to survive what kills it. The MetaTrader5 package is
    a DLL wrapper: a fault inside it takes the interpreter down, and in-process
    that ends the run rather than the leg.

    Output is pumped line by line rather than inherited so it passes through
    `start_logging`'s tee. A child writing to the real file descriptor would
    reach the console and never the session log.
    """
    import subprocess

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                print(line.rstrip())
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise


def merge_ledger() -> None:
    """Fold the closed trades into `data/track_record.jsonl`.

    Between legs rather than during one: the merge reads the account, and a
    read taken while the harvest loop is mid-pass is a read of a moment that
    has already gone. It is keyed by position_id and idempotent, so running it
    every leg costs nothing and is what keeps the record accumulating past the
    broker's history window instead of expiring with it.

    A merge that fails does NOT stop the run. It is bookkeeping, and the trades
    are already MetaTrader's to be merged later.
    """
    import subprocess

    tools = os.path.dirname(os.path.abspath(__file__))
    print("\n  merging the ledger ...")
    try:
        done = subprocess.run(
            [sys.executable, os.path.join(tools, "track_record.py"), "--merge"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=300,
        )
    except Exception as exc:
        print(f"  ledger merge failed ({type(exc).__name__}: {exc}); the trades "
              f"are still MetaTrader's and can be merged later")
        return
    for line in (done.stdout or "").splitlines():
        print("    " + line)
    if done.returncode != 0:
        print(f"  ledger merge exited {done.returncode}; continuing")


def run_continuous(args) -> int:
    """Legs back to back, with no stop hour and no wind-down between them.

    **What this gives up.** The dated session is flat by its stop hour, and
    that is not tidiness: the harvest loop closes at the first sign of profit,
    so it books winners and holds losers, and whatever is open when a leg ends
    IS the losing tail. The deadline is what stopped that tail carrying into
    the next session, the weekend and another night of swap -- on 2026-09-07 it
    was 7 positions at -9.25 floating. Nothing here closes them. Requested
    deliberately; `--until-hour` and `--relax-over` are ignored, and the banner
    says so on every start.

    Note what running longer costs in the one direction this repository has
    measured. Frequency is the only lever with a proven sign and it points
    down, at -1.50 to -6.54 points per trade in spread alone. More hours is
    more trades.

    **A leg is a crash boundary, not a deadline.** Nothing is closed when one
    ends; the next leg adopts whatever is open, exactly as a restart does now.
    Legs exist so a terminal that stops answering costs one leg, not the run.

    **A leg that exits at once is not restarted forever.** `take_profit`
    already retries a failed pass and halts after ten consecutive failures, so
    a leg lasting seconds did not trade, it failed to start -- and the usual
    cause is the Algo Trading toggle being off. Five of those in a row stops
    the supervisor and says what to check, rather than filling the log with a
    restart loop that places nothing.
    """
    import time

    command = [sys.executable,
               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "take_profit.py")]
    argv = leg_argv(args)

    if args.dry_run:
        print(f"\n  continuous: legs of {args.leg_minutes:g} minutes, "
              f"no stop hour, NOTHING force-closed")
        print("  take_profit.py " + " ".join(argv) + "\n")
        return 0

    log_path, restore = start_logging()
    leg = 0
    fast_exits = 0
    try:
        print("\n  " + "=" * 68)
        print("  CONTINUOUS SESSION. There is no stop hour and no wind-down.")
        print("  Nothing is force-closed: what a leg leaves open the next leg")
        print("  adopts, and the harvest floor books winners and holds losers,")
        print("  so what accumulates is the losing tail. Ctrl+C to stop -- and")
        print("  stopping does NOT close anything either. To go flat, run:")
        print("      python tools/run_overnight.py --harvest-only --relax-over 0")
        print("  " + "=" * 68)
        print(f"\n  log {log_path}")
        print(f"  legs of {args.leg_minutes:g} minutes; ledger merge between "
              f"legs: {'off' if args.no_merge else 'on'}")

        while True:
            leg += 1
            started = datetime.now()
            print(f"\n  === leg {leg} starting {started:%Y-%m-%d %H:%M:%S} ===")
            code = run_leg(command + argv)
            ran = (datetime.now() - started).total_seconds()
            print(f"\n  === leg {leg} ended after {ran / 60:.1f} min, "
                  f"exit {code} ===")

            if not args.no_merge:
                merge_ledger()

            if code == RISK_HALT:
                print("\n  STOPPED: the session hit its --max-daily-loss limit.")
                print("  Not restarted, deliberately. A supervisor that starts a")
                print("  fresh session after a risk limit fires has not respected")
                print("  the limit, it has renamed it -- the loss carries and only")
                print("  the counter resets. Positions are NOT closed.")
                print("\n  To go flat:")
                print("      python tools/run_overnight.py --harvest-only --relax-over 0")
                print("  To resume anyway, start this again.")
                return 2

            if code == TERMINAL_HALT:
                print("  that leg gave up on a terminal that stopped answering,")
                print("  which is the case legs exist for. Restarting.")

            if ran < min(FAST_EXIT_SECONDS, args.leg_minutes * 30.0):
                fast_exits += 1
                print(f"  that leg lasted {ran:.0f}s, which is not a session "
                      f"that traded ({fast_exits} in a row)")
                if fast_exits >= MAX_FAST_EXITS:
                    print(f"\n  STOPPED: {fast_exits} legs in a row exited at "
                          f"once.")
                    print("  Nothing is being placed, so restarting again would")
                    print("  only fill the log. Check, in this order:")
                    print("    1. MetaTrader 5 is running and LOGGED IN.")
                    print("    2. Its Algo Trading toggle is ON -- off refuses")
                    print("       every order and exits 1 at once, like this.")
                    print("    3. The last leg's output above, for the refusal.")
                    return 1
            else:
                fast_exits = 0

            delay = RESTART_DELAY_SECONDS * (1 + fast_exits)
            print(f"  next leg in {delay:.0f}s (Ctrl+C to stop)")
            time.sleep(delay)
    except KeyboardInterrupt:
        print(f"\n\n  stopped by Ctrl+C after {leg} leg(s). NOTHING was "
              f"closed -- positions are still open.")
        print("  To go flat: python tools/run_overnight.py --harvest-only "
              "--relax-over 0")
        return 0
    finally:
        print(f"\n  session log: {log_path}")
        restore()


def wants_watchdog(harvest_only: bool, no_watchdog: bool) -> str:
    """Should this session be supervised? The decision, with no I/O.

    Separated from the launch for the same reason `weekend_deadline` is
    separated from the venue read: the interesting part is the rule, and a
    rule that can only be exercised by starting a real session beside a real
    terminal is one that gets asserted in a docstring instead of tested.

    Returns "" to supervise, or the reason not to.
    """
    if no_watchdog:
        return "asked not to (--no-watchdog)"
    if harvest_only:
        # The one case that must never be restarted. A wind-down opens
        # nothing and exists to empty a book; relaunching one that died would
        # re-enter the book it was clearing, which makes the supervisor the
        # hazard it was added to prevent.
        return ("a wind-down is not restarted if it dies, because restarting "
                "it would open the book it is emptying")
    return ""


def session_args_for(argv: list[str]) -> list[str]:
    """The operator's own flags, so a RESTART is the same session.

    `watchdog.start_session` rebuilds `run_overnight.py --until-hour N` plus
    whatever `--session-arg` it was given, and it was given nothing: this
    function had no caller, which is the same defect as the watchdog itself
    had before P1b. Every flag the operator passed was therefore dropped on
    restart and `run_overnight`'s own defaults took over.

    That is not a cosmetic loss. A `--paper` session came back **live** and
    sending orders; a session capped at `--max-daily-loss 50` came back at
    200; `--rule rsi_reversion` came back as `random`; two symbols came back
    as seven. A supervisor that restarts a DIFFERENT, looser session than
    the one that died is a hazard wearing a safety net's clothes.

    `--until-hour` is dropped because the watchdog passes its own, already
    parsed and validated. `--no-watchdog` is dropped because a session that
    asked for no supervisor never reaches here.
    """
    drop_with_value = {"--until-hour"}
    drop_alone = {"--no-watchdog"}
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        head = token.split("=", 1)[0]
        if head in drop_with_value:
            i += 1 if "=" in token else 2
            continue
        if head in drop_alone:
            i += 1
            continue
        out.append(token)
        i += 1
    return out


def watchdog_argv(until_hour: int, session_args: list[str] | None = None) -> list[str]:
    """The watchdog's command line, without spawning anything.

    Separated from `start_watchdog` so a test can hand it to
    `watchdog.build_parser()` and check the two agree. They did not: the
    space form `["--session-arg", "--paper"]` is rejected by argparse,
    because every value forwarded here is ITSELF a flag and argparse will not
    read a token beginning with "-" as an option's value. The `=` form is the
    only one that works, and it works for all of them.

    Measured 2026-09-20: the 20:42 session printed "watchdog: pid 24268,
    adopting this session", the watchdog exited 2 on that line, and the
    session ran unsupervised for 3.7h before stopping at 00:25 with nothing
    to restart it. The bug was in neither program - it was between them.
    """
    forwarded = [f"--session-arg={arg}" for arg in session_args or []]
    return ["--adopt", "--until-hour", str(until_hour), *forwarded]


def rotate_watchdog_log(out_path: str, keep: int = 14) -> str | None:
    """Move an existing watchdog log aside so each session starts a clean one.

    Returns the archive path, or None when there was nothing to rotate.

    This has to happen HERE, immediately before the open below, because that
    is the only moment the file belongs to nobody. The handle `start_watchdog`
    opens is inherited by the detached watchdog as both stdout and stderr and
    stays open until the deadline, so a running session cannot be rotated
    around. Measured on Windows 2026-09-21: `os.rename` on the live file
    raises PermissionError WinError 32, and truncating it in place leaves the
    inherited file pointer where it was, so the next line the watchdog prints
    lands after a run of NUL bytes -- destroying the restart record on exactly
    the night it would be read.

    Why bother: the log was append-only across every run, so the argparse
    failure of 2026-09-20 -- the night the session ran 3.7 hours unsupervised
    -- sat at the top of the file while a healthy session ran below it, and
    telling the two apart meant dating the lines by hand.

    It never raises into the launch. A session that trades is worth more than
    the tidiness of its supervisor's log, so any failure falls back to the
    previous behaviour of appending to whatever is already there.
    """
    try:
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            return None
        base, ext = os.path.splitext(out_path)
        stamp = datetime.fromtimestamp(os.path.getmtime(out_path))
        archive = f"{base}-{stamp:%Y%m%d-%H%M%S}{ext}"
        n = 1
        while os.path.exists(archive):
            archive = f"{base}-{stamp:%Y%m%d-%H%M%S}.{n}{ext}"
            n += 1
        os.rename(out_path, archive)
    except OSError:
        return None

    try:
        _prune_watchdog_archives(base, ext, keep)
    except OSError:
        pass
    return archive


def _prune_watchdog_archives(base: str, ext: str, keep: int) -> None:
    """Keep the newest `keep` rotated logs and delete the rest.

    Matches only names this module writes -- `<base>-YYYYMMDD-HHMMSS<ext>`
    and its `.N` collision form. `logs/watchdog-launch.log` sits beside these
    and is NOT a rotation, so the digit check is what keeps it alive.
    """
    stem = os.path.basename(base) + "-"
    folder = os.path.dirname(base) or "."
    found = []
    for name in os.listdir(folder):
        if not name.startswith(stem) or not name.endswith(ext):
            continue
        middle = name[len(stem):-len(ext)] if ext else name[len(stem):]
        parts = middle.split(".")[0].split("-")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            continue
        if len(parts[0]) != 8 or len(parts[1]) != 6:
            continue
        found.append(os.path.join(folder, name))
    for stale in sorted(found, key=os.path.getmtime, reverse=True)[keep:]:
        os.remove(stale)


def start_watchdog(until_hour: int, session_args: list[str] | None = None):
    """Start `tools/watchdog.py --adopt` beside this session. **P1b, closed.**

    The watchdog has existed, tested, since P1b, and nothing launched it.
    `grep -i watchdog start-trading.bat tools/run_overnight.py` returned
    nothing, so the restart budget, the fast-exit cost and the refusal table
    were all written for a process that was never started. The one launch
    attempt in the logs is 2026-09-14 23:16, by hand, and it failed on its own
    arguments. Three of the four sessions that followed ended in a state a
    supervisor would have acted on.

    **Here rather than in `start-trading.bat`**, for two reasons. The batch
    file is not the only entry point -- the frozen executable runs
    `trade-research.exe overnight` and never touches it -- and the hour has
    already been parsed and validated here, where getting it out of `%*` in
    cmd means the delayed-expansion trap this project has already been bitten
    by twice.

    **`--adopt`, because the session is this process.** Left to start one
    itself the watchdog would launch a second harvest loop on the same
    account. `session_alive()` matches `run_overnight.py` in a command line
    and excludes anything with `watchdog` in it, so the first poll 20 seconds
    from now sees this process and adopts it; there is no window in which it
    can decide the session is missing.

    Detached on purpose. A supervisor that dies with the thing it supervises
    is not one, and the session dying is the entire case it exists for.

    Returns the handle, or None when it could not be started -- never raises.
    A session that trades is worth more than its supervisor, so a watchdog
    that will not start is reported and stepped over, not fatal.
    """
    import subprocess

    root = _paths.project_root()
    script = os.path.join(root, "tools", "watchdog.py")
    if not os.path.exists(script) and not getattr(sys, "frozen", False):
        print("  note: tools/watchdog.py is missing; the session runs unsupervised")
        return None

    # Forwarded so a restart reproduces THIS session rather than the
    # defaults. Repeatable by design on the watchdog's side.
    #
    # The `=` form is REQUIRED, not a style choice. Every value forwarded here
    # is itself a flag, so `["--session-arg", "--paper"]` makes argparse see a
    # token starting with "-" where it wants a value, and it exits 2 with
    # "expected one argument" before the watchdog does anything. Measured
    # 2026-09-20: the session of 20:42 printed "watchdog: pid 24268, adopting
    # this session", the watchdog died on that line instantly, and the session
    # ran UNSUPERVISED for 3.7h and then stopped at 00:25 with nothing to
    # restart it. The space form fails for exactly the flags it exists to
    # carry -- it would only have worked for a value that is not a flag, and
    # there are none.
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "watchdog", *watchdog_argv(until_hour, session_args)]
    else:
        exe = sys.executable
        # pythonw where we have it: the watchdog outlives this process and a
        # console window left behind after the session ends is a window the
        # operator closes, which on Windows is a signal to whatever owns it.
        cand = exe.replace("python.exe", "pythonw.exe")
        if cand != exe and os.path.exists(cand):
            exe = cand
        cmd = [exe, script, *watchdog_argv(until_hour, session_args)]

    logs = os.path.join(root, "logs")
    os.makedirs(logs, exist_ok=True)
    out_path = os.path.join(logs, "watchdog.log")
    rotated = rotate_watchdog_log(out_path)

    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP. Without the new group a
    # Ctrl-C or console-close aimed at the session reaches the watchdog too,
    # and it would be gone at the moment it was needed.
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200

    try:
        handle = open(out_path, "a", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=root, stdout=handle, stderr=handle,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    except (OSError, ValueError) as exc:
        print(f"  note: the watchdog would not start ({type(exc).__name__}: {exc}); "
              f"the session runs unsupervised")
        return None

    print(f"  watchdog: pid {proc.pid}, adopting this session until "
          f"{until_hour:02d}:00 (log logs/watchdog.log)")
    if rotated:
        print(f"  the previous watchdog log was rotated to "
              f"logs/{os.path.basename(rotated)}")
    print("  it will NOT restart past a risk halt, a deliberate stop or the "
          "kill switch")
    return proc


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
    ap.add_argument("--max-consecutive-losses", default=None, metavar="N",
                    help="pause new entries after N losing trades in a row. "
                         "Open positions are still managed and the wind-down "
                         "still runs. 5 reacts to an outlier at this win rate, "
                         "3 to ordinary variance -- see SETTINGS")
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
                    help="start anyway: overrides both the running-session "
                         "guard and the refusal to take a deadline that falls "
                         "in the venue's weekend, when holding the book across "
                         "the close is what you meant")
    ap.add_argument("--continuous", action="store_true",
                    help="run with NO stop hour and NO flat-by wind-down: legs "
                         "restart back to back and nothing is ever force-closed. "
                         "This gives up the guard added after a session ended "
                         "holding 7 positions at -9.25 float; read the banner it "
                         "prints before using it")
    ap.add_argument("--leg-minutes", type=float, default=240.0, metavar="MINUTES",
                    help="with --continuous, minutes per leg before a fresh one "
                         "starts (default 240). A leg is a CRASH BOUNDARY, not a "
                         "deadline: nothing is closed when one ends")
    ap.add_argument("--no-watchdog", action="store_true",
                    help="do not start tools/watchdog.py beside the session; "
                         "a full session supervises itself by default")
    ap.add_argument("--no-merge", action="store_true",
                    help="with --continuous, skip the ledger merge between legs. "
                         "The merge is what keeps data/track_record.jsonl "
                         "accumulating past the broker's history window")
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

    # What a previous session left open, when it never got to say goodbye.
    # Printed BEFORE anything starts, because an abandoned tail is counted
    # against this session's --max-positions and pays another night of swap.
    for record in crash_report.unfinished()[:3]:
        print("\n  PREVIOUS SESSION DID NOT FINISH:")
        print(f"    {crash_report.summarise(record)}")
        # Whether the process is still alive changes what this means, and the
        # banner used to say nothing about it: a session killed on Tuesday read
        # exactly like one trading right now. It also never cleared, so it
        # printed on every launch from then on -- and a warning that never
        # clears stops being read, which is expensive when the next one is real.
        alive = crash_report.process_alive(record)
        if alive is True:
            print(f"    Its process (pid {record.get('pid')}) is STILL RUNNING.")
        elif alive is False:
            print(f"    Its process (pid {record.get('pid')}) is gone; this is a "
                  "notice about its tail, not a live session.")
        else:
            print(f"    Whether pid {record.get('pid')} is still running could "
                  "not be determined.")
        print("  If positions are still open, close them first:")
        print("      start-trading.bat --harvest-only")
        if alive is not True:
            print("  Once they are closed, clear this notice:")
            print("      python tools/crash_report.py --resolve "
                  f"{record.get('session_id')}")
        print()

    if args.continuous:
        return run_continuous(args)

    minutes, target = minutes_until(args.until_hour)

    # A wind-down is EXEMPT, and that is not a loophole. The guard exists so a
    # session does not take positions it will not be able to close;
    # --harvest-only takes none, and it is the exact command an operator runs
    # to clean up a book the weekend caught. Refusing it would block the remedy
    # with a warning about the problem.
    # A --paper run is exempt for the SAME reason, and this file already
    # states it twice: --paper passes no --live, so it sends no orders, and
    # `other_sessions` does not count such a process as a session because it
    # "holds no position slot". There is no book for a weekend close to
    # strand. Without this exemption the one run that can safely SPAN the
    # reopen -- the overnight test that proves the host stays awake -- is
    # the one thing refused, and the refusal below then advises starting
    # exactly the session it just declined.
    weekend = (
        None
        if (args.harvest_only or args.paper)
        else deadline_in_the_weekend(target)
    )
    if weekend is not None and not args.force:
        target_server, offset, live, last_tick = weekend
        sign = "+" if offset.total_seconds() >= 0 else "-"
        hours = abs(offset.total_seconds()) / 3600.0
        print(f"\n  REFUSED: the {args.until_hour:02d}:00 deadline lands on a "
              f"{target_server:%A} at the venue.")
        if live:
            print(f"    your {target:%a %H:%M} is {target_server:%a %H:%M} there "
                  f"(server is {sign}{hours:.1f}h from this clock)")
        else:
            print("    QUOTES ARE NOT ARRIVING, so the venue's clock could not be")
            print(f"    read live. Its last tick is stamped {last_tick:%a %H:%M} and "
                  "the market")
            print("    is already shut -- which is the same answer, reached without")
            print("    quoting an offset measured from a dead tick.")
        print()
        print("    --flat-by cannot be honoured across the weekend close. Every")
        print("    close is refused with retcode 10018 and the positions stay")
        print("    open until the venue reopens, carrying financing and the")
        print("    Monday gap. That happened on 2026-09-12: seven positions,")
        print("    six attempts, none closed.")
        print()
        # Only from a LIVE offset. With the market shut the venue clock comes
        # from a dead tick -- here it was stamped Sat 05:29 against a local
        # Sat 22:19, an apparent -16.8h against a true -2.5h -- and walking
        # days forward on that lands a day late. The first version of this
        # said Tuesday when Monday was fine, which is worse than saying
        # nothing: it argues for waiting, and the operator's alternative to
        # waiting is --force. The refusal three lines above already declines
        # to quote this offset; so does this.
        viable = next_viable_deadline(target, offset) if live else None
        if viable is not None:
            print(f"    The next {args.until_hour:02d}:00 deadline inside the venue's "
                  f"week is")
            print(f"    {viable:%a %d %b %H:%M} your time, so the session can be started "
                  f"the")
            print(f"    evening before -- {viable - timedelta(days=1):%a %d %b}.")
            print()
        else:
            print("    The venue's week reopens Sunday 17:00 New York, so a")
            print("    deadline the morning after IS inside the week. No date is")
            print("    given here because the only clock available is a dead tick")
            print("    and a day computed from it would be wrong.")
            print()
            print("    Note that starting on Sunday evening does NOT clear this")
            print("    by itself: the market is still shut then, so the offset is")
            print("    still unmeasurable and this guard refuses for the same")
            print("    reason. A session that SPANS the reopen has to say so.")
            print()
        print("    Nothing was started and no order was sent. Options:")
        print("      - run it on a night whose deadline is inside the week")
        print("      - --until-hour N, with a deadline before the close")
        print("      - --paper, for a run that sends NO orders: it is exempt,")
        print("        because it opens nothing a weekend close could strand")
        print("      - --force, if holding the book over the weekend is what")
        print("        you actually want. It also waives the running-session")
        print("        guard, so do not use it unattended.")
        print()
        return 1

    argv = override(SETTINGS, "--rule", args.rule)
    argv = override(argv, "--max-positions", args.max_positions)
    argv = override(argv, "--max-consecutive-losses",
                    args.max_consecutive_losses)
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

    session_id = crash_report.new_session_id()
    if not args.dry_run:
        crash_report.start(
            session_id,
            command=["take_profit.py"] + argv,
            log_path=log_path,
            deadline=f"{target:%Y-%m-%d %H:%M}",
            live=not args.paper,
        )

    try:
        print(f"\n  now {datetime.now():%H:%M} -> stop {target:%H:%M} "
              f"({minutes:.0f} minutes)")
        print("  take_profit.py " + " ".join(argv) + "\n")
        if log_path:
            print(f"  log {log_path}\n")

        if args.dry_run:
            return 0

        # FULL SESSIONS ONLY, and --harvest-only is the case that matters.
        # A wind-down opens nothing and exists to empty a book; restarting one
        # that died would re-enter the book it was deliberately clearing,
        # which turns a supervisor into the thing it is supposed to protect
        # against. --continuous never reaches here (it returns above) and it
        # has no fixed deadline for a watchdog to preserve anyway.
        # Deliberately not held and deliberately not killed on the way out.
        # A `finally` that terminated it would fire on exactly the exception
        # that makes a restart worth having, and the watchdog already stands
        # down by itself: `refuses_restart` reads `completed` off the record
        # and exits, and the poll loop ends at the deadline regardless.
        skip = wants_watchdog(args.harvest_only, args.no_watchdog)
        if skip:
            print(f"  no watchdog: {skip}")
        else:
            start_watchdog(args.until_hour, session_args_for(sys.argv[1:]))

        import take_profit

        # The heartbeat. Every pass rewrites the record with the session's
        # last known state, so a kill that reaches no handler still leaves a
        # file saying when it was alive and what it was holding.
        take_profit.PASS_HOOK = lambda n, st: crash_report.beat(session_id, n, st)

        def _stop(reason: str) -> None:
            # Record FIRST, wind down second. On a console close the OS is
            # already counting down, and the evidence has to survive even
            # when the flush does not.
            crash_report.finish(
                session_id, status=reason, reason="stop requested from the console"
            )
            take_profit.STOP_REQUESTED = True

        print(f"  stop handling: {console_guard.install(_stop)}")

        sys.argv = ["take_profit.py"] + argv
        code = take_profit.main()
        if not console_guard.stopping():
            # The post-flush state, not the last heartbeat. A record that
            # closed `completed` while still reporting the positions the
            # final pass saw would read as an abandoned tail.
            done = getattr(take_profit, "LAST_SUMMARY", None) or {}
            status, detail = finishing_status(done, code)
            crash_report.finish(
                session_id, status=status, reason=detail,
                state={
                    "positions_open": done.get("still_open"),
                    "flushed": done.get("flushed"),
                    "passes": done.get("passes"),
                    "harvested": done.get("harvested"),
                    "opened": done.get("opened"),
                    "realised": done.get("realised"),
                } if done else None,
            )
        return code
    except BaseException as exc:
        crash_report.finish(
            session_id, status="error", reason=f"{type(exc).__name__}: {exc}"
        )
        raise
    finally:
        mod = sys.modules.get("take_profit")
        if mod is not None:
            mod.PASS_HOOK = None
        if log_path:
            print(f"\n  session log: {log_path}")
        if restore is not None:
            restore()


if __name__ == "__main__":
    raise SystemExit(main())
