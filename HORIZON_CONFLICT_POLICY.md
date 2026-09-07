# HORIZON_CONFLICT_POLICY.md

L65 section 8. When horizons disagree.

---

## What counts as a conflict

Two horizons returning **different verdicts about the same target**.

Two horizons that agree are not in conflict. Two that speak about different
targets are not either. Reporting those as conflicts would make the conflict
list noise, and a noisy list is one nobody reads.

## Resolution

The most restrictive verdict wins, via L60's `decide()`. See
`HORIZON_AUTHORITY_POLICY.md`.

## The loser is kept

Section 8: *do not discard the long-term recommendation.* A losing
non-safety recommendation becomes a `Deferred` carrying:

- the recommendation itself
- which horizon displaced it
- **why** it was displaced
- the **required condition** that would let it through
- when it expires

The required condition is named rather than implied. A deferral with no
condition is a queue entry nobody can ever clear, which is how "we will look at
it later" becomes "never".

## Safety is not deferred

A safety horizon that loses is not parked - it either decided or it agreed.
Deferral is a waiting state, and EMERGENCY and IMMEDIATE_RISK do not wait.

`test_a_safety_horizon_is_never_parked_in_the_deferred_queue`.

## Explainability

Section 34 requires that a person can read why a decision won.
`MultiHorizonDecision.explain()` produces, for example:

> SHORT_TERM: ALLOW, MEDIUM_TERM: REDUCE, LONG_TERM: ALLOW. MEDIUM_TERM wins
> (PORTFOLIO layer) -> REDUCE. Conflicts: SHORT_TERM says ALLOW and
> MEDIUM_TERM says REDUCE about alloc. Deferred: SHORT_TERM, LONG_TERM.
