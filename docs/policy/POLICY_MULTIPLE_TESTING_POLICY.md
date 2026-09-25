# POLICY_MULTIPLE_TESTING_POLICY.md

L64 section 25. Preventing manufactured confidence.

---

**See `POLICY_OVERFITTING_POLICY.md`** for the gate itself
(`app/research/selection.py`, reused from L58/L59 and validated against this
repository's own recorded searches).

This records the one thing section 25 asks for that is separate: **counting**.

## What must be counted

Candidates tested, variants generated, the selection process, validation
periods, repeated experiments, data reuse.

`expected_by_chance(tested, threshold)` turns the count into the number that
matters - how many candidates a coin flip clears over the same grid - which is
why the count has to be honest rather than merely recorded.

## The failure this prevents, measured on this repository

Every figure below is from `CLAUDE.md`, from searches this project actually
ran:

| Search | Cleared out of sample | Expected by chance |
|---|---|---|
| 41 rule candidates | 0 | about 1 |
| 128 D1 exit combinations | 0 | 3.2 |
| 200 H4 exit combinations | 5 | 4.9 |
| 28 shape candidates | 0 | 0.7 |

The 200-combination row is the instructive one: five combinations cleared the
conventional bar, which sounds like five findings and is fewer than chance
predicts. **A winner cannot be read without its search size**, and the search
size is the thing a research system is most tempted not to count.

## Compute limits

Section 38 asks for maximum candidates, experiments, runtime, parallel jobs,
dataset size and mutation depth.

Not built, and the reason is not that they do not matter - it is that zero
candidates have been generated, so a limit would be a number guarding nothing.
The bound that does exist and does bind is the allow-list: eight researchable
parameters with explicit ranges is a small space by construction, and it is
capped at twelve entries by a test.
