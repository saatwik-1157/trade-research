"""Turn a closed console into a wind-down instead of a kill. **P1b.**

The measured failure this addresses: eight overnight sessions in a row ended
between one twenty-second tick and the next, with no traceback, no
KeyboardInterrupt, and the `finally` that releases the keep-awake hold never
reached. Nothing inside the program went wrong -- it was terminated from
outside, and the most likely outside is the console window being closed.

Windows delivers `CTRL_CLOSE_EVENT` to a process whose console is closing and
then kills it after a grace period. **A process with no handler installed gets
no grace at all.** Installing one converts the most likely cause of those
eight deaths from "killed mid-pass, positions left open" into "asked to stop,
ran the flush".

Three honest limits, because a guard that oversells itself is worse than none:

1. **The grace period is short and not ours.** Windows allows on the order of
   five seconds for `CTRL_CLOSE_EVENT` before terminating regardless, and the
   exact figure is policy, not contract. Closing seven positions may not fit.
   The guard therefore writes its record FIRST and attempts the flush second,
   so the evidence survives even when the wind-down does not.
2. **It cannot help against `taskkill /F`, a power loss, or a suspend.** No
   handler runs for those. `crash_report.beat()` is what covers them: the last
   heartbeat already on disk says when the session was alive and what it held.
3. **Closing the lid is not a console event.** The keep-awake hold does not
   defeat a lid, and neither does this.

The reliable protection is not to receive the event at all -- launch detached,
so the console owns no session to close. `start-trading.bat --detach` does
that, and this guard is what covers the launches that did not.
"""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Callable

#: Windows console control events.
CTRL_C = 0
CTRL_BREAK = 1
CTRL_CLOSE = 2
CTRL_LOGOFF = 5
CTRL_SHUTDOWN = 6

NAMES = {
    CTRL_C: "ctrl_c",
    CTRL_BREAK: "ctrl_break",
    CTRL_CLOSE: "console_closed",
    CTRL_LOGOFF: "logoff",
    CTRL_SHUTDOWN: "shutdown",
}

#: Held at module scope. ctypes callbacks are garbage collected like any other
#: object, and a collected console handler is silently uninstalled -- the
#: failure looks exactly like never having installed one.
_KEEP_ALIVE: list[object] = []

#: Set when a stop has been requested, so a second Ctrl-C can be told that the
#: wind-down is already running rather than starting a second one.
_STOPPING = threading.Event()


def stopping() -> bool:
    return _STOPPING.is_set()


def install(on_stop: Callable[[str], None]) -> str:
    """Ask for `on_stop(reason)` when the console or the OS says to stop.

    `on_stop` must be fast and must not block on a venue: it runs on the
    handler thread while the OS is counting down. Set a flag and let the
    session loop do the closing -- closing a position from a handler while the
    loop is mid-order is how one intention becomes two.

    Returns a short description of what was installed, for the session banner.
    Never raises: a session that cannot install a guard should still run.
    """

    def _fire(reason: str) -> None:
        if _STOPPING.is_set():
            return
        _STOPPING.set()
        try:
            on_stop(reason)
        except Exception:  # noqa: BLE001 - a guard never ends a session itself
            pass

    installed: list[str] = []

    if sys.platform == "win32":
        try:
            import ctypes

            proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def _handler(event: int) -> bool:
                if event in NAMES:
                    _fire(NAMES[event])
                    # True = handled. For CTRL_C and CTRL_BREAK this keeps the
                    # process alive so the loop can wind down. For CTRL_CLOSE
                    # Windows terminates us anyway once its timer expires;
                    # returning True only buys the interval.
                    return True
                return False

            cb = proto(_handler)
            _KEEP_ALIVE.append(cb)
            if ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True):
                installed.append("console")
        except Exception:  # noqa: BLE001
            pass

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda _s, _f, _n=name: _fire(_n.lower()))
            installed.append(name.lower())
        except Exception:  # noqa: BLE001 - not every signal exists everywhere
            continue

    return ", ".join(installed) if installed else "none (a kill will be abrupt)"
