# POLICY_ROBUSTNESS_POLICY.md

L64 section 27. Perturbation testing.

---

## The requirement

Perturb thresholds, cooldowns, weights, timing, volatility, execution
assumptions and data quality. A robust policy should not fail from tiny
changes.

## What is built

Bounds. Every researchable parameter has an inclusive range, and a candidate
outside it is `REJECTED_OUT_OF_BOUNDS` before anything runs.

That is not robustness testing and is not presented as it. It is the
precondition that makes robustness testing meaningful, because a parameter with
no approved range has nothing to be perturbed *within*.

`Score.robustness` sits in the ordering above stability and below safety, so a
robustness result has a defined place the day one can be produced.

## Not built

## Not built, and the reason is the same one

This needs **a governance policy that has actually run** to produce the data it
would evaluate. There is no policy deployment history, no autonomous action
history and no effectiveness record on this platform.

`CLAUDE.md` spends several hundred lines on what happens when a search is run
against insufficient data: 41 candidates, 128 combinations, 200 combinations,
five universes, and in every case the winner did not survive out-of-sample.
Running that machinery over an empty sample would not be a smaller version of
that mistake, it would be a purer one.

The methodology exists and is proven: `tools/rule_search.py` implements era
blocks, walk-forward folds, date clustering and a permutation null that
shuffles a candidate's own signals so the null pays the same spread. Applying
it to governance policy needs governance decisions to shuffle.
