"""Checks on the sourcing gate, run with plain python - no test framework needed.

    python tests/test_verify.py

verify.py is what turns "only state figures the data layer produced" from an
instruction into a test. That makes the gate itself load-bearing: if it stops
flagging invented numbers, nothing downstream notices, because a fabricated
figure reads exactly like a real one. These checks pin the three outcomes the
gate has to keep separating - computed, cited, and invented - against a fixture
snapshot so they run without network access.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "tests", "sourcing_report.md")
SNAPSHOT = os.path.join(ROOT, "tests", "fixture.snapshot.json")

FAILURES: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<52} got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def run(*extra):
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "verify.py"), REPORT, SNAPSHOT, *extra],
        capture_output=True, text=True,
    )
    return proc


print("The gate separates computed, cited and invented figures")
result = json.loads(run("--json").stdout)

tokens = {c["token"] for c in result["verified_detail"]}
check("a snapshot figure verifies", "$214.72" in tokens, True)
check("it names the field it came from",
      result["verified_detail"][0]["source_field"], "technical.price")

sourced = {c["token"] for c in result["externally_sourced_detail"]}
check("a figure cited inline is not flagged", "$1.658B" in sourced, True)
check("a percentage on the cited line is not flagged", "27%" in sourced, True)

unverified = {c["token"] for c in result["unverified"]}
check("an invented figure is flagged", "$340B" in unverified, True)
check("only the invented figure is flagged", len(result["unverified"]), 1)

print("\nExit codes gate the pipeline")
check("without --strict the run reports and exits 0", run("--json").returncode, 0)
check("with --strict an unverified figure exits non-zero",
      run("--json", "--strict").returncode != 0, True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}\n")
    raise SystemExit(1)
print("All sourcing-gate checks passed.\n")
