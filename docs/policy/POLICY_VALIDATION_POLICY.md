# POLICY_VALIDATION_POLICY.md

L64 sections 10, 11, 14, 15 and 16. The validation pipeline.

---

## The pipeline

    POLICY IDEA -> HYPOTHESIS -> CANDIDATE -> STATIC VALIDATION
      -> SAFETY VALIDATION -> HISTORICAL REPLAY -> COUNTERFACTUAL
      -> STRESS TEST -> WALK-FORWARD -> SHADOW -> BASELINE COMPARISON
      -> GOVERNANCE REVIEW -> APPROVAL -> CERTIFICATION -> CONTROLLED DEPLOYMENT

`PromotionState` encodes it and `check_promotion` permits only forward steps.
**Nothing reaches ACTIVE except from CANARY**, asserted over the whole table
rather than at the states somebody thought to check.

There is no AI-to-deployment edge, and it is not prevented by a check:
`policy_version` is a governance target, so any action aimed at it is FORBIDDEN
with no approval path (L63).

## What is built

**Static validation** (section 11) - schema, types, ranges, forbidden fields,
hard-safety detection. Every check runs; a candidate is refused for every
reason it is refusable, because an author reading a rejection wants the whole
list rather than one fix at a time.

**Safety validation** - the Category A/B/C separation, which is the gate every
later stage depends on. A candidate that fails it never reaches simulation.

**Multiple-testing control** (section 25) - `app/research/selection.py`,
reused. It is the one piece of research machinery this project has validated
against its own recorded data.

## What is not built

Historical replay of candidate policies, the counterfactual harness, stress
testing, and walk-forward policy evaluation.

## Not built, and the reason is the same one

This needs **a governance policy that has actually run** to produce the data it
would evaluate. There is no policy deployment history, no autonomous action
history and no effectiveness record on this platform.

`CLAUDE.md` spends several hundred lines on what happens when a search is run
against insufficient data: 41 candidates, 128 combinations, 200 combinations,
five universes, and in every case the winner did not survive out-of-sample.
Running that machinery over an empty sample would not be a smaller version of
that mistake, it would be a purer one.

The walk-forward methodology itself is not missing - `tools/rule_search.py`
implements era blocks, walk-forward folds, date clustering and a permutation
null, all documented in `CLAUDE.md`. It has data to run on. Policy research
does not.
