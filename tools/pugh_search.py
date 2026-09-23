#!/usr/bin/env python3
"""Every two- and three-bar shape there is, enumerated, with an EXACT correction.

Fourteen searches in CLAUDE.md, and every one of them shares a weakness that
has nothing to do with its result: **the candidate list was chosen.** Someone
picked 41 rules, or 28 shapes, or 16 bookshelf families, and the Bonferroni
threshold was computed over the list that happened to be written down. That
correction is honest about the trials RUN and silent about the trials
AVAILABLE -- the grid could always have been wider, and a threshold derived
from a list somebody chose is a floor on the real multiplicity, not the real
multiplicity.

This search does not have that problem, and it is the only one here that does
not. Pugh's encoding (Archer, `Getting Started in Currency Trading`) labels a
bar by its high and low against the previous bar's, and there are exactly four
labels and no parameters:

    BULL     higher high, higher low
    BEAR     lower high, lower low
    OUTSIDE  higher high, lower low
    INSIDE   lower high, higher low

So the set of two-bar shapes is 16 and the set of three-bar shapes is 64, and
those are not selections -- they are **the complete families**. Testing all 80
and correcting across 80 is therefore an exact correction rather than a
conventional one, and there is no wider grid to be accused of stopping short
of. Nothing here has a threshold, a lookback or a smoothing constant to tune,
which is the other half of the appeal: `rule_search`'s 41 candidates carry 41
parameter choices, and these carry none.

**It also subsumes rules from the shelf rather than competing with them.**
Archer notes every classical chart pattern reduces to a Pugh series, and the
Nofri congestion rule and the four "bathtub" conditionals in the same books are
all specific members of this family. Enumerating the family tests them all at
once, under one correction, instead of one at a time under fifteen.

**One qualification to the exactness claim, measured rather than assumed.**
The family is complete, but it is not uniformly POWERED. Across seven majors
and 20,000 H1 bars each, `out_out_out` occurs 72 times and `in_in_in` 113,
against 23,184 for `bull_bull`. A cell that rare cannot carry a verdict, so
the correction is exact over the family while the evidence within it is very
uneven, and the rare cells are reported as under-powered rather than as nulls.

**What a null here would mean, stated before the run.** Not "these 80 shapes
do not work" but something stronger: **no two- or three-bar configuration of
highs and lows carries a tradable directional bias at this cost.** That is a
closed statement about an entire representation, which is worth more than
another open-ended family coming back empty.

Everything except the candidate list is `rule_search`'s: the same permutation
null, era blocks, walk-forward, date clustering and measured spread.

    python tools/pugh_search.py
    python tools/pugh_search.py --timeframe D1
"""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
import rule_search

BULL, BEAR, OUTSIDE, INSIDE = 0, 1, 2, 3
NAMES = ("bull", "bear", "out", "in")


def pugh_states(h, l):
    """One of four labels per bar, from its high and low against the previous.

    The partition is built on two STRICT comparisons -- a strictly higher high
    and a strictly lower low -- because those are the only two facts the four
    definitions need, and every combination of two booleans lands in exactly
    one class. That is what makes the family exhaustive and disjoint, which is
    the whole basis for correcting across it exactly.

    **Ties decide where flat bars go, and the first version got this wrong.**
    Written as "higher high" and "higher low", a bar with a high and low equal
    to its predecessor's satisfies neither, falls into the not-higher/
    not-higher corner, and is labelled BEAR -- but a bar identical to the one
    before it is not bearish, it is contained. Measured on 139,993 H1 bars
    across the seven majors, **2,597 of them (1.855%) carry an equal high or
    low**, so this is not a rounding detail. Phrased as "strictly higher high"
    and "strictly lower low", the same two booleans send an equal bar to
    INSIDE, which is what containment means.
    """
    h = np.asarray(h, float)
    l = np.asarray(l, float)
    hh = np.empty(len(h), dtype=bool)   # strictly higher high
    ll = np.empty(len(h), dtype=bool)   # strictly lower low
    hh[0] = ll[0] = False
    hh[1:] = h[1:] > h[:-1]
    ll[1:] = l[1:] < l[:-1]
    state = np.where(hh & ll, OUTSIDE,
                     np.where(hh & ~ll, BULL,
                              np.where(~hh & ll, BEAR, INSIDE)))
    state[0] = -1                      # the first bar has no predecessor
    return state


def make_sequence(seq, short=False):
    """Fire when the last len(seq) bars carry exactly this label sequence.

    The signal is read at the bar that COMPLETES the sequence, so it uses that
    bar's own high and low and nothing after them. `rule_search` enters at the
    next bar's open, which is where the one-bar lag lives.
    """
    seq = tuple(seq)
    k = len(seq)

    def f(o, h, l, c):
        st = pugh_states(h, l)
        n = len(st)
        sig = np.zeros(n)
        if n <= k:
            return sig
        match = np.ones(n, dtype=bool)
        match[:k] = False
        for j, want in enumerate(seq):
            off = k - 1 - j
            shifted = np.full(n, -1)
            shifted[off:] = st[:n - off] if off else st
            match &= shifted == want
        sig[match] = -1.0 if short else 1.0
        return sig

    return f


def build_pugh_candidates():
    """All 16 two-bar and all 64 three-bar shapes. Complete, not selected.

    Long-side only. A short on the same shape is the exact negation, so its
    t-statistic is this one's with the sign flipped and testing both would
    double the correction while adding no information. The permutation null in
    `rule_search` handles the two-sidedness.
    """
    c = []
    for k in (2, 3):
        for seq in itertools.product(range(4), repeat=k):
            name = "_".join(NAMES[s] for s in seq)
            c.append((f"pugh_{name}", f"pugh_{k}bar", make_sequence(seq)))
    return c


def main():
    """Monkeypatched, as `shape_search` and `book_rules_search` do it.

    `rule_search.py` owns the null, the correction, the era blocks, the
    walk-forward, the date clustering and the measured spread.
    """
    rule_search.build_candidates = build_pugh_candidates
    if "--out" not in sys.argv:
        sys.argv += ["--out", "reports/pugh_search.json"]
    return rule_search.main()


if __name__ == "__main__":
    raise SystemExit(main())
