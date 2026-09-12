#!/usr/bin/env python3
"""Freeze the toolkit into a single `trade-research.exe`.

    python build_exe.py            # build
    python build_exe.py --clean    # build, discarding the previous work tree

The hidden imports are derived from `tools/cli.py`'s own command table rather
than listed here. `cli` reaches its tools through `importlib.import_module`,
which PyInstaller's static analysis cannot follow, so every module behind a
command has to be named explicitly -- and a second hand-written list would
drift the first time a command was added. Deriving it means a new command is
bundled by having been registered, which is the only place it can be forgotten.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(ROOT, "tools")
NAME = "trade-research"

# Imported lazily inside functions, or reached only through a string, so the
# analysis does not see them either.
EXTRA_HIDDEN = (
    "MetaTrader5",
    "numpy",
    "pandas",
    "requests",
    "psutil",
)


def hidden_imports() -> list[str]:
    sys.path.insert(0, TOOLS)
    import cli

    modules = sorted({module for module, _summary in cli.COMMANDS.values()})
    return modules + [m for m in EXTRA_HIDDEN if m not in modules]


def main() -> int:
    ap = argparse.ArgumentParser(description="Freeze the toolkit into one executable.")
    ap.add_argument("--clean", action="store_true", help="discard the previous build tree")
    # A directory by default, because onefile unpacks the whole bundle to a
    # temporary directory on EVERY invocation and deletes it afterwards.
    # Measured on this machine, `--help` alone:
    #
    #     onefile   5,400 ms     38 MB on disk
    #     onedir    1,000 ms     71 MB on disk
    #     source      410 ms
    #
    # Five seconds per command is not a startup cost, it is a different tool.
    # 33 MB buys it back. `--onefile` stays available for when one portable
    # file matters more than speed -- copying it to another machine, say.
    ap.add_argument("--onefile", action="store_true",
                    help="a single portable file instead of a directory; ~5x slower to start")
    args = ap.parse_args()

    modules = hidden_imports()
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile" if args.onefile else "--onedir",
        "--console",
        "--name", NAME,
        "--paths", TOOLS,
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
    ]
    if args.clean:
        command.append("--clean")
    for module in modules:
        command += ["--hidden-import", module]
    command.append(os.path.join(TOOLS, "cli.py"))

    print(f"freezing {len(modules)} modules into {NAME}")
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        return completed.returncode

    # Where the executable lands differs by mode, and checking the wrong path
    # would report a build that did not happen as a success.
    exe = NAME + (".exe" if os.name == "nt" else "")
    built = (os.path.join(ROOT, "dist", exe) if args.onefile
             else os.path.join(ROOT, "dist", NAME, exe))
    if not os.path.exists(built):
        print(f"PyInstaller reported success but {built} is not there", file=sys.stderr)
        return 1

    size = os.path.getsize(built) / 1_048_576
    print(f"built {built}  ({size:.1f} MB)")
    if not args.onefile:
        print("  run it from this directory; the files beside it are the bundle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
