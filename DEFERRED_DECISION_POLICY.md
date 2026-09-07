# DEFERRED_DECISION_POLICY.md

L65 sections 28 and 30. Recommendations that wait.

---

## A deferral names its condition

Every `Deferred` carries a `required_condition` - the thing that would have to
become true. Not optional, and not implied.

Section 28's last line is *do not create indefinite queues*, and the mechanism
that prevents one is not a size cap. It is that every entry states what would
clear it and when it expires. A queue of entries that each name their exit
condition drains; a queue of "later" does not.

## Revalidation is not a formality

Section 30. Before a deferred recommendation applies, five gates are re-asked:

1. **Expiry** - has the recommendation outlived its validity window?
2. **The deferral condition** - has the thing that displaced it actually
   cleared?
3. **Certification** - does the current certification permit autonomous
   adaptation? *A recommendation made under one certification does not carry
   into another.*
4. **Risk** - does the RiskEngine permit it **now**? Risk is re-asked rather
   than remembered: the answer it gave when this was proposed was about a
   portfolio that no longer exists.
5. **Data freshness** - missing is never read as permission.

The only path to `allowed=True` is past all five. Each returns a sentence
naming the condition, not a boolean.

`test_a_deferred_recommendation_is_revalidated_before_it_applies` breaks each
gate in turn and asserts each one alone blocks.

## Why the risk gate is re-asked rather than cached

This is the failure a deferral queue invites: a recommendation that was safe
when made, applied later against a portfolio that has moved. The longer it
waited, the more likely the answer changed - so the wait itself is the reason
to re-ask.
