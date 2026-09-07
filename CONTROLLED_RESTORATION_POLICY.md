# CONTROLLED_RESTORATION_POLICY.md

Getting autonomy back. L63 §17 and §18, 2026-09-06.

---

## The rule

**A condition clearing is not evidence that the thing it broke now works.**

§16 says do not restore maximum autonomy merely because the original error
disappeared. The way that rule gets broken is a transition table where
`SUSPENDED → CERTIFIED` exists at all, so it does not exist:
`test_no_state_reaches_certified_except_through_revalidating` asserts it over
the entire table, not at the three states somebody thought to check.

## The staged path

| Step | To level | Requires |
|---|---|---|
| 1 | OBSERVE | safe state reached and the cause diagnosed |
| 2 | RECOMMEND | targeted regression for the affected gates passes |
| 3 | APPROVAL | shadow operation shows the expected decisions |
| 4 | BOUNDED | a limited canary has run without violation |
| 5 | CERTIFIED | full revalidation passes and certification is renewed |

`next_restoration_step` returns **one step at a time**, and returns `None`
rather than skipping when the next step is above the certification ceiling.
One step at a time is what makes each step have to be earned separately.

## The asymmetry is deliberate

Falling is one step and automatic. Climbing back is staged and needs evidence
at each stage. A system that recovered as fast as it degraded would be a system
whose degradation meant nothing.

## Canary restoration

§18 asks for controlled restoration through one strategy, one bot, a limited
allocation, paper or shadow mode. The pattern for it already exists and is
named here so it is not rebuilt: `app/brokers/shadow.py` is a non-executing
adapter with its own test suite, and the BOUNDED step above is where it
belongs.

**It is not wired up**, because there is no autonomous control loop to run a
canary for. The staged path is the part that must be right before the first
restoration, and the canary is the part it would be tempting to add afterwards.

Whatever runs it stays behind the RiskEngine, the PositionSizer and the OMS
exactly as everything else does — `test_only_the_oms_reaches_a_venue_to_write`
covers a canary the same as anything else.
