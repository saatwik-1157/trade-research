"""One way for the backend to reach the research toolkit.

Four modules used to do this for themselves -- `app/strategies/indicators.py`,
`app/backtest/runner.py`, `app/marketdata/providers/mt5.py` and
`.../yfinance.py` -- each computing the toolkit's location from its own
`__file__` and inserting it on `sys.path`. Every one of them was correct. That
is what made the arrangement worth replacing rather than leaving alone: the
four used three different nesting depths, two of them named the repository
root `root` and the other two gave that name to `backend/`, and nothing
imported any of them at start-up. A file moved one directory would have left
the mistake invisible until the first backtest ran.

The order below is the point of the module:

1. **An installed `tr_toolkit`** wins, because a declared dependency is the
   honest way to depend on something. `pip install -e ./tools`.
2. **The sibling directory** is the fallback, so a checkout that has not
   installed anything still works -- which is how every developer machine and
   the Docker image have run since L12, and breaking them to make a point
   about packaging would be a poor trade.

The fallback searches for a sibling `tools/` by walking up from here rather
than counting `dirname` calls, so it holds for both layouts that exist: the
repository (`<root>/backend`, `<root>/tools`) and the image (`/srv/backend`,
`/srv/tools`).

Nothing here imports the toolkit at module scope. The toolkit pulls in numpy
and, on some paths, MetaTrader5; a failure to import it must surface at the
call that needed it, not as an API that will not start.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from types import ModuleType

__all__ = ["ToolkitUnavailable", "load", "where"]


class ToolkitUnavailable(RuntimeError):
    """The toolkit could not be found or imported.

    Raised instead of returning None so that a caller cannot mistake an absent
    toolkit for an empty result -- the same reason `BacktestError` exists.
    """


@lru_cache(maxsize=1)
def _sibling_tools() -> str | None:
    """The in-repo `tools/` directory, or None if this is not that layout."""
    here = os.path.dirname(os.path.abspath(__file__))
    # app/core -> app -> backend -> the directory holding both. Four parents is
    # further than any real layout needs; stopping at the filesystem root is
    # what actually ends the loop.
    for _ in range(5):
        parent = os.path.dirname(here)
        if parent == here:
            return None
        candidate = os.path.join(parent, "tools")
        if os.path.isdir(candidate):
            return candidate
        here = parent
    return None


def where() -> str:
    """Where the toolkit is being loaded from. For diagnostics and health.

    Returns the installed package's directory when one is installed, the
    sibling path when the fallback is in use, and the string ``"unavailable"``
    when neither -- never an empty string, which reads as "here".
    """
    try:
        import tr_toolkit
    except ImportError:
        pass
    else:
        path = getattr(tr_toolkit, "__file__", None)
        if path:
            return os.path.dirname(os.path.abspath(path))
    sibling = _sibling_tools()
    return sibling if sibling else "unavailable"


def load(name: str) -> ModuleType:
    """Import one toolkit module by its flat name, e.g. ``"rule_backtest"``.

    `name` is always a literal at the call sites. It is validated anyway,
    because `importlib` on an attacker-chosen string is an import gadget and
    the check costs nothing.
    """
    if not name.isidentifier():
        raise ToolkitUnavailable(f"not a module name: {name!r}")

    try:
        import tr_toolkit
    except ImportError:
        pass
    else:
        try:
            return tr_toolkit.load(name)
        except (AttributeError, ImportError) as exc:
            raise ToolkitUnavailable(
                f"the installed tr_toolkit could not provide {name!r}: {exc}"
            ) from exc

    tools = _sibling_tools()
    if tools is None:
        raise ToolkitUnavailable(
            f"cannot import {name!r}: tr_toolkit is not installed and no sibling "
            "tools/ directory was found. Run `pip install -e ./tools`."
        )
    if tools not in sys.path:
        sys.path.append(tools)

    import importlib

    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ToolkitUnavailable(f"cannot import {name!r} from {tools}: {exc}") from exc
