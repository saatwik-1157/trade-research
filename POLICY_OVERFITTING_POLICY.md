# POLICY_OVERFITTING_POLICY.md

L64 sections 25 and 26. Multiple testing and overfitting.

---

## Reused, not rebuilt

`app/research/selection.py` - the L58/L59 multiple-testing gate - is the
implementation. It provides `clearing_p`, `expected_by_chance`,
`bonferroni_threshold`, `assess` and `survives_correction`.

**It was validated against this repository's own recorded searches.**
`CLAUDE.md` reports 41 candidates clearing about 1 by chance, 128 combinations
clearing 3.2, 200 clearing 4.9, 28 shape candidates clearing 0.7, and a
Bonferroni bar of 3.546 for 128 - and the gate reproduces every one.

Rebuilding it for policy research would have been a second implementation of
the one piece of research machinery this project has already proven against
real data.

## Its three verdicts, and none is approval

- `NOTHING_ESTABLISHED` - cleared no more than chance predicts. **This is the
  verdict every search in `CLAUDE.md` received.**
- `ABOVE_CHANCE_NOT_SIGNIFICANT` - more cleared than chance, but no candidate
  clears the bar the search size demands.
- `SURVIVES_CORRECTION` - still not an approval. It is the point at which
  out-of-sample and walk-forward evidence becomes worth gathering.

## Duplicate detection is part of this

Re-testing an already-assessed candidate is refused as another draw from the
same urn. Section 25 asks to track candidates tested, variants, repeated
experiments and data reuse; deduplication is the part of that which can be
enforced rather than merely counted.

## The calibration that had to be fixed at L58

The first draft used `cleared <= expected`, a hairline that called an H4 search
with 5 cleared against 4.99958 expected above chance. The margin is now two
standard deviations of the null's own binomial spread, which puts the notable
threshold for a 200-candidate grid at 9.4 rather than 5.0. Recorded because a
threshold set at the hairline is one that fires on rounding.
