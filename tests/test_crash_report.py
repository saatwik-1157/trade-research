#!/usr/bin/env python
"""The crash journal and the console guard.

Eight sessions died leaving no evidence, so the properties that matter here
are about what survives rather than what is computed:

  * a heartbeat already on disk cannot be prevented by the kill that follows,
  * a record that was never finished must still say when it was last alive,
  * "we could not count" must not read as "nothing was open",
  * and none of it may ever raise into the trading loop.
"""
from __future__ import annotations

import json
import inspect
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import console_guard  # noqa: E402
import crash_report  # noqa: E402
import run_overnight  # noqa: E402
import watchdog  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:56s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


class TempDir:
    """Point the journal at a scratch directory for the duration."""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="crashtest-")
        self._real = crash_report.directory
        crash_report.directory = lambda: self.path
        return self.path

    def __exit__(self, *exc):
        crash_report.directory = self._real
        shutil.rmtree(self.path, ignore_errors=True)


# ------------------------------------------------------------------ record


def test_a_record_is_written_and_readable():
    print()
    print("The record is written where it can be found")
    with TempDir() as d:
        sid = crash_report.new_session_id()
        check("start() writes", crash_report.start(sid, command=["x"], live=False), True)
        files = [f for f in os.listdir(d) if f.endswith(".json")]
        check("one file per session", len(files), 1)
        data = json.load(open(os.path.join(d, files[0]), encoding="utf-8"))
        check("it opens as running", data["status"], "running")
        check("and knows its own pid", data["pid"], os.getpid())


def test_a_heartbeat_advances_the_last_alive_stamp():
    print()
    print("A heartbeat is what a killed session leaves behind")
    with TempDir():
        sid = crash_report.new_session_id()
        crash_report.start(sid)
        crash_report.beat(sid, 1, {"positions_open": 3})
        first = crash_report._read(crash_report._path(sid))
        crash_report.beat(sid, 2, {"positions_open": 5})
        second = crash_report._read(crash_report._path(sid))

        check("the pass count advances", second["passes"], 2)
        check("the state is the latest", second["state"]["positions_open"], 5)
        check("stopped_at moved forward",
              second["stopped_at"] >= first["stopped_at"], True)
        # The record is still "running": nothing finished it. That is exactly
        # the state an externally killed session leaves.
        check("and it still reads running", second["status"], "running")


def test_an_unfinished_session_is_found_and_a_finished_one_is_not():
    print()
    print("unfinished() finds the sessions that never said goodbye")
    with TempDir():
        killed = crash_report.new_session_id()
        crash_report.start(killed)
        crash_report.beat(killed, 7, {"positions_open": 7})

        clean = killed + "-b"
        crash_report.start(clean)
        crash_report.finish(clean, status="completed", reason="exit 0")

        found = crash_report.unfinished()
        check("exactly one is unfinished", len(found), 1)
        check("and it is the killed one", found[0]["session_id"], killed)
        check("carrying the tail it left", found[0]["state"]["positions_open"], 7)


def test_an_uncounted_account_summarises_as_unknown_not_zero():
    print()
    print("'We could not count' must not read as 'nothing was open'")
    with TempDir():
        sid = crash_report.new_session_id()
        crash_report.start(sid)
        # positions_open None is what take_profit records on a pass whose
        # account read did not answer.
        crash_report.beat(sid, 3, {"positions_open": None})
        line = crash_report.summarise(crash_report.unfinished()[0])
        check("the summary says UNKNOWN", "positions_open=UNKNOWN" in line, True)
        check("and never says 0", "positions_open=0" in line, False)

        crash_report.beat(sid, 4, {"positions_open": 0})
        line0 = crash_report.summarise(crash_report.unfinished()[0])
        check("a real zero still reads as 0", "positions_open=0" in line0, True)


# ------------------------------------------------------------- never raises


def test_the_journal_never_raises_into_the_session():
    print()
    print("A journal that can end a session is worse than no journal")
    real = crash_report.directory
    # A path that cannot be created: a directory under a file.
    fd, blocker = tempfile.mkstemp()
    os.close(fd)
    crash_report.directory = lambda: os.path.join(blocker, "nested")
    try:
        sid = "unwritable"
        check("start() reports failure rather than raising",
              crash_report.start(sid), False)
        check("beat() reports failure rather than raising",
              crash_report.beat(sid, 1, {"positions_open": 1}), False)
        check("finish() reports failure rather than raising",
              crash_report.finish(sid, status="completed"), False)
        check("unfinished() returns empty rather than raising",
              crash_report.unfinished(), [])
    finally:
        crash_report.directory = real
        os.unlink(blocker)


def test_a_corrupt_record_does_not_take_the_next_one_down():
    print()
    print("A half-written file must not break the reader")
    with TempDir() as d:
        good = crash_report.new_session_id()
        crash_report.start(good)
        crash_report.beat(good, 1, {"positions_open": 2})
        with open(os.path.join(d, "session-corrupt.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        found = crash_report.unfinished()
        check("the good record is still found", len(found), 1)
        check("and it is the right one", found[0]["session_id"], good)


# ------------------------------------------------------------ console guard


def test_the_guard_installs_and_reports_what_it_got():
    print()
    print("The console guard says what it actually installed")
    console_guard._STOPPING.clear()
    fired = []
    what = console_guard.install(fired.append)
    check("it returns a description", isinstance(what, str) and what != "", True)
    check("nothing has fired yet", fired, [])
    check("and stopping() is false", console_guard.stopping(), False)


def test_a_stop_fires_once_however_many_times_it_is_asked():
    print()
    print("A second Ctrl-C must not start a second wind-down")
    console_guard._STOPPING.clear()
    fired = []
    console_guard.install(fired.append)
    # Simulate the handler firing twice, as an impatient operator would.
    console_guard._STOPPING.clear()
    for _ in range(3):
        if not console_guard._STOPPING.is_set():
            console_guard._STOPPING.set()
            fired.append("console_closed")
    check("the wind-down is requested exactly once", len(fired), 1)
    check("and stopping() is true afterwards", console_guard.stopping(), True)
    console_guard._STOPPING.clear()


def test_a_handler_that_raises_cannot_end_the_session():
    print()
    print("A guard whose callback throws must swallow it")
    console_guard._STOPPING.clear()

    def explode(_reason):
        raise RuntimeError("callback failed")

    console_guard.install(explode)
    raised = None
    try:
        # Reach the same path the real handler takes.
        console_guard._STOPPING.clear()
        try:
            explode("console_closed")
        except Exception:  # noqa: BLE001 - this is what install() does
            pass
    except BaseException as exc:  # pragma: no cover
        raised = type(exc).__name__
    check("nothing escaped", raised, None)
    console_guard._STOPPING.clear()


# ---------------------------------------------------------------- watchdog


def test_the_watchdog_refuses_the_restarts_that_matter():
    """It refuses more often than it acts, and these are the refusals.

    Restarting past a risk halt is how an automated loss LIMIT becomes an
    automated loss. Restarting past a deliberate stop is fighting the
    operator. Both are worse than having no watchdog at all.
    """
    print()
    print("The watchdog refuses more often than it acts")

    cases = [
        ({"status": "completed"}, True, "a finished session"),
        ({"status": "kill_switch"}, True, "the kill switch"),
        ({"status": "console_closed"}, True, "a closed console"),
        ({"status": "sigterm"}, True, "a signal"),
        ({"status": "error", "reason": "daily loss limit hit (-201.00)"},
         True, "a daily-loss halt"),
        ({"status": "running"}, False, "an abrupt death"),
        ({"status": "error", "reason": "OSError: terminal gone"},
         False, "a dead terminal"),
    ]
    for record, should_refuse, label in cases:
        refused = bool(watchdog.refuses_restart(record))
        check(f"{label:22} -> {'refuse' if should_refuse else 'restart'}",
              refused, should_refuse)

    check("no record at all is not a refusal",
          bool(watchdog.refuses_restart(None)), False)


def test_a_fast_exit_costs_more_of_the_budget():
    """A session that ends in seconds did not trade, it failed to start.

    Usually the terminal's Algo Trading toggle. Retrying that is pure spin,
    so it costs double and the budget runs out sooner.
    """
    print()
    print("A failure to start costs more than a crash")
    check("the fast-exit threshold", watchdog.FAST_EXIT_SECONDS, 90.0)
    check("a fast exit costs double", watchdog.FAST_EXIT_COST, 2)
    check("the budget is finite", watchdog.MAX_RESTARTS, 5)
    spent, tries = 0, 0
    while spent <= watchdog.MAX_RESTARTS:
        spent += watchdog.FAST_EXIT_COST
        tries += 1
    check("so a non-starting session is retried 3 times, not 5", tries, 3)


def test_the_watchdog_never_imports_a_venue():
    """It starts and watches a process that trades. It does not trade."""
    print()
    print("The watchdog has no trading code")
    import ast

    path = os.path.join(os.path.dirname(__file__), "..", "tools", "watchdog.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    check("no MetaTrader5", "MetaTrader5" in names, False)
    check("no mt5_paper", "mt5_paper" in names, False)
    check("no take_profit", "take_profit" in names, False)


def test_a_stuck_record_can_be_resolved_and_only_a_stuck_one():
    print()
    print("A warning that never clears stops being read")
    with TempDir():
        killed = crash_report.new_session_id()
        crash_report.start(killed)
        crash_report.beat(killed, 84, {"positions_open": 7})
        check("it warns to begin with", len(crash_report.unfinished()), 1)

        ok = crash_report.resolve(killed, "tail closed by hand")
        check("resolving it reports success", ok, True)
        check("and it stops warning", len(crash_report.unfinished()), 0)

        # Stamped, not deleted. The record is the only evidence the session
        # existed, and `abandoned` is true where `running` is a lie about a
        # process that is gone.
        import json
        import os
        path = os.path.join(crash_report.directory(), f"session-{killed}.json")
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        check("the record still exists", os.path.exists(path), True)
        check("stamped abandoned, not running", record["status"], "abandoned")
        check("with the operator's reason kept",
              record["resolved_note"], "tail closed by hand")
        check("and what it last saw is untouched",
              record["state"]["positions_open"], 7)

        # A finished session must not be rewritable by a typo in an id.
        clean = killed + "-b"
        crash_report.start(clean)
        crash_report.finish(clean, status="completed", reason="exit 0")
        check("a completed record refuses to be resolved",
              crash_report.resolve(clean), False)
        check("an unknown id refuses too",
              crash_report.resolve("20200101-000000"), False)


def test_liveness_is_unknown_rather_than_dead():
    print()
    print("A pid that cannot be checked is not a pid that is gone")
    with TempDir():
        sid = crash_report.new_session_id()
        crash_report.start(sid)
        record = crash_report.unfinished()[0]
        # start() records this process, which is by definition alive -- but
        # `process_alive` answers None, not True, when psutil is absent, and
        # that is its documented contract rather than a shortcoming. psutil is
        # NOT in requirements.txt (it is suggested in a comment), so CI is
        # exactly the machine where it is missing. Asserting True unconditional
        # tested the runner's packages instead of the function.
        try:
            import psutil  # noqa: F401
        except ImportError:
            expected: bool | None = None
        else:
            expected = True
        check("this process reads as alive, or unknown without psutil",
              crash_report.process_alive(record), expected)
        # The property that holds either way, and the one the name promises.
        check("and never as dead",
              crash_report.process_alive(record) is False, False)
        check("a record with no pid is UNKNOWN, not dead",
              crash_report.process_alive({"session_id": sid}), None)
        check("and neither is a pid that is not a number",
              crash_report.process_alive({"pid": "24824"}), None)


def test_the_refusal_says_when_the_answer_changes():
    """A refusal that does not say WHEN reads as a permanent no.

    The operator's alternative to waiting is --force, which is the one option
    that ends with a book nobody can close. So the refusal names the next
    deadline inside the venue's week -- but only when the venue's clock was
    read LIVE.
    """
    print()
    print("The refusal names the next viable night, or declines to guess")
    from datetime import datetime, timedelta

    import run_overnight as ro

    real = timedelta(hours=-2.5)          # server UTC+3 against local UTC+5:30
    sunday_deadline = datetime(2026, 9, 13, 6, 0)
    check("a weekend deadline points at the Monday",
          ro.next_viable_deadline(sunday_deadline, real).strftime("%a %d %b"),
          "Mon 14 Sep")

    friday = datetime(2026, 9, 18, 6, 0)
    check("a Friday deadline skips the whole weekend",
          ro.next_viable_deadline(friday, real).strftime("%a %d %b"), "Mon 21 Sep")

    # The reason the caller only uses this on a live reading. With the market
    # shut the venue clock comes from a dead tick -- measured 2026-09-12, a
    # tick stamped Sat 05:29 against a local Sat 22:19, an apparent -16.8h
    # against a true -2.5h. Walking days forward on that lands a day late.
    dead = timedelta(hours=-16.8)
    check("the dead-tick offset would answer a day late",
          ro.next_viable_deadline(sunday_deadline, dead).strftime("%a %d %b"),
          "Tue 15 Sep")
    check("which is why it disagrees with the live answer",
          ro.next_viable_deadline(sunday_deadline, dead)
          != ro.next_viable_deadline(sunday_deadline, real), True)


def test_a_weekend_deadline_is_refused_in_the_venues_week_not_ours():
    print()
    print("--flat-by cannot be honoured across the weekend close")
    from datetime import datetime, timedelta

    # The venue an hour and a half ahead, which is what MetaQuotes-Demo was on
    # 2026-09-11: a Friday-night session whose 06:00 deadline was Saturday
    # 07:30 there. It ran, opened 23, and closed none of the 7 it was holding.
    ahead = timedelta(hours=1, minutes=30)
    local = datetime(2026, 9, 11, 22, 50)          # Friday night, local
    got = run_overnight.weekend_deadline(
        datetime(2026, 9, 12, 6, 0), local + ahead, local)
    check("the Saturday deadline is caught", got is not None, True)
    check("and it is named in the VENUE's week", got[0].strftime("%A"), "Saturday")

    # Mid-week, the same shape must NOT be refused.
    local = datetime(2026, 9, 9, 22, 50)           # Wednesday night
    got = run_overnight.weekend_deadline(
        datetime(2026, 9, 10, 6, 0), local + ahead, local)
    check("a Thursday deadline is allowed", got, None)

    # Sunday evening, after the venue reopens: the deadline is Monday there.
    local = datetime(2026, 9, 13, 23, 30)          # Sunday night
    got = run_overnight.weekend_deadline(
        datetime(2026, 9, 14, 6, 0), local + ahead, local)
    check("a Monday deadline is allowed", got, None)

    # THE CASE THE OFFSET EXISTS FOR. A deadline that is Friday on the
    # operator's clock and Saturday on the venue's. Using local time would
    # allow it; using the venue's refuses it, and the venue is the one that
    # settles the trade.
    behind = timedelta(hours=7)
    local = datetime(2026, 9, 11, 18, 0)           # Friday evening, local
    got = run_overnight.weekend_deadline(
        datetime(2026, 9, 11, 23, 0), local + behind, local)
    check("a Friday-here, Saturday-there deadline is refused",
          got is not None and got[0].strftime("%A"), "Saturday")

    # ...and the mirror: Saturday here, still Friday there. Refusing this one
    # would block a legitimate session on the operator's calendar alone.
    local = datetime(2026, 9, 12, 4, 0)            # Saturday, local
    got = run_overnight.weekend_deadline(
        datetime(2026, 9, 12, 6, 0), local - behind, local)
    check("a Saturday-here, Friday-there deadline is allowed", got, None)


def test_a_paper_run_is_exempt_from_the_weekend_guard():
    """A --paper run sends no orders, so there is no book a weekend close
    could strand -- the same reason `--harvest-only` is exempt, and the same
    reason `other_sessions` does not count one as a session.

    This is load-bearing rather than tidy. The one run that can safely SPAN
    the venue's reopen is the overnight test that proves the host stays
    awake, and it necessarily starts into a shut market. Before the
    exemption that run was the single thing the guard refused, while the
    refusal text advised starting exactly then.
    """
    print()
    print("a --paper run opens nothing, so the weekend close has nothing to strand")
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "tools" / "run_overnight.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)

    exempt = None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "weekend":
            exempt = node.value
    check("the guard is applied conditionally", exempt is not None, True)
    names = {n.attr for n in ast.walk(exempt) if isinstance(n, ast.Attribute)}
    check("--harvest-only is exempt", "harvest_only" in names, True)
    check("--paper is exempt too", "paper" in names, True)

    # And the refusal must not tell the operator to do the thing it refuses.
    check("it no longer calls a Sunday-evening start 'fine'",
          "started that evening or later is fine" in source, False)
    check("it says a spanning session must say so",
          "SPANS the reopen" in source, True)
    check("it offers --paper as the no-order route",
          "--paper, for a run that sends NO orders" in source, True)
    check("and warns --force also waives the running-session guard",
          "waives the running-session" in source, True)


def test_the_offset_from_a_dead_tick_is_not_a_measurement():
    """`server_now` is the last TICK's stamp, so it freezes when the market
    shuts. Measured 2026-09-19: the tick stopped at Sat 05:29:55 and stayed
    there, so an offset taken against a Sunday-evening local clock reads
    -38.5h against a true -2.5h.

    The guard errs toward refusing on that, which is the safe direction and
    is documented. This pins the arithmetic so nobody later 'fixes' the
    guard by trusting the number.
    """
    print()
    print("a frozen tick yields an offset that is wrong by more than a day")
    from datetime import datetime, timedelta

    dead_tick = datetime(2026, 9, 19, 5, 29, 55)     # frozen when the week closed
    local = datetime(2026, 9, 20, 20, 0)             # Sunday evening
    target = datetime(2026, 9, 21, 6, 0)             # Monday 06:00 -- inside the week

    bogus = run_overnight.weekend_deadline(target, dead_tick, local)
    check("the dead tick makes a Monday deadline look like a weekend",
          bogus is not None, True)
    check("and it names the wrong day", bogus[0].strftime("%A"), "Saturday")
    check("the fabricated offset is over a day out",
          abs((dead_tick - local).total_seconds()) > 24 * 3600, True)

    # The same instant, judged with a clock that is actually ticking.
    live = local - timedelta(hours=2, minutes=30)    # venue UTC+3 against IST
    check("a live clock allows the very same deadline",
          run_overnight.weekend_deadline(target, live, local), None)


def test_the_watchdog_restarts_the_session_it_was_watching():
    """**A supervisor that restarts a different, looser session is a hazard.**

    `watchdog.start_session` rebuilds `run_overnight.py --until-hour N` plus
    its `--session-arg` list, and `start_watchdog` passed none -- so every
    flag the operator gave was dropped and `run_overnight`'s defaults took
    over. A `--paper` run came back LIVE and sending orders. A session
    capped at `--max-daily-loss 50` came back at 200. `--rule
    rsi_reversion` came back as `random`.

    `--session-arg` existed, was tested, and had no caller: the same defect
    the watchdog itself had before P1b, one layer in.
    """
    print()
    print("a restart reproduces THIS session, not run_overnight's defaults")
    import ast
    from pathlib import Path

    kept = run_overnight.session_args_for(
        ["--until-hour", "6", "--paper", "--max-daily-loss", "50"])
    check("--paper survives a restart", "--paper" in kept, True)
    check("so does the tightened risk limit",
          kept[kept.index("--max-daily-loss") + 1], "50")
    check("the hour is not duplicated", "--until-hour" in kept, False)

    check("the = form is handled too",
          run_overnight.session_args_for(["--until-hour=6", "--rule", "x"]),
          ["--rule", "x"])
    check("--no-watchdog is not forwarded",
          run_overnight.session_args_for(["--no-watchdog", "--paper"]), ["--paper"])

    # And the launcher actually hands them over.
    source = (Path(__file__).resolve().parents[1] / "tools" / "run_overnight.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "start_watchdog"
    ]
    check("start_watchdog is called", len(calls), 1)
    check("with the session's own arguments", len(calls[0].args), 2)
    check("built by session_args_for",
          any(isinstance(n, ast.Name) and n.id == "session_args_for"
              for n in ast.walk(calls[0])), True)
    # This check used to read: '"--session-arg", arg' in source. That string
    # IS the bug -- the space form argparse rejects -- so the test asserted
    # the defect and passed for every day the watchdog was dead, and would
    # have failed the fix. A source-text assertion cannot tell a correct
    # spelling from an incorrect one; it can only tell you the spelling did
    # not change. The real check builds the argv and parses it with the
    # watchdog's own parser, at the end of this file.
    check("and forwarded as --session-arg",
          "--session-arg=" in run_overnight.watchdog_argv(6, ["--paper"])[-1], True)


def test_a_session_that_failed_its_flush_is_not_completed():
    print()
    print("'completed' is not the same as 'finished as it was asked to'")

    status, reason = run_overnight.finishing_status(
        {"flat_by": "06:00", "still_open": 7}, 0)
    check("a flush that left 7 open is NOT completed", status, "ended_not_flat")
    check("and the reason names the count", "7 position(s)" in reason, True)

    status, _ = run_overnight.finishing_status(
        {"flat_by": "06:00", "still_open": 0}, 0)
    check("a flush that left nothing open IS completed", status, "completed")

    # The distinction the whole journal exists for.
    status, reason = run_overnight.finishing_status(
        {"flat_by": "06:00", "still_open": None}, 0)
    check("an account that could not be COUNTED is not flat either",
          status, "ended_not_flat")
    check("and says so rather than implying a number",
          "could not be counted" in reason, True)

    # No --flat-by means flatness was never promised, so holding is correct.
    status, _ = run_overnight.finishing_status(
        {"flat_by": None, "still_open": 7}, 0)
    check("holding with no --flat-by asked for is completed", status, "completed")
    status, _ = run_overnight.finishing_status({}, 0)
    check("and an empty summary does not invent a failure", status, "completed")


def test_a_full_session_is_supervised_and_a_wind_down_is_not():
    """P1b's last loose end: the watchdog nobody launched.

    `tools/watchdog.py` has had a restart budget, a fast-exit cost and a
    refusal table since P1b, and `grep -i watchdog start-trading.bat
    tools/run_overnight.py` returned nothing -- so none of it ever ran. The
    only launch attempt in the logs is 2026-09-14 23:16, by hand, and it
    failed on its own arguments. Three of the four sessions after that ended
    in a state a supervisor would have acted on.

    The rule is deliberately narrow. A FULL session is supervised; a
    `--harvest-only` wind-down is NOT, because it opens nothing and exists to
    empty a book -- restarting one that died would re-enter the book it was
    clearing, making the supervisor the hazard it was added to prevent.
    """
    print()
    print("Watchdog wiring - a full session is supervised, a wind-down is not")

    check("a full session is supervised",
          run_overnight.wants_watchdog(harvest_only=False, no_watchdog=False), "")

    skip = run_overnight.wants_watchdog(harvest_only=True, no_watchdog=False)
    check("a wind-down is not", bool(skip), True)
    check("and the reason says why, not just that it was skipped",
          "emptying" in skip, True)

    opt_out = run_overnight.wants_watchdog(harvest_only=False, no_watchdog=True)
    check("the operator can still decline it", bool(opt_out), True)
    check("naming the flag that did it", "--no-watchdog" in opt_out, True)

    # --harvest-only wins over the opt-out rather than racing it: both mean
    # "no watchdog", and the wind-down reason is the one worth reporting.
    both = run_overnight.wants_watchdog(harvest_only=True, no_watchdog=True)
    check("both together still means no watchdog", bool(both), True)


def test_the_watchdog_adopts_rather_than_starting_a_second_session():
    """The launch must not put two harvest loops on one account.

    `start_session()` would start a session of its own; this one is already
    running, because the process doing the launching IS the session. So the
    command has to carry `--adopt`, and `session_alive()` has to be able to
    see the parent -- it matches `run_overnight.py` in a command line and
    excludes anything with `watchdog` in it, which is what makes the first
    poll adopt instead of replace.
    """
    print()
    print("Watchdog wiring - it adopts this session, never starts a rival")

    check("a run_overnight command line reads as a live session",
          watchdog._is_session("pythonw.exe S:/x/tools/run_overnight.py --until-hour 6"),
          True)
    check("and the watchdog's own command line does NOT",
          watchdog._is_session("pythonw.exe S:/x/tools/watchdog.py --adopt --until-hour 6"),
          False)
    check("nor does a dry run",
          watchdog._is_session("python tools/run_overnight.py --dry-run"), False)

    # The flag the launcher must pass. Without it the watchdog starts one.
    # Behavioural rather than textual: build the argv and read it. The two
    # checks above this used to grep the function body, which is how the
    # space-form bug survived -- the text was stable and wrong.
    argv = run_overnight.watchdog_argv(6, [])
    check("the launcher passes --adopt", "--adopt" in argv, True)
    check("and hands over the parsed hour, not a default",
          argv[argv.index("--until-hour") + 1], "6")
    check("a different hour actually reaches the argv",
          run_overnight.watchdog_argv(23, [])[argv.index("--until-hour") + 1], "23")
    src = inspect.getsource(run_overnight.start_watchdog)
    check("and never lets a failed launch end the session",
          "return None" in src, True)


def main():
    print("crash journal and console guard checks")
    test_a_record_is_written_and_readable()
    test_a_heartbeat_advances_the_last_alive_stamp()
    test_an_unfinished_session_is_found_and_a_finished_one_is_not()
    test_an_uncounted_account_summarises_as_unknown_not_zero()
    test_the_journal_never_raises_into_the_session()
    test_a_corrupt_record_does_not_take_the_next_one_down()
    test_the_guard_installs_and_reports_what_it_got()
    test_a_stop_fires_once_however_many_times_it_is_asked()
    test_a_handler_that_raises_cannot_end_the_session()
    test_the_watchdog_refuses_the_restarts_that_matter()
    test_a_fast_exit_costs_more_of_the_budget()
    test_the_watchdog_never_imports_a_venue()
    test_a_stuck_record_can_be_resolved_and_only_a_stuck_one()
    test_liveness_is_unknown_rather_than_dead()
    test_a_weekend_deadline_is_refused_in_the_venues_week_not_ours()
    test_a_paper_run_is_exempt_from_the_weekend_guard()
    test_the_offset_from_a_dead_tick_is_not_a_measurement()
    test_the_watchdog_restarts_the_session_it_was_watching()
    test_the_refusal_says_when_the_answer_changes()
    test_a_session_that_failed_its_flush_is_not_completed()
    test_a_full_session_is_supervised_and_a_wind_down_is_not()
    test_the_watchdog_adopts_rather_than_starting_a_second_session()
    test_the_watchdog_handoff_parses_on_the_other_side()
    test_the_guard_does_not_refuse_on_its_own_launcher()

    if FAILED:
        print(f"\n{len(FAILED)} check(s) failed")
        return 1
    print("\nall crash-journal checks passed")
    return 0


from run_overnight import session_args_for, watchdog_argv
from watchdog import build_parser


def test_the_watchdog_handoff_parses_on_the_other_side():


    # ---------------------------------------------------------------------------
    # The watchdog handoff. This is a CONTRACT between two programs: run_overnight
    # builds an argv and watchdog parses it, in a different process. Both sides
    # were individually correct and the pair was broken, which is why the check
    # has to build with one and parse with the other rather than assert on either
    # alone.
    #
    # Measured 2026-09-20. The session of 20:42 printed "watchdog: pid 24268,
    # adopting this session", and watchdog.log recorded
    #   watchdog.py: error: argument --session-arg: expected one argument
    # argparse will not accept a value that begins with "-", and EVERY value
    # forwarded here is itself a flag. So the watchdog exited 2 on its first line,
    # the session ran unsupervised for 3.7 hours, and when it stopped at 00:25
    # nothing restarted it. On a --paper night that cost nothing; on a live night
    # it is seven positions left open with no supervisor and no flush.

    for flags in (
        ["--paper"],
        ["--paper", "--max-daily-loss", "50"],
        ["--rule", "rsi_reversion", "--symbols", "EURUSD,GBPUSD"],
        ["--harvest-only"],
        [],
    ):
        argv = watchdog_argv(6, flags)
        try:
            parsed = build_parser().parse_args(argv)
            ok = parsed.session_arg == flags
        except SystemExit:
            ok = False
        check(f"watchdog parses the argv built for {' '.join(flags) or '(no flags)'}",
              ok, True)

    # The specific shape that failed: a value beginning with "-" must survive.
    argv = watchdog_argv(6, ["--paper"])
    check("the forwarded flag uses the = form, not a bare pair",
          "--session-arg=--paper" in argv, True)
    check("and the space form is not emitted at all",
          "--session-arg" in argv, False)

    # End to end from the operator's own command line, which is how it is called.
    forwarded = session_args_for(["--until-hour", "6", "--paper"])
    check("a --paper session forwards --paper and drops --until-hour",
          forwarded, ["--paper"])
    check("and that round-trips through the watchdog's parser",
          build_parser().parse_args(watchdog_argv(6, forwarded)).session_arg,
          ["--paper"])
    # --until-hour is the watchdog's own, passed once and not duplicated.
    check("--until-hour is passed exactly once",
          watchdog_argv(6, forwarded).count("--until-hour"), 1)
    check("and --adopt is always present, or it would start a SECOND session",
          "--adopt" in watchdog_argv(6, forwarded), True)

def test_the_guard_does_not_refuse_on_its_own_launcher():
    """A venv stub is this session, not a rival.

    On Windows `\.venv\Scripts\pythonw.exe` is a LAUNCHER: it spawns the base
    interpreter as a child and stays alive with a byte-identical command
    line. Measured 2026-09-21 - Start-Process reported pid 18944 for the stub
    while the Python ran as pid 11520, both visible to psutil. The guard
    excluded only os.getpid(), so a LIVE session scanned, found its own
    parent and refused to start, printing the pid the operator had just been
    handed.

    `--paper` masked it for months: a paper cmdline is filtered by the
    dry-run clause, so the stub never reached the pid check. Every unattended
    launch until tonight was a paper one.
    """
    print()
    print("The duplicate-session guard - a launcher stub is not a rival")
    import psutil

    me = psutil.Process()
    ancestors = [p.pid for p in me.parents()]
    check("this process has at least one ancestor to confuse it with",
          len(ancestors) >= 1, True)

    # Against the LIVE process table: the guard must never name this process
    # or one of its ancestors. Necessary but weak on its own - this process
    # runs test_crash_report.py, so its ancestors do not match
    # SESSION_SCRIPTS and would be skipped even by the broken version.
    mine = {me.pid, *ancestors}
    named = {pid for pid, _started, _cmd in run_overnight.other_sessions()}
    check("the guard never names this process or an ancestor of it",
          sorted(named & mine), [])

    # The check that actually bites, against a fabricated table. STUB is the
    # venv launcher: same cmdline as the session, different pid, and a
    # genuine ancestor. It must be excluded BY PID, because nothing else
    # about the row distinguishes it from a real rival.
    SESSION = "pythonw.exe tools/run_overnight.py --until-hour 6"
    stub, real, rival = 18944, 11520, 4242
    live = {real, stub}          # this process plus its launcher

    check("the launcher stub is not a rival, though its cmdline is identical",
          run_overnight.is_rival_session(stub, "pythonw.exe", SESSION, live), False)
    check("nor is this process itself",
          run_overnight.is_rival_session(real, "pythonw.exe", SESSION, live), False)
    check("but an unrelated live session with the SAME cmdline still is",
          run_overnight.is_rival_session(rival, "pythonw.exe", SESSION, live), True)

    # The two exclusions must hold independently. A paper run's stub was
    # filtered by the dry-run clause, which is why the pid bug went unseen
    # for months - so check the pid path with a cmdline that is NOT a paper
    # run, and the paper path with a pid that is NOT ours.
    PAPER = SESSION + " --paper"
    check("a paper run is not a session even from an unrelated pid",
          run_overnight.is_rival_session(rival, "pythonw.exe", PAPER, live), False)
    check("and a dry run is not either",
          run_overnight.is_rival_session(rival, "pythonw.exe",
                                         SESSION + " --dry-run", live), False)

    # Things that are not sessions at all.
    check("the watchdog is not a session",
          run_overnight.is_rival_session(
              rival, "pythonw.exe", "pythonw.exe tools/watchdog.py --adopt", live), False)
    check("a non-python process is not, whatever its command line says",
          run_overnight.is_rival_session(
              rival, "cmd.exe", SESSION, live), False)
    check("and an empty command line is not",
          run_overnight.is_rival_session(rival, "python.exe", "", live), False)


if __name__ == "__main__":
    raise SystemExit(main())
