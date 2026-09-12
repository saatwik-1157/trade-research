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


def main() -> int:
    print("cli dispatcher checks")
    test_the_project_root_survives_freezing()
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
