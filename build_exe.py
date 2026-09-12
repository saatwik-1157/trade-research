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
    ap.add_argument("--onedir", action="store_true",
                    help="a directory rather than one file; starts faster")
    args = ap.parse_args()

    modules = hidden_imports()
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir" if args.onedir else "--onefile",
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

    built = os.path.join(ROOT, "dist", NAME + (".exe" if os.name == "nt" else ""))
    if not args.onedir and not os.path.exists(built):
        print(f"PyInstaller reported success but {built} is not there", file=sys.stderr)
        return 1
    if os.path.exists(built):
        print(f"built {built}  ({os.path.getsize(built) / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
