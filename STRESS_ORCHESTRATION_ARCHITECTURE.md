# STRESS_ORCHESTRATION_ARCHITECTURE.md

Which stresses to run, and what running them established. L67, 2026-09-06.
Source: `backend/app/portfolio/stress.py`.

---

## The property this module exists to protect

**Every other safety artefact in this platform refuses something. This one
makes a positive claim.**

A coverage matrix asserts *the portfolio has been tested against that*, and a
positive claim is the kind that gets quoted. A stress orchestrator reporting
green without having run anything does not merely fail to help -- it
manufactures exactly the confidence the rest of the platform exists to avoid.

So coverage is **earned, never assumed**:

| Coverage value | Counts as covered? |
|---|---|
| `COVERED` | **yes** |
| `PARTIALLY_COVERED` | no |
| `UNCOVERED` | no |
| `STALE` | no |
| `UNKNOWN` | no |

One value of five. And there is no path from absence to it:

- No run at all -> `UNCOVERED`, not absent from the report.
- A run that **failed** -> `UNCOVERED`. Recording a crash as a run would turn
  an outage into evidence.
- Another **account's** run -> `UNCOVERED`. Coverage is account-scoped
  throughout; a matrix that pooled accounts would report coverage the fragile
  one does not have.
- A run with **no stated confidence** -> `PARTIALLY_COVERED`. A run nobody
  could vouch for has not established what it appears to.
- An **old** run -> `STALE`. Not covered, and not uncovered either: the
  distinction tells an operator whether to run it or re-run it.

Capability-checked: making absence return COVERED, or making UNKNOWN count as
covered, fails ten tests.

## The data constraint

Read against this deployment the coverage matrix returns **0 of 16 covered**
and the resilience verdict is **UNKNOWN**, which blocks autonomy.

That is the correct answer. No stress has ever been run here. The platform has
700 H1 bars for one instrument, one open position, and 271 orders from an
imported ledger.

`test_this_deployment_reports_zero_coverage` asserts it, so the honest reading
cannot quietly drift into a green report.

## It orchestrates; it does not execute

Section 44: stress must never become auto-execution.

`app/portfolio/stress.py` imports exactly one application module --
`app.portfolio.scenario`, for L66's `ScenarioDefinition`, `Recommendation` and
risk matrix, which section 4 asks it to reuse rather than duplicate. It imports
no session, no engine, no adapter, and nothing under `app/` imports it.

Both directions are asserted, and the second one caught something: adding this
module made L66's "nothing imports the scenario engine" test fail. The property
still holds transitively -- stress is itself unreachable -- so `stress.py` was
allow-listed **explicitly with the reason** rather than the rule being
loosened. The two tests together give the transitive property.

## Stress classes reuse L66's scenario kinds

Sixteen classes, each mapped to an existing `ScenarioKind`. The mapping exists
so this module adds a vocabulary for *scheduling* without adding a second
schema for *scenarios*.

## Hard failure outranks the score

Section 13. `evaluate()` checks for a hard safety failure **before anything is
weighed**, not as the largest term in a sum.

A weighted score cannot express "this outranks everything" -- only "this counts
for a lot". That is the same reasoning L63 used for critical invariants and L64
for lexicographic safety ordering, and it is now the fourth level where the
answer was to rank rather than to weight.
