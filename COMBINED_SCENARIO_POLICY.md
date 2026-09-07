# COMBINED_SCENARIO_POLICY.md

L67 sections 9, 10 and 40. Combinations, and stopping.

---

## The caps

| Limit | Value |
|---|---|
| `MAX_DIMENSIONS` | 3 |
| `MAX_COMBINATIONS` | 24 |
| `MAX_RECURSION_DEPTH` | 2 |

Deliberately small. The cost of a cap that is too tight is a scenario somebody
runs by hand; the cost of one too loose is the explosion this exists to
prevent.

Configuration decisions, not measurements. **They require approval.**

## It stops loudly

Every cap returns a `GenerationStop` naming which one was hit.

**A generator that silently truncates produces a coverage report whose gaps
look like decisions.** Section 40 asks for limits; the part that makes them
safe is that exceeding one is visible.

## It stops before generating, not after

The caps are checked before the combinations are produced, so an over-large
request costs nothing. The explosion this prevents happens *during* generation
-- checking afterwards would mean paying for it first.

`combinations(all 16 classes, dimensions=3)` is 560 combinations against a cap
of 24: it returns `COMBINATION_CAP` with an empty result and the arithmetic in
the reason, rather than the first 24.

## Recursion

Depth is tracked and capped. **A scenario that generates scenarios is a loop**,
and a loop that generates work is the one that does not stop on its own. This
is the L51 lesson -- five good gates and none asking *how many times* -- in its
fourth costume.
