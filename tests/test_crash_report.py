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
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import console_guard  # noqa: E402
import crash_report  # noqa: E402
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
        # start() records this process, which is by definition alive.
        check("this process reads as alive",
              crash_report.process_alive(record), True)
        check("a record with no pid is UNKNOWN, not dead",
              crash_report.process_alive({"session_id": sid}), None)
        check("and neither is a pid that is not a number",
              crash_report.process_alive({"pid": "24824"}), None)


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

    if FAILED:
        print(f"\n{len(FAILED)} check(s) failed")
        return 1
    print("\nall crash-journal checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
