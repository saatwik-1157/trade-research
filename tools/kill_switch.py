"""Stop a running session without hunting for its process. **P2.**

The platform has three kill-switch scopes -- global, per account, per strategy
-- and the RiskEngine refuses an order when any is engaged. The harness in
`take_profit.py` has none, and that gap is the practical half of the finding
that it reaches a broker while importing nothing from `app/`.

The gap has teeth once a session is detached. `start-trading.bat --detach`
runs under `pythonw` with no console, so an operator who wants it to stop has
to find a PID and `taskkill` it -- which is precisely the abrupt termination
that left eight sessions' losing tails open. The only available stop was the
one that skips the flush.

So: a file. Create it and the session winds down at its next pass, running the
same `--flat-by` flush it would have run at its deadline.

    python tools/kill_switch.py on  --reason "spread blew out"
    python tools/kill_switch.py off
    python tools/kill_switch.py           # report

**A file rather than a signal**, for three reasons. A detached process has no
console to signal. Anyone can create a file -- no PID, no admin rights, no
Python. And it survives a restart, so a session relaunched by a supervisor
into a halted state stops again instead of resuming trading.

**Presence is the whole condition.** The contents are a human note recorded
beside the stop; an empty or unreadable file still stops the session. A stop
that depended on parsing its own reason would fail exactly when the disk is
the thing going wrong.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import paths as _paths

#: Repository root, beside `CRASH_REPORTS/`. Not inside `logs/`, which is
#: swept, and not in a temp directory, which does not survive a reboot.
FILENAME = "STOP"


def _root() -> str:
    """The project root, via `paths` rather than this file's own location.

    Deriving it from `__file__` was wrong inside the frozen executable, and
    wrong in the way that matters most for a kill switch: `__file__` sits under
    PyInstaller's extraction directory, so `engage()` wrote STOP where no
    running session was watching and `engaged()` read a path that never had it.
    The switch would report success and stop nothing.

    Observed 2026-09-14: `kill` answered "not engaged (no
    dist/trade-research/STOP)" while a session was live.
    """
    return _paths.project_root()


def path() -> str:
    return os.path.join(_root(), FILENAME)


def engaged() -> bool:
    """Whether a stop has been asked for. Never raises.

    An error reading the file is NOT treated as "no stop". The file's presence
    is what matters and `os.path.exists` is the whole check; the read that
    follows is only for the note.
    """
    try:
        return os.path.exists(path())
    except OSError:
        # Cannot tell. Do not invent a permission to keep trading.
        return True


def reason() -> str:
    """The note left beside the stop, or a default. Never raises."""
    try:
        with open(path(), encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
        return text or "kill switch engaged (no reason recorded)"
    except OSError:
        return "kill switch engaged (file present, unreadable)"


def engage(note: str = "") -> str:
    """Ask every running session to wind down. Returns the file path."""
    p = path()
    stamp = datetime.now(timezone.utc).isoformat()
    body = f"{stamp}\n{note}\n" if note else f"{stamp}\n"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    return p


def release() -> bool:
    """Remove the stop. Returns True if a file was actually removed."""
    try:
        os.unlink(path())
        return True
    except FileNotFoundError:
        return False


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", nargs="?", default="status",
                    choices=["on", "off", "status"])
    ap.add_argument("--reason", default="", help="a note recorded beside the stop")
    args = ap.parse_args()

    if args.action == "on":
        p = engage(args.reason)
        print(f"  kill switch ENGAGED: {p}")
        print("  Running sessions wind down at their next pass and run the")
        print("  --flat-by flush. Nothing new opens. Remove it with:")
        print("      python tools/kill_switch.py off")
        return 0

    if args.action == "off":
        if release():
            print("  kill switch released; new sessions may start")
        else:
            print("  kill switch was not engaged")
        return 0

    if engaged():
        print(f"  ENGAGED: {path()}")
        print(f"  reason:  {reason()}")
        return 1
    print(f"  not engaged (no {path()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
