# POLICY_SELECTION_POLICY.md

L64 sections 18 and 20. Choosing between baseline and challenger.

---

## Safety dominates lexicographically

Section 18 says a policy with better performance but worse safety must lose.

**A weighted sum cannot deliver that.** A weight is a price: with a large enough
gain elsewhere, the sum tips. `Score.ordering()` returns
`(safety, robustness, stability, -complexity)` and comparison is tuple
comparison, so safety is compared first and alone. The rule is true by
construction rather than by tuning - the same move L60 used to stop a lower
layer relaxing a higher one.

`test_safety_is_compared_first_and_alone` puts a candidate with safety 1 and
robustness 100 against one with safety 2 and robustness 0, and the safer one
wins.

## A tie goes to the incumbent

Section 20: never replace the baseline solely because the challenger scored
higher. `better()` refuses an equal score, because **a running policy has
evidence from running that a candidate does not have.** Equality on a score is
not equality of evidence.

## Complexity is a tie-break, not a term

Section 19. Negated in the ordering, so of two otherwise-equal policies the
simpler wins - but complexity can never outweigh safety, robustness or
stability, which is what would happen if it were a weighted term.

`Candidate.complexity()` counts the parameters moved. Measured, not asserted.

## What promotion still requires

A score is not an approval. Section 20's list - safety pass, validation pass,
robustness pass, regression pass, shadow evidence, governance approval,
certification - is encoded as `PromotionState` transitions, and no state
reaches ACTIVE except CANARY.
