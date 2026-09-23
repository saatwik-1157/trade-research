#!/usr/bin/env python3
"""The rules from the bookshelf that no previous search covers, measured.

Twelve books in `trade books/` were read on 2026-09-23 and mapped against the
thirteen searches in CLAUDE.md. Almost everything in them is ground already
refuted here -- RSI, MA crosses, Donchian, Bollinger, momentum, single-bar
candle shape, carry, calendar. **Four families came back genuinely untested
AND mechanically unambiguous**, which is the pair of conditions that matters:
a rule this repository cannot state without inventing a definition is a rule
it would be testing on its own authorship rather than the book's.

  * **Parabolic SAR** (Wilder, acceleration 0.02, cap 0.2). Not the ATR trail
    and not Supertrend: the band accelerates on each NEW EXTREME, so its
    distance from price depends on how long the move has run rather than on
    volatility. The only complete stop-and-reverse SYSTEM on the shelf.
  * **Linear-regression slope.** A least-squares slope over N bars is not an
    MA cross and not an N-bar return -- it weights the interior of the window,
    where both of those read only its ends.
  * **Heikin Ashi.** A recursive OHLC transform whose open carries state from
    every prior bar. `shape_search` refuted SINGLE-BAR shape; this is the one
    candle construction on the shelf that is not a function of one bar.
  * **Multi-bar sequences** -- Three White Soldiers, Three Black Crows, and
    Rising/Falling Three Methods. Also outside single-bar shape, and the only
    three the books define without a judgement call. Engulfing, Harami and the
    Star patterns are deliberately NOT here: each needs "in a definable trend"
    or "well into the body", and quantifying those would be inventing the rule
    rather than testing it.

**Pivot points are deliberately absent and it is worth saying why.** The
classic level is (H+L+C)/3 of the PREVIOUS CALENDAR DAY, held fixed through
the next day. `rule_search`'s signal contract passes o/h/l/c and no
timestamps, so a calendar-day pivot cannot be computed inside it, and a
rolling 24-bar substitute is a different rule. Untested with a reason stated
beats tested as something else.

Everything except the candidate list is `rule_search`'s: the same permutation
null, the same Bonferroni threshold, the same era blocks, walk-forward and
date clustering, the same measured spread.

    python tools/book_rules_search.py
    python tools/book_rules_search.py --timeframe D1
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
import rule_search


def parabolic_sar(h, l, acc=0.02, step=0.02, cap=0.2):
    """Wilder's SAR. Returns the band; the caller compares it with price.

    Written out rather than taken from a library because the acceleration IS
    the mechanism: `acc` rises by `step` only on a NEW extreme, and is capped.
    A version that accelerated every bar would be a different rule that still
    looked like this one from the outside.
    """
    n = len(h)
    sar = np.full(n, np.nan)
    if n < 2:
        return sar
    bull = True
    ep = h[0]
    af = acc
    sar[0] = l[0]
    for t in range(1, n):
        prev = sar[t - 1]
        cur = prev + af * (ep - prev)
        if bull:
            cur = min(cur, l[t - 1], l[max(0, t - 2)])
            if l[t] < cur:
                bull, cur, ep, af = False, ep, l[t], acc
            elif h[t] > ep:
                ep, af = h[t], min(af + step, cap)
        else:
            cur = max(cur, h[t - 1], h[max(0, t - 2)])
            if h[t] > cur:
                bull, cur, ep, af = True, ep, h[t], acc
            elif l[t] < ep:
                ep, af = l[t], min(af + step, cap)
        sar[t] = cur
    return sar


def make_sar(acc, cap, fade=False):
    def f(o, h, l, c):
        sar = parabolic_sar(np.asarray(h, float), np.asarray(l, float),
                            acc=acc, step=acc, cap=cap)
        c = np.asarray(c, float)
        sig = np.where(c > sar, 1.0, np.where(c < sar, -1.0, 0.0))
        sig[~np.isfinite(sar)] = 0.0
        return -sig if fade else sig

    return f


def regression_slope(c, n):
    """Least-squares slope of close on bar index over the trailing n bars.

    Divided by price so the output is dimensionless. A slope in price units
    carries the instrument's price, and pooling those across symbols is the
    metals-points error this repository documents.
    """
    out = np.full(len(c), np.nan)
    if len(c) < n:
        return out
    x = np.arange(n, dtype=float)
    xc = x - x.mean()
    denom = float(np.dot(xc, xc))
    win = np.lib.stride_tricks.sliding_window_view(c, n)
    out[n - 1:] = (win - win.mean(axis=1, keepdims=True)) @ xc / denom
    return out / np.where(c == 0, np.nan, c)


def make_regression(n, fade=False):
    def f(o, h, l, c):
        s = regression_slope(np.asarray(c, dtype=float), n)
        sig = np.sign(s)
        sig[~np.isfinite(sig)] = 0.0
        return -sig if fade else sig

    return f


def heikin_ashi(o, h, l, c):
    """HA open and close. The OPEN is recursive -- it carries every prior bar.

    That recursion is exactly why this is not covered by `shape_search`, whose
    28 candidates are all functions of one bar's four prices.
    """
    ha_c = (o + h + l + c) / 4.0
    ha_o = np.empty_like(ha_c)
    ha_o[0] = (o[0] + c[0]) / 2.0
    for t in range(1, len(ha_c)):
        ha_o[t] = (ha_o[t - 1] + ha_c[t - 1]) / 2.0
    return ha_o, ha_c


def make_heikin(fade=False, flip_only=False):
    def f(o, h, l, c):
        ha_o, ha_c = heikin_ashi(np.asarray(o, float), np.asarray(h, float),
                                 np.asarray(l, float), np.asarray(c, float))
        colour = np.sign(ha_c - ha_o)
        if flip_only:
            prev = np.concatenate(([0.0], colour[:-1]))
            colour = np.where(colour != prev, colour, 0.0)
        colour[~np.isfinite(colour)] = 0.0
        return -colour if fade else colour

    return f


def make_soldiers(fade=False):
    """Three White Soldiers / Three Black Crows, fully mechanical.

    Three same-colour bodies, each closing beyond the last, each OPENING
    inside the previous bar's body. The definition needs no trend
    precondition, which is why this is testable where Engulfing is not.
    """

    def f(o, h, l, c):
        o, c = np.asarray(o, float), np.asarray(c, float)
        n = len(c)
        sig = np.zeros(n)
        white, black = c > o, c < o
        for t in range(2, n):
            up = (white[t] and white[t - 1] and white[t - 2]
                  and c[t] > c[t - 1] > c[t - 2]
                  and o[t - 1] <= o[t] <= c[t - 1]
                  and o[t - 2] <= o[t - 1] <= c[t - 2])
            dn = (black[t] and black[t - 1] and black[t - 2]
                  and c[t] < c[t - 1] < c[t - 2]
                  and c[t - 1] <= o[t] <= o[t - 1]
                  and c[t - 2] <= o[t - 1] <= o[t - 2])
            sig[t] = 1.0 if up else (-1.0 if dn else 0.0)
        return -sig if fade else sig

    return f


def make_three_methods(fade=False):
    """Rising / Falling Three Methods.

    A long bar, three smaller bars held inside its range, then a close beyond
    the first bar's close. "Small" is fixed at half the first body rather than
    left to judgement, and that threshold is this file's choice -- the books
    say only "small", so it is named here rather than buried.
    """

    def f(o, h, l, c):
        o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
        n = len(c)
        sig = np.zeros(n)
        body = np.abs(c - o)
        for t in range(4, n):
            b0 = body[t - 4]
            small = all(body[t - k] < b0 * 0.5 for k in (3, 2, 1))
            inside = all(h[t - k] <= h[t - 4] and l[t - k] >= l[t - 4]
                         for k in (3, 2, 1))
            rise = (c[t - 4] > o[t - 4] and small and inside
                    and c[t] > o[t] and c[t] > c[t - 4])
            fall = (c[t - 4] < o[t - 4] and small and inside
                    and c[t] < o[t] and c[t] < c[t - 4])
            sig[t] = 1.0 if rise else (-1.0 if fall else 0.0)
        return -sig if fade else sig

    return f


def build_book_candidates():
    """Sixteen candidates, and the size is deliberate.

    A wider grid raises the Bonferroni threshold, and this repository has
    already measured what a wide search does against a permutation null: the
    36-cell bracket sweep scored the RANDOM rule at 1.76 in sample against the
    best real candidate's 0.83. Every family here carries its own inverse,
    because a rule and its inverse cannot both be skill and the difference
    between them is the cost of trading rather than the timing.
    """
    c = []
    for acc, cap in ((0.02, 0.2), (0.01, 0.1)):
        tag = f"{acc}_{cap}"
        c.append((f"sar_ride_{tag}", "parabolic_sar", make_sar(acc, cap)))
        c.append((f"sar_fade_{tag}", "parabolic_sar_fade",
                  make_sar(acc, cap, fade=True)))
    for n in (50, 200):
        c.append((f"linreg_ride_{n}", "regression_slope", make_regression(n)))
        c.append((f"linreg_fade_{n}", "regression_slope_fade",
                  make_regression(n, fade=True)))
    c.append(("ha_ride", "heikin_ashi", make_heikin()))
    c.append(("ha_fade", "heikin_ashi_fade", make_heikin(fade=True)))
    c.append(("ha_flip_ride", "heikin_ashi_flip", make_heikin(flip_only=True)))
    c.append(("ha_flip_fade", "heikin_ashi_flip_fade",
              make_heikin(fade=True, flip_only=True)))
    c.append(("soldiers_ride", "three_soldiers", make_soldiers()))
    c.append(("soldiers_fade", "three_soldiers_fade", make_soldiers(fade=True)))
    c.append(("three_methods_ride", "three_methods", make_three_methods()))
    c.append(("three_methods_fade", "three_methods_fade",
              make_three_methods(fade=True)))
    return c


def main():
    """Monkeypatched, exactly as `shape_search` does it.

    `rule_search.py` holds the null, the correction, the era blocks, the
    walk-forward, the date clustering and the measured spread. Reimplementing
    any of that to carry a new candidate list would be rebuilding the only
    part of this project that has ever worked.
    """
    rule_search.build_candidates = build_book_candidates
    if "--out" not in sys.argv:
        sys.argv += ["--out", "reports/book_rules_search.json"]
    return rule_search.main()


if __name__ == "__main__":
    raise SystemExit(main())
