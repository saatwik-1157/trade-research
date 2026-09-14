"""Checks on the single entry point, run with plain python - no test framework.

    python tests/test_cli.py

`cli.py` reaches its tools through `importlib.import_module` on a string, and
`build_exe.py` derives the executable's hidden imports from the same table. So
the table is load-bearing twice over, and both failures are quiet: a command
naming a module that does not exist is fine until someone types it, and a
module missing from the table is simply absent from the executable with no
build error at all.

These checks make both loud.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import cli

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<56} got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def test_every_command_resolves() -> None:
    print("\nEvery command names a module that imports and exposes main()")
    broken: list[str] = []
    for command, (module_name, _summary) in cli.COMMANDS.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - the point is to report any of them
            broken.append(f"{command} -> {module_name}: {type(exc).__name__}")
            continue
        if not callable(getattr(module, "main", None)):
            broken.append(f"{command} -> {module_name}: no main()")
    check("no command points at a missing module or missing main()", broken, [])
    check("the table is not empty", len(cli.COMMANDS) > 0, True)


def test_the_groups_and_the_table_agree() -> None:
    print("\nThe help text and the dispatch table describe the same commands")
    grouped: dict[str, tuple[str, str]] = {}
    for _title, group in cli.GROUPS:
        grouped.update(group)
    check("every grouped command is dispatchable",
          sorted(set(grouped) - set(cli.COMMANDS)), [])
    check("every dispatchable command is shown in the help",
          sorted(set(cli.COMMANDS) - set(grouped)), [])
    # A command listed in two groups would print twice and read as two tools.
    seen: list[str] = []
    for _title, group in cli.GROUPS:
        seen += list(group)
    check("no command appears in two groups", len(seen), len(set(seen)))


def test_the_usage_text_states_what_this_is_not() -> None:
    print("\nThe help does not let the reader infer an edge")
    usage = cli._usage().lower()
    check("it says plainly that no command finds profitable trades",
          "no command here finds profitable trades" in usage, True)
    check("it says the trading commands are demo-fenced in code",
          "demo" in usage, True)


def test_help_and_unknown_commands() -> None:
    print("\nArgument handling at the top level")
    check("no arguments prints the usage and succeeds", cli.main([]), 0)
    check("--help succeeds", cli.main(["--help"]), 0)
    # 2, not 1: a usage error is not the same as a tool that ran and failed.
    check("an unknown command is a usage error", cli.main(["no-such-command"]), 2)


def test_build_hidden_imports_cover_the_table() -> None:
    print("\nThe executable bundles every module the table can reach")
    sys.path.insert(0, ROOT)
    import build_exe

    hidden = set(build_exe.hidden_imports())
    referenced = {module for module, _summary in cli.COMMANDS.values()}
    missing = sorted(referenced - hidden)
    check("no command's module would be left out of the build", missing, [])


def test_the_project_root_survives_freezing() -> None:
    """The frozen executable must find the real `data/`, not a temp copy.

    This has been wrong twice, and neither way was loud. First `__file__`,
    which under PyInstaller points into an extraction directory deleted on
    exit -- so a merge read an empty ledger, reported "no order-log entry" for
    every trade, wrote its output into nothing and exited zero. Then
    `sys.executable`'s own directory, which is `dist/` and has no `data/` -- so
    it quietly started a SECOND ledger there and reported against that.

    A wrong answer here does not raise. It produces a plausible report about
    the wrong data, which is why this is asserted rather than assumed.
    """
    print("\nProject root resolution, frozen and not")
    import paths

    real = os.path.abspath(ROOT)
    check("unfrozen, it is the checkout", os.path.abspath(paths.project_root()), real)

    saved = (getattr(sys, "frozen", False), sys.executable, os.getcwd(),
             os.environ.get("TRADE_RESEARCH_ROOT"))
    try:
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = os.path.join(real, "dist", "trade-research.exe")
        os.environ.pop("TRADE_RESEARCH_ROOT", None)

        os.chdir(real)
        check("frozen, run from the checkout", os.path.abspath(paths.project_root()), real)

        # From somewhere unrelated it must walk up from the executable, past
        # `dist`, rather than inventing a root where the user happens to stand.
        os.chdir(os.path.dirname(real))
        check("frozen, run from elsewhere", os.path.abspath(paths.project_root()), real)

        os.environ["TRADE_RESEARCH_ROOT"] = real
        check("an explicit root always wins", os.path.abspath(paths.project_root()), real)
    finally:
        frozen, executable, cwd, override = saved
        if frozen:
            sys.frozen = True  # type: ignore[attr-defined]
        elif hasattr(sys, "frozen"):
            del sys.frozen  # type: ignore[attr-defined]
        sys.executable = executable
        os.chdir(cwd)
        os.environ.pop("TRADE_RESEARCH_ROOT", None)
        if override is not None:
            os.environ["TRADE_RESEARCH_ROOT"] = override


#: The call chain that made four modules wrong when frozen. `paths` is the one
#: place entitled to it, because it IS the answer to the question.
OWN_ROOT = "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"


def test_only_paths_derives_its_own_root() -> None:
    """No tool may compute the project root from its own `__file__`.

    The previous check proves `paths` is right. It says nothing about whether
    anyone USES it, and on 2026-09-14 four modules did not: `risk_gate`,
    `kill_switch`, `crash_report` and `cost_hurdle` each kept a private copy of
    the broken calculation. The suite was green throughout, because every test
    ran from a checkout where the broken and correct answers are identical.

    The failures were silent and expensive. `risk_gate` could not import the
    engine, so a live session refused all 27 of its entries and exited 0.
    `kill_switch` wrote STOP where no session was watching -- a stop that
    reported success and stopped nothing.

    So this is structural rather than behavioural: it asserts the mistake is
    not present in the source, which is the only form that catches the FIFTH
    module before it is written.
    """
    print("\nRoot resolution is not reimplemented anywhere")
    tools = os.path.join(ROOT, "tools")
    offenders = []
    for name in sorted(os.listdir(tools)):
        if not name.endswith(".py") or name == "paths.py":
            continue
        with open(os.path.join(tools, name), encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                bare = line.strip()
                if bare.startswith("#"):
                    continue  # a comment explaining the bug is not the bug
                if OWN_ROOT in bare:
                    offenders.append(f"{name}:{number}")
    check("no module outside paths.py derives its own root", offenders, [])


def test_the_frozen_root_reaches_its_callers() -> None:
    """The modules that need the root must get the RIGHT one when frozen.

    **`__file__` is moved, and that is the whole point.** Setting `sys.frozen`
    alone does not reproduce the bug: from a checkout,
    `dirname(dirname(kill_switch.__file__))` IS the real root, so the broken
    calculation and the correct one agree and the check passes either way. That
    accidental agreement is exactly why the suite stayed green for months while
    four modules were wrong, and a negative control caught this test making the
    same mistake on 2026-09-14.

    So the extraction directory is simulated too. Under PyInstaller `__file__`
    points into a temporary tree that is deleted on exit; here it points at one
    that never existed. Anything deriving a root from it now lands outside the
    checkout and fails, while `paths.project_root()` still finds the real one.
    """
    print("\nFrozen root, as the callers see it")
    import crash_report
    import kill_switch

    real = os.path.abspath(ROOT)
    meipass = os.path.join(tempfile.gettempdir(), "_MEI_not_a_real_checkout")
    saved = (getattr(sys, "frozen", False), sys.executable, os.getcwd(),
             os.environ.get("TRADE_RESEARCH_ROOT"),
             kill_switch.__file__, crash_report.__file__)
    try:
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = os.path.join(real, "dist", "trade-research", "trade-research.exe")
        os.environ.pop("TRADE_RESEARCH_ROOT", None)
        kill_switch.__file__ = os.path.join(meipass, "tools", "kill_switch.py")
        crash_report.__file__ = os.path.join(meipass, "tools", "crash_report.py")
        # Stand somewhere with no project, so a root taken from the working
        # directory would be visibly wrong rather than accidentally right.
        os.chdir(os.path.dirname(real))

        check("STOP sits in the checkout, not the extraction directory",
              os.path.abspath(kill_switch.path()),
              os.path.join(real, kill_switch.FILENAME))
        check("crash reports sit in the checkout, not the extraction directory",
              os.path.abspath(crash_report.directory()),
              os.path.join(real, crash_report.DIRNAME))
    finally:
        (frozen, executable, cwd, override,
         kill_file, crash_file) = saved
        if frozen:
            sys.frozen = True  # type: ignore[attr-defined]
        elif hasattr(sys, "frozen"):
            del sys.frozen  # type: ignore[attr-defined]
        sys.executable = executable
        kill_switch.__file__ = kill_file
        crash_report.__file__ = crash_file
        os.chdir(cwd)
        os.environ.pop("TRADE_RESEARCH_ROOT", None)
        if override is not None:
            os.environ["TRADE_RESEARCH_ROOT"] = override


def test_the_watchdog_recognises_a_frozen_session() -> None:
    """A frozen session must be visible to the watchdog that would replace it.

    Matching only on `take_profit.py` and `run_overnight.py` was blind to the
    frozen build, where the command line is `trade-research.exe overnight`.
    Blind in the dangerous direction: it reported no session while one was
    running, and the next restart would have put two harvest loops on one
    account.
    """
    print("\nWatchdog session detection")
    import watchdog

    cases = [
        ("trade-research.exe overnight --until-hour 6", True, "frozen session"),
        ("trade-research.exe harvest --minutes 60 --live", True, "frozen harvest"),
        ("trade-research.exe watchdog --until-hour 6", False, "the watchdog itself"),
        ("trade-research.exe paper --close-all --live", False, "a flush, not a session"),
        ("trade-research.exe track-record --merge", False, "read-only"),
        ("python tools/take_profit.py --live", True, "source session"),
        ("python tools/run_overnight.py --until-hour 6", True, "source overnight"),
        ("python tools/take_profit.py --dry-run", False, "a dry run trades nothing"),
        ("trade-research.exe harvest --minutes 1 --dry-run", False, "frozen dry run"),
    ]
    for cmdline, want, why in cases:
        check(f"{why}: {'a session' if want else 'not a session'}",
              watchdog._is_session(cmdline), want)


def test_every_check_in_this_file_runs() -> None:
    """`main()` names its checks by hand, so one can be written and forgotten.

    That has happened here before: a check was defined, never called, and the
    file reported "all checks passed" without running it. A test nobody runs is
    worse than no test, because it also buys false confidence.
    """
    print("\nEvery check is registered")
    import inspect

    body = inspect.getsource(main)
    missing = [name for name, obj in sorted(globals().items())
               if name.startswith("test_") and inspect.isfunction(obj)
               and f"{name}()" not in body]
    check("every test_ function is called by main", missing, [])


def main() -> int:
    print("cli dispatcher checks")
    test_the_project_root_survives_freezing()
    test_only_paths_derives_its_own_root()
    test_the_frozen_root_reaches_its_callers()
    test_the_watchdog_recognises_a_frozen_session()
    test_every_check_in_this_file_runs()
    test_every_command_resolves()
    test_the_groups_and_the_table_agree()
    test_the_usage_text_states_what_this_is_not()
    test_help_and_unknown_commands()
    test_build_hidden_imports_cover_the_table()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
