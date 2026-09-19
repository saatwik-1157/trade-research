"""Did the host stay awake? Read a session log and measure the gaps.

The harvest loop prints one `[HH:MM:SS] pass N:` line per interval, so a
session that ran without interruption leaves an unbroken ladder of them
roughly `--interval` seconds apart. **A GAP in that ladder is the failure**
this project has been chasing since the soak started producing nothing: the
machine slept, the loop stopped, and the log simply resumes later with no
error to explain it.

Until now reading that evidence meant eyeballing a 160 KB file, which is how
a gap gets missed and a session gets recorded as clean. This measures it.

**Timestamps carry no date.** A session runs across midnight, so a stamp that
goes backwards is read as the next day. That is right for every gap shorter
than 24 hours, and a session is bounded by its deadline long before then --
but it means this tool cannot distinguish a 23-hour gap from a 1-hour one,
and it says so rather than guessing.

**UNBROKEN is not the same as COMPLETED.** This measures gaps in what was
logged, and a session that died after twenty minutes has no gaps because it
has almost no log: `overnight-20260916-082522` reads UNBROKEN over 0.30h,
and what actually happened is that it lost the terminal and stopped. Pass
`--expect-hours` to have the span checked too, and read
`run_overnight.finishing_status` for whether the session met its
obligations. A clean ladder is necessary evidence, never sufficient.

Nothing here connects to a venue or reads an account. It parses a file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths as _paths  # noqa: E402

#: `[20:11:42] pass 3: harvested=0 opened=6 ...`
PASS = re.compile(r"^\s*\[(\d{2}):(\d{2}):(\d{2})\]\s+pass\s+(\d+)\b")

#: A gap is worth naming at this multiple of the expected interval. Three
#: rather than two: one slow pass is a venue that took its time answering,
#: and calling that a gap would make the measure useless by crying wolf.
GAP_FACTOR = 3.0
#: ...but never below this, because at a 20s interval 3x is a minute and a
#: minute of nothing is not evidence of sleep.
GAP_FLOOR_SECONDS = 120.0


def passes(text: str) -> list[tuple[datetime, int]]:
    """Every pass line, as (stamp, pass number), with midnight unwrapped."""
    out: list[tuple[datetime, int]] = []
    base = datetime(2000, 1, 1)
    day = 0
    previous: datetime | None = None
    for line in text.splitlines():
        m = PASS.match(line)
        if not m:
            continue
        h, mi, s, n = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        stamp = base + timedelta(days=day, hours=h, minutes=mi, seconds=s)
        if previous is not None and stamp < previous:
            day += 1
            stamp += timedelta(days=1)
        previous = stamp
        out.append((stamp, n))
    return out


def measure(text: str, interval: float, factor: float = GAP_FACTOR) -> dict:
    """The ladder, its gaps, and whether it is unbroken."""
    rungs = passes(text)
    if len(rungs) < 2:
        return {
            "passes": len(rungs),
            "verdict": "NO_DATA",
            "detail": "fewer than two pass lines; nothing to measure",
            "gaps": [],
        }

    threshold = max(interval * factor, GAP_FLOOR_SECONDS)
    deltas = []
    gaps = []
    for (t0, n0), (t1, n1) in zip(rungs, rungs[1:]):
        seconds = (t1 - t0).total_seconds()
        deltas.append(seconds)
        if seconds >= threshold:
            gaps.append({
                "after_pass": n0,
                "from": t0.strftime("%H:%M:%S"),
                "to": t1.strftime("%H:%M:%S"),
                "seconds": round(seconds, 1),
                "minutes": round(seconds / 60.0, 1),
            })

    ordered = sorted(deltas)
    span = (rungs[-1][0] - rungs[0][0]).total_seconds()
    # Time inside a gap is time the loop was not running. Reported as a share
    # of the session because "47 minutes lost" means different things in a
    # one-hour run and a ten-hour one.
    lost = sum(g["seconds"] for g in gaps)
    return {
        "passes": len(rungs),
        "first": rungs[0][0].strftime("%H:%M:%S"),
        "last": rungs[-1][0].strftime("%H:%M:%S"),
        "span_hours": round(span / 3600.0, 2),
        "interval_expected_seconds": interval,
        "interval_median_seconds": round(ordered[len(ordered) // 2], 1),
        "interval_max_seconds": round(ordered[-1], 1),
        "gap_threshold_seconds": round(threshold, 1),
        "gaps": gaps,
        "seconds_lost_to_gaps": round(lost, 1),
        "share_of_session_lost": round(lost / span, 4) if span else None,
        "verdict": "UNBROKEN" if not gaps else "GAPPED",
        "detail": (
            f"{len(rungs)} passes over {span / 3600.0:.2f}h with no gap over "
            f"{threshold:.0f}s"
            if not gaps
            else f"{len(gaps)} gap(s), the largest {max(g['minutes'] for g in gaps):.1f} min"
        ),
    }


def latest_log() -> str | None:
    logs = os.path.join(_paths.project_root(), "logs")
    if not os.path.isdir(logs):
        return None
    found = [
        os.path.join(logs, f)
        for f in os.listdir(logs)
        if f.startswith("overnight-") and f.endswith(".log")
    ]
    return max(found, key=os.path.getmtime) if found else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", help="session log; omit to use the newest")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="the pass interval the session was run with (default 20)")
    ap.add_argument("--expect-hours", type=float, default=None,
                    help="how long the session was meant to run; a span much "
                         "shorter than this is a session that STOPPED, which "
                         "an unbroken ladder does not rule out")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    path = args.log or latest_log()
    if not path or not os.path.exists(path):
        print("  no session log found in logs/")
        return 1

    with open(path, encoding="utf-8", errors="replace") as fh:
        report = measure(fh.read(), args.interval)
    report["log"] = os.path.basename(path)

    # A short session is a stopped session, and no gap can show that.
    short = None
    if args.expect_hours and report.get("span_hours") is not None:
        if report["span_hours"] < args.expect_hours * 0.9:
            short = (f"the log spans {report['span_hours']:.2f}h against the "
                     f"{args.expect_hours:g}h asked for, so it STOPPED early -- "
                     "an unbroken ladder does not rule that out")
            report["verdict"] = "STOPPED_EARLY"
    report["short"] = short

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["verdict"] != "GAPPED" else 1

    print()
    print(f"  {report['log']}")
    print("  " + "-" * 68)
    if report["verdict"] == "NO_DATA":
        print(f"  NO_DATA   {report['detail']}")
        print()
        return 1
    print(f"  passes            {report['passes']}  "
          f"({report['first']} -> {report['last']}, {report['span_hours']}h)")
    print(f"  interval          median {report['interval_median_seconds']}s, "
          f"max {report['interval_max_seconds']}s "
          f"(expected {report['interval_expected_seconds']:g}s)")
    print(f"  gap threshold     {report['gap_threshold_seconds']:g}s")
    for g in report["gaps"]:
        print(f"    GAP  {g['from']} -> {g['to']}  "
              f"{g['minutes']:.1f} min  (after pass {g['after_pass']})")
    print("  " + "-" * 68)
    print(f"  {report['verdict']}   {report['detail']}")
    if report.get("short"):
        print()
        print(f"  {report['short']}")
    if report["verdict"] == "GAPPED":
        print()
        print("  A gap is the loop not running. On this machine that has meant")
        print("  the host slept: an S0 standby the power request did not hold.")
        print(f"  {report['seconds_lost_to_gaps'] / 60.0:.1f} minutes of the session "
              f"were inside a gap.")
    print()
    return 0 if report["verdict"] == "UNBROKEN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
