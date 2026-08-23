"""Checks on SEC period alignment, run with plain python - no test framework needed.

    python tests/test_edgar.py

CLAUDE.md names mismatched fiscal periods as the known trap here, and it is the
right one to name: a ratio built from a numerator and denominator that cover
different windows is not obviously wrong on the page. It is a plausible number,
which is the failure this project exists to prevent.

Two behaviours carry that weight. _annual_facts has to keep quarterly figures
out of the annual series - a 10-K reports Q4 alongside the full year, and both
are tagged the same way, so only the period length separates them. _nearest has
to tolerate the few days that 52/53-week calendars drift without silently
reaching across a whole quarter.

The SEC response shape is faked here so these run offline and without burning
the rate limit.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import edgar  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<54} got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def fact(start, end, val, filed="2026-01-01", form="10-K"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form,
            "fy": 2025, "fp": "FY", "accn": "0000-00-000000"}


def fake_sec(payloads: dict):
    """Stand in for _get, serving one canned companyconcept body per tag."""
    def _get(url, cache_key, max_age_s=None):
        for tag, body in payloads.items():
            if f"/{tag}.json" in url:
                return body
        return None
    return _get


def concept(rows, unit="USD"):
    return {"units": {unit: rows}}


ORIGINAL_GET = edgar._get


print("Quarterly figures stay out of the annual series")
edgar._get = fake_sec({"Revenues": concept([
    fact("2024-09-29", "2025-09-27", 400_000),   # full year, 363 days
    fact("2025-06-28", "2025-09-27", 100_000),   # Q4 from the same 10-K, 91 days
])})
facts = edgar._annual_facts("0000320193", ["Revenues"], "duration")
check("the annual period is kept", "2025-09-27" in facts, True)
check("the annual value is the full year, not Q4", facts["2025-09-27"]["value"], 400_000)
check("only one period survives", len(facts), 1)

edgar._get = fake_sec({"Revenues": concept([
    fact("2025-01-01", "2025-04-01", 90_000),    # a lone quarter, 90 days
    fact("2023-01-01", "2025-01-01", 800_000),   # two years, 731 days
])})
check("a bare quarter is excluded", len(edgar._annual_facts("x", ["Revenues"], "duration")), 0)

print("\nInstants and durations are not mixed")
edgar._get = fake_sec({"Assets": concept([
    {"start": None, "end": "2025-09-27", "val": 350_000, "filed": "2026-01-01"},
    fact("2024-09-29", "2025-09-27", 400_000),   # a duration row in the same concept
])})
instants = edgar._annual_facts("x", ["Assets"], "instant")
check("the instant is kept", instants["2025-09-27"]["value"], 350_000)
check("the duration row is dropped", len(instants), 1)

print("\nRestatements and tag preference resolve deterministically")
edgar._get = fake_sec({"Revenues": concept([
    fact("2024-09-29", "2025-09-27", 400_000, filed="2025-10-30"),
    fact("2024-09-29", "2025-09-27", 411_000, filed="2026-02-01"),   # restated later
])})
facts = edgar._annual_facts("x", ["Revenues"], "duration")
check("the most recently filed value wins", facts["2025-09-27"]["value"], 411_000)

edgar._get = fake_sec({
    "RevenueFromContractWithCustomerExcludingAssessedTax":
        concept([fact("2024-09-29", "2025-09-27", 400_000)]),
    "Revenues":
        concept([fact("2024-09-29", "2025-09-27", 999_000)]),
})
facts = edgar._annual_facts("x", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"], "duration")
check("the higher-preference tag supplies the period", facts["2025-09-27"]["value"], 400_000)
check("and names itself as the source tag",
      facts["2025-09-27"]["xbrl_tag"], "RevenueFromContractWithCustomerExcludingAssessedTax")

edgar._get = ORIGINAL_GET

print("\n52/53-week drift is tolerated, a quarter is not")
facts = {"2025-09-27": {"period_end": "2025-09-27", "value": 1},
         "2024-09-28": {"period_end": "2024-09-28", "value": 2}}
check("an exact period end matches", edgar._nearest(facts, "2025-09-27")["value"], 1)
check("a few days of calendar drift still matches",
      edgar._nearest(facts, "2025-09-30")["value"], 1)
check("a quarter away does not match", edgar._nearest(facts, "2025-06-30"), None)
check("the closer of two candidates wins", edgar._nearest(facts, "2024-10-05")["value"], 2)
check("no facts yields no match", edgar._nearest({}, "2025-09-27"), None)
check("the tolerance boundary is inclusive",
      edgar._nearest(facts, "2025-10-17")["value"], 1)      # exactly 20 days
check("one day past the boundary is refused",
      edgar._nearest(facts, "2025-10-18"), None)            # 21 days

print("\nRatios refuse to divide by nothing")
check("a zero denominator yields None", edgar._ratio(10, 0), None)
check("a missing denominator yields None", edgar._ratio(10, None), None)
check("a missing numerator yields None", edgar._ratio(None, 10), None)
check("a real ratio is computed", edgar._ratio(1, 4), 0.25)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}\n")
    raise SystemExit(1)
print("All EDGAR alignment checks passed.\n")
