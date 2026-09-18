#!/usr/bin/env python
"""Search Supertrend, the one family five searches never covered.

WHERE THIS CAME FROM
--------------------
`studiogangster/next-gen-algo-trading-bot` ships one concrete strategy,
`strategies/supertrend_rsi.py`: buy when Supertrend flips bullish and RSI > 50,
sell when it flips bearish and RSI < 50, at Supertrend(10, 3.0) and RSI(14) on
Indian equities intraday. Two other repos were read alongside it and neither
contained a testable rule at all.

WHY IT IS WORTH A RUN WHEN NOTHING ELSE WAS
-------------------------------------------
Every other candidate that has arrived here was a rename. MACD is an EMA cross;
a "breakout" is Donchian; a "trend filter" on a moving average is an MA cross.
Supertrend is not one of those, and the reason is its RATCHET: the band only
ever moves toward price and never away, so the flip level carries state from
every bar since the last flip. A Donchian channel is a pure function of the
last n bars; a Bollinger band is a pure function of the last n closes.
Supertrend's band on bar t depends on its own value at t-1, which is the one
structural property none of the searched families has.

So it is a genuinely new shape, and that makes it worth the Bonferroni cost.
It is also the last obvious candidate: the L23 feature catalogue was closed by
`shape_search.py`, and this closes the one family an outside repo contributed.

THE PRIOR, STATED BEFORE THE RUN
--------------------------------
Six families across five universes have failed here, and the only effects ever
large enough to measure were negative and were cost. The honest prior is that
this fails too. It is written down here, before the numbers, because a prior
recorded after the result is not a prior.

What would count as a finding: clearing the Bonferroni threshold IN sample,
beating the permutation null, AND holding out of sample, AND surviving the era
blocks and the walk-forward. `donchian_fade_55` cleared three of those and was
still not a survivor.

WHAT IS REUSED
--------------
Everything except the candidate list, exactly as `shape_search.py` does it.
`rule_search.search_market` supplies the permutation null, the Bonferroni
threshold over the true candidate count, the era blocks, the walk-forward and
the date-clustered inference; `rule_backtest.simulate` supplies the measured
spread. Those gates are what dissolved every previous candidate, so a new
family that does not face them has not been tested, it has been demonstrated.

THE GRID IS DELIBERATELY SMALL
------------------------------
14 candidates, not 140. Every extra one raises the threshold the winner has to
clear, and this project has already watched a 41-candidate search hand its best
in-sample result to the permutation null. The grid covers the parameters the
source strategy used and one step either side, plus the inverse of each, plus
the RSI-filtered variant the source actually specifies.

    python tools/supertrend_search.py --timeframe H1
    python tools/supertrend_search.py --timeframe D1 --blocks 5
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rule_search  # noqa: E402
from rule_backtest import atr_series, wilder_rsi  # noqa: E402


def supertrend_direction(h, l, c, length: int, multiplier: float):
    """The Supertrend direction series: +1 bullish, -1 bearish, NaN while warming.

    This is the pandas_ta formulation, written out rather than imported,
    because the ratchet is the whole reason this family is not a rename and a
    library call would hide it.

    The band is built from hl2 +/- multiplier * ATR, and then RATCHETED: while
    the trend is up the lower band may only rise, and while it is down the
    upper band may only fall. That carry is what makes the flip level depend on
    every bar since the last flip, rather than on a fixed lookback.

    A flip happens when the close crosses the OPPOSING band as it stood on the
    previous bar -- `upper[i-1]`, not `upper[i]` -- so the rule reads a closed
    bar and `simulate()` enters on the following one. Comparing against the
    current bar's band would be a look-ahead, and it is the easy mistake here.
    """
    n = len(c)
    atr = atr_series(h, l, c, length)
    hl2 = (h + l) / 2.0
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    direction = np.full(n, np.nan)
    fu = np.copy(upper)
    fl = np.copy(lower)

    # The first bar with a usable ATR seeds the state. Everything before it
    # stays NaN and is masked out of the signal, rather than being handed a
    # default direction that the search could trade on.
    start = int(np.argmax(np.isfinite(atr))) if np.any(np.isfinite(atr)) else n
    if start >= n - 1:
        return direction

    # SEEDED FROM THE DATA, not from a constant. Every Supertrend
    # implementation has to guess a direction for its first bar, and the usual
    # guess is +1. On a series that opens in a downtrend that guess is wrong
    # and the indicator emits a correcting flip a few bars later -- a SELL
    # signal that exists only because of the seed. Verified on a synthetic
    # ramp: seeded at +1, a falling series flipped at bar 16 having never
    # crossed a band on its own terms.
    #
    # One spurious trade per symbol per run is small, and this project does
    # not get to call things small without measuring them.
    #
    # Seeded from the direction of the warm-up window itself, which is data
    # the ATR has already consumed, so nothing is read ahead. Comparing the
    # close to hl2 was tried first and is useless: on bars whose high and low
    # are symmetric about the close, hl2 IS the close, the test degenerates to
    # `c >= c`, and the seed is +1 again by accident.
    #
    # `max(0, ...)` is not defensive padding. A negative index does not raise
    # in numpy, it wraps to the END of the array -- so if `atr_series` ever
    # warmed up in fewer than `length` bars, the seed would be read from the
    # future and the only symptom would be a rule that looked slightly good.
    # Audited across five bar counts and three lengths: the index is 0 every
    # time, so this changes no result today and forecloses a silent one.
    seed_ref = max(0, start - length)
    direction[start] = 1.0 if c[start] >= c[seed_ref] else -1.0
    for i in range(start + 1, n):
        if not np.isfinite(atr[i]):
            direction[i] = direction[i - 1]
            continue
        prev = direction[i - 1]
        if c[i] > fu[i - 1]:
            direction[i] = 1.0
        elif c[i] < fl[i - 1]:
            direction[i] = -1.0
        else:
            direction[i] = prev
            # The ratchet. A band may only tighten toward price while the
            # trend it belongs to is in force.
            if direction[i] > 0 and fl[i] < fl[i - 1]:
                fl[i] = fl[i - 1]
            if direction[i] < 0 and fu[i] > fu[i - 1]:
                fu[i] = fu[i - 1]
    return direction


def make_supertrend(length: int, multiplier: float, invert: bool = False):
    """Trade the flip, and only the flip.

    A signal on every bar the trend is up would be an exposure rule, not a
    timing one -- and this project has already established that trading more
    often is the one lever with a measured sign, pointing down. So the signal
    is non-zero only on the bar the direction changes.
    """
    def f(o, h, l, c):
        d = supertrend_direction(h, l, c, length, multiplier)
        sig = np.zeros(c.shape, dtype=int)
        flip_up = np.zeros(c.shape, dtype=bool)
        flip_dn = np.zeros(c.shape, dtype=bool)
        ok = np.isfinite(d)
        flip_up[1:] = ok[1:] & ok[:-1] & (d[:-1] == -1.0) & (d[1:] == 1.0)
        flip_dn[1:] = ok[1:] & ok[:-1] & (d[:-1] == 1.0) & (d[1:] == -1.0)
        sig[flip_up] = -1 if invert else 1
        sig[flip_dn] = 1 if invert else -1
        return sig
    return f


def make_supertrend_rsi(length: int, multiplier: float, rsi_n: int = 14,
                        level: float = 50.0, invert: bool = False):
    """The source strategy exactly: the flip, gated on which side of 50 RSI is.

    Worth testing SEPARATELY from the bare flip rather than instead of it. RSI
    as an ENTRY is a refuted family here; RSI as a FILTER on another entry is a
    different claim, and the only way to attribute a result to the filter is to
    measure the unfiltered rule beside it. If the filtered version scores and
    the bare one does not, the filter did something; if both score the same,
    the filter is decoration on a Supertrend result.
    """
    def f(o, h, l, c):
        d = supertrend_direction(h, l, c, length, multiplier)
        r = wilder_rsi(c, rsi_n)
        sig = np.zeros(c.shape, dtype=int)
        ok = np.isfinite(d) & np.isfinite(r)
        up = np.zeros(c.shape, dtype=bool)
        dn = np.zeros(c.shape, dtype=bool)
        up[1:] = ok[1:] & np.isfinite(d[:-1]) & (d[:-1] == -1.0) & (d[1:] == 1.0)
        dn[1:] = ok[1:] & np.isfinite(d[:-1]) & (d[:-1] == 1.0) & (d[1:] == -1.0)
        up &= r > level
        dn &= r < level
        sig[up] = -1 if invert else 1
        sig[dn] = 1 if invert else -1
        return sig
    return f


def build_supertrend_candidates():
    """14 candidates. The source's parameters, one step either side, and inverses.

    Kept small on purpose. The Bonferroni threshold is computed over the true
    candidate count, so every speculative extra raises the bar for the ones
    that were actually motivated -- and an unmotivated grid is how the 41-rule
    search ended up handing its best result to a shuffle.
    """
    c = []
    # (10, 3.0) is what next-gen ships. The others are one step either side on
    # each axis, which is the smallest grid that can say whether a result is a
    # parameter artefact or a property of the family.
    for length, mult in ((10, 3.0), (7, 3.0), (14, 3.0), (10, 2.0), (10, 4.0)):
        c.append((f"supertrend_{length}_{mult:g}", "supertrend",
                  make_supertrend(length, mult)))
    # The inverse of each is not padding. A rule and its inverse cannot both be
    # skill, and when the pair scores symmetrically the difference between them
    # is cost rather than timing -- which is how `body_ride_0.6` at t = -4.65
    # was read, and it is the most reliable reading this project gets.
    for length, mult in ((10, 3.0), (7, 3.0), (10, 2.0)):
        c.append((f"supertrend_{length}_{mult:g}_inv", "supertrend_inv",
                  make_supertrend(length, mult, invert=True)))
    # The source strategy as written, plus the parameters either side of it.
    for length, mult in ((10, 3.0), (7, 3.0), (14, 3.0)):
        c.append((f"st_rsi_{length}_{mult:g}", "supertrend_rsi",
                  make_supertrend_rsi(length, mult)))
    c.append(("st_rsi_10_3_inv", "supertrend_rsi_inv",
              make_supertrend_rsi(10, 3.0, invert=True)))
    # A tighter and a looser RSI gate, to separate "the filter matters" from
    # "50 happens to be a good number on this sample".
    c.append(("st_rsi_10_3_lvl45", "supertrend_rsi",
              make_supertrend_rsi(10, 3.0, level=45.0)))
    c.append(("st_rsi_10_3_lvl55", "supertrend_rsi",
              make_supertrend_rsi(10, 3.0, level=55.0)))
    return c


def main():
    """Run `rule_search` against this candidate set instead of its own.

    Monkeypatched rather than parameterised, following `shape_search.py`:
    `rule_search.py` holds every gate that matters and the candidate list is
    the only thing that should differ between searches.
    """
    rule_search.build_candidates = build_supertrend_candidates
    return rule_search.main()


if __name__ == "__main__":
    raise SystemExit(main())
