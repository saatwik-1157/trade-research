# POLICY_GOVERNANCE_WORKFLOW.md

L64 sections 28 and 32. Review and the AI boundary.

---

**See `POLICY_CHANGE_GOVERNANCE.md` (L63) for the workflow itself.** This adds
only what L64 changes, which is the research stage in front of it.

## What a governance review item must carry

Baseline, candidate, policy diff, safety report, validation report, replay
report, stress report, shadow report, complexity, known limitations, rollback
plan, certification impact.

`Diff.as_dict()` and `Validation.as_dict()` produce the first three today. The
rest need runs that have not happened.

## The AI boundary, unchanged and now narrower

AI may identify research opportunities, generate hypotheses, suggest candidates,
explain differences, summarise evidence and rank candidates.

AI may not approve, certify, deploy, bypass governance, modify hard safety,
change risk ceilings, enable live trading or disable monitoring.

**L64 makes the last five unreachable rather than forbidden.** Every one of
those targets is Category A, and Category A is not proposable - so an AI cannot
even form a candidate aimed at one. It is not rejected at review; it fails
static validation as malformed input.

## Human approval remains mandatory

For every Category B change. Category B may be *researched* without a person;
it may not be *applied* without one. The distinction is the whole of section 4:
research is proposal, not authority.

## No self-modifying loop

Section 42 forbids: AI -> policy modification -> activation -> AI.

The loop is broken at activation, structurally. `policy_version` is a
governance target (L63), so an autonomous action aimed at it is FORBIDDEN with
no approval path, and nothing under `app/` imports the research module at all.
