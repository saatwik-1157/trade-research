"""The gap measure: did the host stay awake?

The evidence for Critical 1 is a session log's ladder of `pass N` lines, and
until this tool existed reading it meant eyeballing a 160 KB file. Run this
after touching `tools/session_gaps.py`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import session_gaps  # noqa: E402

FAILURES: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<58} got={got} want={want}")
    if not ok:
        FAILURES.append(label)


def ladder(start_h: int, start_m: int, count: int, step: int = 20, jump_after=None, jump_s=0):
    """A synthetic pass log. `jump_after` inserts a gap of `jump_s` seconds."""
    lines, t = [], start_h * 3600 + start_m * 60
    for n in range(1, count + 1):
        h, rem = divmod(t % 86400, 3600)
        m, s = divmod(rem, 60)
        lines.append(f"  [{h:02d}:{m:02d}:{s:02d}] pass {n}: harvested=0 opened=0")
        t += jump_s if (jump_after is not None and n == jump_after) else step
    return "\n".join(lines)


def test_an_unbroken_ladder_is_unbroken():
    print()
    print("a session that ran without interruption")
    got = session_gaps.measure(ladder(20, 0, 200), interval=20)
    check("every pass is counted", got["passes"], 200)
    check("no gaps", got["gaps"], [])
    check("the verdict", got["verdict"], "UNBROKEN")
    check("the median interval is the one asked for", got["interval_median_seconds"], 20.0)


def test_a_sleep_shows_up_as_a_gap():
    """112 minutes is what `overnight-20260915-201059` actually lost."""
    print()
    print("a host that slept leaves a hole in the ladder")
    text = ladder(20, 0, 100, jump_after=50, jump_s=112 * 60)
    got = session_gaps.measure(text, interval=20)
    check("the verdict", got["verdict"], "GAPPED")
    check("one gap", len(got["gaps"]), 1)
    check("it is named after the pass it followed", got["gaps"][0]["after_pass"], 50)
    check("and measured in minutes", got["gaps"][0]["minutes"], 112.0)


def test_one_slow_pass_is_not_a_gap():
    """A venue that took its time is not the machine sleeping. Crying wolf
    on a single slow pass would make the measure useless."""
    print()
    print("a slow pass is not a sleep")
    text = ladder(20, 0, 50, jump_after=25, jump_s=61)
    got = session_gaps.measure(text, interval=20)
    check("61s at a 20s interval is tolerated", got["verdict"], "UNBROKEN")
    check("but it is visible in the max", got["interval_max_seconds"], 61.0)
    # ...and the floor is what tolerates it: 3 x 20s is 60s, under the 120s floor.
    check("the threshold is the floor, not 3x", got["gap_threshold_seconds"], 120.0)


def test_midnight_does_not_look_like_a_gap():
    """**The trap.** Timestamps carry no date, so 23:59:40 -> 00:00:00 goes
    backwards. Read naively that is a negative interval; read as a new day it
    is 20 seconds. Every overnight session crosses this."""
    print()
    print("a session crossing midnight")
    text = ladder(23, 55, 60)          # 20 minutes of passes, straddling 00:00
    got = session_gaps.measure(text, interval=20)
    check("no gap is invented at midnight", got["verdict"], "UNBROKEN")
    check("the span is continuous", got["span_hours"], round(59 * 20 / 3600.0, 2))
    check("and the last stamp is after midnight", got["last"].startswith("00:"), True)


def test_a_log_with_nothing_to_measure_says_so():
    print()
    print("an empty or truncated log")
    check("no pass lines at all", session_gaps.measure("", 20)["verdict"], "NO_DATA")
    check("one pass is not a ladder",
          session_gaps.measure(ladder(20, 0, 1), 20)["verdict"], "NO_DATA")
    check("and it is not reported as clean",
          session_gaps.measure("", 20)["gaps"], [])


def test_the_real_logs_parse():
    """Against the files actually on disk, when they are there. The parser
    matching nothing would report every session UNBROKEN, which is the one
    failure that looks like success."""
    print()
    print("the sessions this machine has actually run")
    import glob

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logs = sorted(glob.glob(os.path.join(root, "logs", "overnight-*.log")))
    if not logs:
        print("    (no session logs on this machine; skipped)")
        return
    parsed = 0
    for path in logs:
        with open(path, encoding="utf-8", errors="replace") as fh:
            if session_gaps.measure(fh.read(), 20)["passes"] > 1:
                parsed += 1
    check("at least one log yields a ladder", parsed > 0, True)


def main():
    test_an_unbroken_ladder_is_unbroken()
    test_a_sleep_shows_up_as_a_gap()
    test_one_slow_pass_is_not_a_gap()
    test_midnight_does_not_look_like_a_gap()
    test_a_log_with_nothing_to_measure_says_so()
    test_the_real_logs_parse()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all session-gap checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
