"""Checks on cache pruning, run with plain python - no test framework needed.

    python tests/test_cache.py

prune_cache calls os.remove in a loop, which is the one operation in this
project that can destroy data rather than merely report it wrongly. The failure
that matters is an off-by-one in the age comparison quietly deleting entries
that are still live, so the boundary is pinned here rather than trusted.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import market  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<50} got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def seed(dirpath: str, name: str, age_days: float) -> str:
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}")
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


print("Pruning removes stale entries and nothing else")
with tempfile.TemporaryDirectory() as tmp:
    original = market.CACHE_DIR
    market.CACHE_DIR = tmp
    try:
        fresh = seed(tmp, "ohlcv.NVDA.3y.1d.20260823.json", age_days=0)
        stale = seed(tmp, "ohlcv.NVDA.3y.1d.20260101.json", age_days=30)
        edge = seed(tmp, "ohlcv.AMD.3y.1d.20260820.json", age_days=6.9)
        other = seed(tmp, "notes.txt", age_days=30)

        removed = market.prune_cache(max_age_days=7)

        check("the stale entry is removed", os.path.exists(stale), False)
        check("today's entry survives", os.path.exists(fresh), True)
        check("an entry just inside the window survives", os.path.exists(edge), True)
        check("a non-json file is left alone", os.path.exists(other), True)
        check("the removal count is reported", removed, 1)

        check("a second run has nothing left to do", market.prune_cache(max_age_days=7), 0)
    finally:
        market.CACHE_DIR = original

print("\nA missing cache directory is not an error")
original = market.CACHE_DIR
market.CACHE_DIR = os.path.join(tempfile.gettempdir(), "trade-research-cache-does-not-exist")
try:
    check("pruning an absent directory returns 0", market.prune_cache(), 0)
finally:
    market.CACHE_DIR = original

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}\n")
    raise SystemExit(1)
print("All cache checks passed.\n")
