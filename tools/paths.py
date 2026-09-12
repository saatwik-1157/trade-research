#!/usr/bin/env python3
"""Where `data/` and `reports/` live, answered once.

Every tool used to derive this from its own `__file__`, which is correct from a
checkout and **silently wrong inside the frozen executable**: PyInstaller
extracts the bundle to a temporary directory, sets `__file__` under it, and
deletes it on exit. So `track-record --merge` read an empty ledger, reported
"524 trades have no order-log entry", wrote a new ledger into the temp
directory and exited zero. Nothing failed. The numbers were simply computed
against nothing, which is the worst way for this to go wrong -- a crash would
have been kinder than a plausible report.

"Beside the executable" is not the answer either, and that was the first
attempt: the exe lives in `dist/`, which has no `data/`. So the root is
*searched for* rather than assumed -- upward from the working directory, then
upward from the executable, looking for a directory that actually holds the
project's data. Falling back to the working directory means a fresh checkout
with no `data/` yet still writes somewhere sensible.
"""

from __future__ import annotations

import os
import sys

#: A directory is the project root if it holds any of these. `tools` is
#: included so a checkout that has not written any data yet is still found.
MARKERS = ("data", "reports", "tools")


def _looks_like_root(path: str) -> bool:
    return any(os.path.isdir(os.path.join(path, marker)) for marker in MARKERS)


def _search_upward(start: str) -> str | None:
    """The nearest ancestor of `start` (inclusive) that looks like the root."""
    current = os.path.abspath(start)
    while True:
        if _looks_like_root(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:  # reached the drive root
            return None
        current = parent


def project_root() -> str:
    """The directory that holds `data/` and `reports/`."""
    override = os.environ.get("TRADE_RESEARCH_ROOT")
    if override:
        return os.path.abspath(override)

    if getattr(sys, "frozen", False):
        # Where the user is stands first: running the exe from a project
        # directory should act on THAT project's data.
        found = _search_upward(os.getcwd())
        if found:
            return found
        # Then upward from the executable, so `dist/trade-research.exe`
        # finds the checkout it was built in rather than `dist` itself.
        found = _search_upward(os.path.dirname(os.path.abspath(sys.executable)))
        return found or os.getcwd()

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_path(*parts: str) -> str:
    """A path under `data/`, created lazily by whoever writes to it."""
    return os.path.join(project_root(), "data", *parts)


def reports_path(*parts: str) -> str:
    """A path under `reports/`."""
    return os.path.join(project_root(), "reports", *parts)
