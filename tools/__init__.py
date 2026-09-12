"""The research toolkit, importable from outside this directory.

Every script here is also a command -- `python tools/snapshot.py NVDA` is the
documented way to run one, and each module puts its own directory on
`sys.path` at import time so that its flat imports (`import indicators`,
`from swap import nights_between`) resolve whatever the working directory is.
That arrangement works and is not being changed: the commands in README.md,
CLAUDE.md and start-trading.bat all depend on it.

What it does NOT give you is a way to depend on the toolkit from somewhere
else. Before this file existed the backend reached in by computing a relative
path and inserting it on `sys.path` -- four times, in four modules, with three
different nesting depths and a variable named `root` that meant `backend/` in
two of them and the repository root in the other two. All four were correct,
which is the problem: nothing tested them, and the first file to move would
have broken one silently at call time rather than at import.

So the flat namespace stays, and this adds a second door to the same rooms:

    from tr_toolkit import rule_backtest     # from anywhere, once installed
    import rule_backtest                     # from a tool, as before

Both reach the same file. `__getattr__` imports the FLAT module and registers
it under the dotted name as well, so the two spellings return the same object
rather than two copies of it -- a toolkit imported twice would run
`market.py`'s cache sweep twice and keep two of every module-level table, and
a bug that shape does not announce itself.

The one spelling that is not supported is `import tr_toolkit.rule_backtest`.
That form goes to the import machinery directly, never reaches `__getattr__`,
and would produce the second copy this file exists to prevent -- unless the
module was already loaded, in which case it quietly agrees. Use the
`from tr_toolkit import ...` form; `test_toolkit_package.py` asserts the
identity that makes it safe.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

__all__ = ["load"]

#: This directory. The flat imports inside every tool resolve against it.
_HERE = os.path.dirname(os.path.abspath(__file__))

if _HERE not in sys.path:
    # Appended, not inserted at 0: an installed toolkit must not be able to
    # shadow a stdlib or site-packages module for the whole process just
    # because a tool here happens to share its name. The tools themselves
    # still insert at 0, because for a script that IS the intent.
    sys.path.append(_HERE)


def load(name: str) -> ModuleType:
    """Import one toolkit module by its flat name.

    The single entry point, so that a caller outside this directory never has
    to know where the files are. `name` is a module name, never a path.
    """
    if not name.isidentifier():
        raise ValueError(f"not a module name: {name!r}")
    module = importlib.import_module(name)
    # Register the dotted spelling too, so a later `import tr_toolkit.<name>`
    # finds this object in sys.modules instead of loading a second copy.
    sys.modules[f"{__name__}.{name}"] = module
    return module


def __getattr__(name: str) -> ModuleType:
    """`from tr_toolkit import rule_backtest`, resolved lazily.

    Lazy because the toolkit's dependencies are not uniform: importing every
    module eagerly would make `from tr_toolkit import market` require
    MetaTrader5, which is Windows-only and optional.
    """
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        return load(name)
    except ModuleNotFoundError as exc:
        # A missing OPTIONAL dependency of a real tool must not be reported as
        # "no such tool". `mt5_paper` exists; MetaTrader5 may not be installed.
        if exc.name and exc.name != name:
            raise
        raise AttributeError(f"no toolkit module named {name!r}") from exc
