#!/usr/bin/env python
"""Search candle-shape and volatility-regime rules on unsearched ground.

`rule_search.py` has searched five families -- RSI, MA crosses, Donchian,
Bollinger and momentum -- across five universes and three timeframes, and
nothing survived out of sample. This tool asks a narrower question about ground
none of those searches covered.

WHY THESE FAMILIES AND NOT THE OTHERS
-------------------------------------
The L23 feature catalogue in `backend/app/datasets/features.py` holds 22
features. Five of them reconstruct the families already searched:

    rsi_14                      -> rsi_reversion / rsi_momentum
    ema_spread_10_50            -> the ema_10_50 cross IS sign(spread) flipping
    high_20_distance/low_20_    -> a donchian_20 break IS distance == 0
    sma_distance_20 + volatility_20 -> the Bollinger band breach
    roc_10                      -> momentum, at a shorter lookback

Handing a model all 22 would search dead ground and new ground at once, and a
result could not be attributed to either. So this searches ONLY what was never
covered:

    body_pct, range_pct, upper_wick_pct, lower_wick_pct   candle shape
    atr_pct_14, atr_ratio_14_50                           volatility regime

Volume and time-of-day are the other two unsearched families and are
deliberately excluded here. MT5 supplies TICK volume -- a count of quote
updates, not traded size -- so a volume result would need its own caveat; and
hour-of-day already has one measured negative reading (`--skip-hours 0` moved
median out-of-sample expectancy from -6.64 to -7.40 and improved 18 of 41
candidates, a coin flip).

WHAT IS REUSED, AND WHY THAT MATTERS
------------------------------------
Everything except the candidate list. `rule_search.search_market` supplies the
permutation null, the Bonferroni threshold, the era blocks, the walk-forward and
the date clustering, and `rule_backtest.simulate` supplies the measured spread.
Those are the gates that dissolved every previous candidate, including the
+227-point `donchian_fade_55` that looked like a survivor until the era split,
the date clustering and the walk-forward were applied to it.

Using a second harness would make the result incomparable with the 41-candidate
baseline, which is the only thing that gives a number here any meaning.

THE NULL IS THE CONTROL
-----------------------
`permute()` shuffles a candidate's own signals, preserving the trade count and
the buy/sell mix. The shuffled rule pays the same spread and takes the same
exposure, so beating it isolates TIMING rather than exposure -- which is exactly
the control a filter-style result needs, and it is already built.

Expect a null. The value of this run is that it would be a null on ground the
repository has not covered, which is worth more than another null on RSI.

    python tools/shape_search.py --out reports/shape_search.json
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rule_search  # noqa: E402
from rule_backtest import atr_series, sma  # noqa: E402


# ------------------------------------------------------------------ shape
#
# Every one of these reads a CLOSED bar; `simulate()` enters on the following
# one. The same discipline the other families follow, and the reason a wick on
# bar t can be acted on at t+1 without looking ahead.


def _parts(o, h, l, c):
    """The four L23 candle-shape features, computed once.

    `range` is the denominator throughout, so every output is dimensionless --
    the rule the whole feature catalogue follows, because a body measured in
    price is the price of the instrument and pooling those across symbols is
    the metals-points error.
    """
    rng = h - l
    with np.errstate(divide="ignore", invalid="ignore"):
        body = np.abs(c - o) / rng
        upper = (h - np.maximum(o, c)) / rng
        lower = (np.minimum(o, c) - l) / rng
        range_pct = rng / c
    for arr in (body, upper, lower, range_pct):
        arr[~np.isfinite(arr)] = np.nan
    return body, upper, lower, range_pct


def make_wick(threshold, invert=False):
    """A long wick is a rejection of the prices it reached.

    An upper wick above the threshold means price went there and came back, so
    the reading is bearish; a lower wick is bullish. `invert` takes the other
    side, because a rejection and a continuation are the two readings of the
    same bar and the search should not assume which.
    """

    def f(o, h, l, c):
        _body, upper, lower, _rng = _parts(o, h, l, c)
        sell = (upper > threshold) & (upper > lower)
        buy = (lower > threshold) & (lower > upper)
        sig = np.where(buy, -1 if invert else 1, np.where(sell, 1 if invert else -1, 0))
        sig[np.isnan(upper) | np.isnan(lower)] = 0
        return sig

    return f


def make_body(threshold, fade=False):
    """A bar that is mostly body closed with conviction in one direction."""

    def f(o, h, l, c):
        body, _u, _lo, _rng = _parts(o, h, l, c)
        strong = body > threshold
        direction = np.sign(c - o)
        sig = np.where(strong, -direction if fade else direction, 0).astype(float)
        sig[np.isnan(body)] = 0
        return sig

    return f


def make_range_expansion(n, k, fade=False):
    """A bar whose range is k times its own recent mean range.

    Relative to the instrument's OWN recent range rather than to an absolute
    figure, so the same rule means the same thing on EURUSD and on XAUUSD.
    """

    def f(o, h, l, c):
        _b, _u, _lo, range_pct = _parts(o, h, l, c)
        baseline = sma(range_pct, n)
        wide = range_pct > (k * baseline)
        direction = np.sign(c - o)
        sig = np.where(wide, -direction if fade else direction, 0).astype(float)
        sig[np.isnan(range_pct) | np.isnan(baseline)] = 0
        return sig

    return f


def make_body_atr(k, fade=False):
    """Body measured against recent volatility rather than against its own bar.

    The distinction matters: a bar can be almost all body and still be small.
    This fires only when the move itself is large for the instrument's current
    volatility, which is what `atr_pct_14` is for.
    """

    def f(o, h, l, c):
        atr = atr_series(h, l, c)
        with np.errstate(divide="ignore", invalid="ignore"):
            scaled = np.abs(c - o) / atr
        scaled[~np.isfinite(scaled)] = np.nan
        big = scaled > k
        direction = np.sign(c - o)
        sig = np.where(big, -direction if fade else direction, 0).astype(float)
        sig[np.isnan(scaled)] = 0
        return sig

    return f


# ------------------------------------------------------ volatility regime


def _atr_ratio(h, l, c, fast=14, slow=50):
    """`atr_ratio_14_50`: short-window volatility over long-window.

    Above 1 the market is more volatile than it has been; below 1, less. A
    ratio rather than a level, so it is comparable across instruments -- the
    same reason the catalogue offers `atr_pct_14` and not raw ATR.
    """
    quick = atr_series(h, l, c, fast)
    slow_atr = atr_series(h, l, c, slow)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = quick / slow_atr
    ratio[~np.isfinite(ratio)] = np.nan
    return ratio


def make_vol_regime(k, contracting=False, fade=False):
    """Fire when volatility CROSSES into a regime, not while it sits there.

    A level test fires on every bar of a regime and produces one enormous
    position rather than a series of trades; the crossing is the event. The
    same correction `make_momentum` already applies.
    """

    def f(o, h, l, c):
        ratio = _atr_ratio(h, l, c)
        inside = (ratio < k) if contracting else (ratio > k)
        crossed = np.zeros(c.shape, dtype=bool)
        crossed[1:] = inside[1:] & ~inside[:-1]
        direction = np.sign(c - o)
        sig = np.where(crossed, -direction if fade else direction, 0).astype(float)
        sig[np.isnan(ratio)] = 0
        return sig

    return f


def build_shape_candidates():
    """Twenty-eight candidates, on ground no previous search covered.

    Deliberately close to `rule_search`'s 41 in size. A larger grid raises the
    Bonferroni threshold and this repository has already shown what happens when
    a wide search meets a permutation null: the 36-cell bracket sweep scored the
    RANDOM rule at 1.76 in sample against the best real candidate's 0.83.
    """
    c = []
    for threshold in (0.4, 0.5, 0.6):
        c.append((f"wick_rej_{threshold}", "wick_rejection", make_wick(threshold)))
        c.append((f"wick_cont_{threshold}", "wick_continuation", make_wick(threshold, invert=True)))
    for threshold in (0.6, 0.7, 0.8):
        c.append((f"body_ride_{threshold}", "body_dominance", make_body(threshold)))
        c.append((f"body_fade_{threshold}", "body_dominance_fade", make_body(threshold, fade=True)))
    for k in (1.5, 2.0):
        c.append((f"range_exp_ride_{k}", "range_expansion", make_range_expansion(20, k)))
        c.append(
            (f"range_exp_fade_{k}", "range_expansion_fade", make_range_expansion(20, k, fade=True))
        )
    for k in (1.0, 1.5):
        c.append((f"body_atr_ride_{k}", "body_vs_volatility", make_body_atr(k)))
        c.append((f"body_atr_fade_{k}", "body_vs_volatility_fade", make_body_atr(k, fade=True)))
    for k in (1.1, 1.3):
        c.append((f"vol_expand_ride_{k}", "vol_regime_expand", make_vol_regime(k)))
        c.append((f"vol_expand_fade_{k}", "vol_regime_expand_fade", make_vol_regime(k, fade=True)))
    for k in (0.9, 0.8):
        c.append(
            (f"vol_contract_ride_{k}", "vol_regime_contract", make_vol_regime(k, contracting=True))
        )
        c.append(
            (
                f"vol_contract_fade_{k}",
                "vol_regime_contract_fade",
                make_vol_regime(k, contracting=True, fade=True),
            )
        )
    return c


def main():
    """Run `rule_search`'s pipeline with only the candidate list swapped.

    Monkeypatched rather than parameterised because `rule_search.py` holds every
    measured figure in `CLAUDE.md` and is not edited to accommodate a new
    caller. The gates, the null and the corrections are byte-identical to the
    ones that rejected the other five families.
    """
    rule_search.build_candidates = build_shape_candidates
    rule_search.main()


if __name__ == "__main__":
    main()
