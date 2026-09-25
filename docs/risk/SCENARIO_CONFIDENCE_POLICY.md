# SCENARIO_CONFIDENCE_POLICY.md

L66 section 13. Confidence, and what unknown means.

---

## Separate components

Data, model, scenario, portfolio, execution. **Not one score.**

A caller that wants a single number has to decide how to combine them, and the
honest combination is "the lowest" -- which is what `weakest()` returns.

## Unmeasured is not zero and not one

`weakest()` returns `None` when nothing was stated. Not `0`, not `1`.

A caller must be able to distinguish *nothing was measured* from *everything
was measured and it was bad*, because those warrant different responses: one
needs measurement, the other needs action.

## Unknown counts as low

`is_low()` returns `True` when the weakest component is `None`.

Section 13 says UNKNOWN must not be read as HIGH, and the reading that does the
damage is the one that treats an unmeasured confidence as good enough to act
on. This platform has been bitten by that shape before -- `open_symbols`
defaulting to an empty frozenset made "nobody told me" and "nothing is open"
the same fact.

## Low confidence makes the handling more conservative, not less

Section 46's TEST 4. A forecast nobody can vouch for is a reason to do **less**,
never a reason to do more.

`gate()` raises the verdict on low or unstated confidence. It never lowers one
on high confidence -- there is no path that does, which is the asymmetry the
whole module rests on.
