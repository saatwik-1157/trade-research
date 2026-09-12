"""What the session was doing when it stopped. **P1b.**

Eight consecutive overnight sessions ended before their deadline and not one
left a record of why. The logs stop mid-pass, between one twenty-second tick
and the next, with no final line -- no traceback, no KeyboardInterrupt, and
the `finally` that releases the keep-awake hold never printed. That is what an
externally terminated process looks like, and it is unfalsifiable from the
outside: a closed console, a `taskkill`, a machine that suspended and an OOM
kill are indistinguishable once the process is gone.

So this writes the evidence *before* it is needed, rather than trying to
capture it at the moment of death:

* **A heartbeat.** `beat()` rewrites one small JSON file every pass with the
  session's last known state -- the pass number, the balance, what is open,
  when it last spoke. A hard kill cannot prevent a write that already
  happened, so even SIGKILL leaves a file whose `stopped_at` is the last
  moment the session was alive and whose `positions_open` is the tail it
  left behind.
* **An exit record.** `finish()` stamps the same file with a reason when the
  session ends in a way it can observe.

A file per session, never appended to a shared log: a session that dies
mid-write must not be able to corrupt the record of the one before it.

**This module never raises into the session.** A crash journal that can end a
trading session is worse than no crash journal -- it converts a disk problem
into an open position. Every public function swallows its own errors and
reports through the return value.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

#: Directory holding one file per session. Beside the code rather than in the
#: log directory, because `logs/` is swept and this is the record that says
#: what a swept log was doing.
DIRNAME = "CRASH_REPORTS"

#: A session that has not beaten in this long was almost certainly killed
#: rather than stopped. Read by `unfinished()`, never enforced here.
STALE_SECONDS = 120.0


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def directory() -> str:
    return os.path.join(_root(), DIRNAME)


def _path(session_id: str) -> str:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return os.path.join(directory(), f"session-{safe}.json")


def _write(path: str, payload: dict[str, Any]) -> bool:
    """Atomic replace, so a kill mid-write cannot truncate the record.

    `os.replace` is atomic on Windows and POSIX alike. Writing in place would
    make the one file that explains a crash the file most likely to be
    destroyed by it.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:  # noqa: BLE001 - never raise into a trading session
        return False


def _read(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def new_session_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("%Y%m%d-%H%M%S")


def start(
    session_id: str,
    *,
    command: list[str] | None = None,
    log_path: str | None = None,
    account: str | None = None,
    deadline: str | None = None,
    live: bool = False,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Open the record. Called once, before the first pass."""
    payload: dict[str, Any] = {
        "session_id": session_id,
        "schema": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stopped_at": None,
        "status": "running",
        "reason": None,
        "command": command or [],
        "log_path": log_path,
        "account": account,
        "deadline": deadline,
        "live": live,
        "pid": os.getpid(),
        "passes": 0,
        "state": {},
    }
    if extra:
        payload.update(extra)
    return _write(_path(session_id), payload)


def beat(session_id: str, passes: int, state: dict[str, Any] | None = None) -> bool:
    """Rewrite the record with the session's last known state.

    Called every pass. `stopped_at` is advanced on each beat, so a record that
    was never `finish()`ed still says when the session was last alive -- which
    is the only timestamp a killed process can leave.
    """
    path = _path(session_id)
    payload = _read(path) or {"session_id": session_id, "schema": 1}
    payload["passes"] = passes
    payload["stopped_at"] = datetime.now(timezone.utc).isoformat()
    payload["status"] = "running"
    if state:
        payload["state"] = state
    return _write(path, payload)


def finish(
    session_id: str,
    *,
    status: str,
    reason: str = "",
    state: dict[str, Any] | None = None,
) -> bool:
    """Stamp the record with how the session ended.

    `status` is one of: `completed`, `halted`, `interrupted`, `console_closed`,
    `error`. A record still reading `running` after the process is gone is the
    interesting case -- see `unfinished()`.
    """
    path = _path(session_id)
    payload = _read(path) or {"session_id": session_id, "schema": 1}
    payload["status"] = status
    payload["reason"] = reason
    payload["stopped_at"] = datetime.now(timezone.utc).isoformat()
    if state:
        payload["state"] = state
    return _write(path, payload)


def unfinished(now: datetime | None = None) -> list[dict[str, Any]]:
    """Records still reading `running`, newest first.

    These are the sessions that did not get to say goodbye. Each one names the
    positions it last saw open, which is the tail a later `--harvest-only` run
    has to close.
    """
    out: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(directory()), reverse=True)
    except OSError:
        return out
    for name in names:
        if not (name.startswith("session-") and name.endswith(".json")):
            continue
        data = _read(os.path.join(directory(), name))
        if data and data.get("status") == "running":
            out.append(data)
    return out


def summarise(record: dict[str, Any]) -> str:
    """One human line. Used by the banner a new session prints."""
    state = record.get("state") or {}
    opened = state.get("positions_open")
    where = record.get("log_path") or "no log recorded"
    return (
        f"session {record.get('session_id')} stopped at "
        f"{record.get('stopped_at')} after {record.get('passes')} passes "
        f"with positions_open={opened if opened is not None else 'UNKNOWN'} "
        f"({where})"
    )


def process_alive(record: dict[str, Any]) -> bool | None:
    """Is the process this record belongs to still running? None if unknowable.

    A record reading `running` says only that nothing ever wrote an ending to
    it. Whether the session is ALIVE is a different question, and the answer
    changes what the operator should do: a live one must not be started
    alongside, while a dead one is a notice about a tail that may still be
    open. The banner said neither, so a record from a session killed days ago
    read exactly like one still trading.

    None rather than False when psutil is absent or the pid is missing.
    An unknowable answer is not a negative one.
    """
    pid = record.get("pid")
    if not isinstance(pid, int):
        return None
    try:
        import psutil
    except Exception:  # noqa: BLE001 - absent psutil is a gap, not a dead process
        return None
    try:
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return None


def resolve(session_id: str, note: str = "") -> bool:
    """Stamp a stuck `running` record `abandoned`. The operator's answer to it.

    **Why this exists.** `unfinished()` is permanent. A session killed from
    outside leaves `running` behind forever, so the warning it produces prints
    on every launch from then on, long after its positions were closed. A
    warning that never clears stops being read, and the one it will be
    confused with is a real abandoned tail.

    **Stamped, not deleted.** The record is the only evidence that session
    existed, and `abandoned` is true where `running` is a lie about a process
    that is gone. What it does not claim is that the tail was closed; it
    records that a person said so, and when.

    Refuses a record that is not `running`, so a finished session cannot be
    rewritten by a typo in a session id.
    """
    path = _path(session_id)
    payload = _read(path)
    if not payload or payload.get("status") != "running":
        return False
    payload["status"] = "abandoned"
    payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
    payload["resolved_note"] = note or "acknowledged by the operator"
    return _write(path, payload)


def main() -> int:
    """List the stuck records, or resolve one by id."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Sessions that never wrote an ending, and how to clear them.")
    ap.add_argument("--resolve", metavar="SESSION_ID", default=None,
                    help="stamp that record `abandoned`; use once its positions "
                         "are closed")
    ap.add_argument("--note", default="", help="why, recorded beside it")
    args = ap.parse_args()

    if args.resolve:
        if resolve(args.resolve, args.note):
            print(f"  {args.resolve}: resolved")
            return 0
        print(f"  {args.resolve}: no record reading `running` under that id. "
              "Nothing was changed.")
        return 1

    stuck = unfinished()
    if not stuck:
        print("  no unfinished sessions")
        return 0
    for record in stuck:
        alive = process_alive(record)
        state = {True: "STILL RUNNING", False: "process is gone",
                 None: "liveness unknown"}[alive]
        print(f"\n  {summarise(record)}")
        print(f"    pid {record.get('pid')}: {state}")
        if alive is not True:
            print(f"    clear it with: python tools/crash_report.py "
                  f"--resolve {record.get('session_id')}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
