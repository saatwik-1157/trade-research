#!/usr/bin/env python3
"""Tick volume on FX: the last untested input, and the trap that comes with it.

Twenty-third search. CLAUDE.md has recorded this input as untested from the
beginning -- *"MT5 supplies TICK volume, a count of quote updates rather than
traded size"* -- and `volume_search` tested real traded volume on Binance
crypto, never tick volume here. `real_volume` is measured at **all zero** on
this venue's FX, so tick volume is not a proxy for size: it is the only volume
that exists.

**Kaufman's warning decides the whole design, and it is measured rather than
taken on faith.** Intraday volume carries a dominant intraday shape, so
comparing a bar to a rolling baseline that spans hours measures the clock
rather than the market. Measured across 50,000 H1 bars per pair, tick volume
by server hour runs **0.27x at hour 0 to 2.13x at hour 17 -- a 7.9x range** --
and the hourly profile correlates **+0.959 across pairs**, so the shape is a
property of the FX day and not of any instrument.

His prescription is therefore mandatory here: every bar is expressed relative
to the SAME BAR-OF-DAY's own recent history, never to the last n bars
whatever hour they fell in. A trailing window, so it stays causal.

**The unnormalised arm is kept as the control, and that is the point of the
run.** If the raw candidates score and the normalised ones do not, the
"finding" was the clock. `volume_search` established the shape of this
argument when its unweighted control beat both weighted arms, and six earlier
searches had no equivalent and are weaker for it.

The candidates are Kaufman's own tick-volume family, chosen because they are
specified rather than described: a spike against a LAGGED baseline (his
construction deliberately ends the baseline five bars before the spike, so a
run-up cannot inflate its own threshold), the Force Index in both its
mean-reverting and trend readings, Accumulation/Distribution, the Chaikin
Volume Accumulator, the Money Flow Index, and the Market Facilitation Index.

    python tools/tick_volume_search.py
    python tools/tick_volume_search.py --raw     # the unnormalised control
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
import rule_search

#: Trailing days of the same bar-of-day used as a bar's own baseline.
SAME_HOUR_DAYS = 20
#: Kaufman's lag: the baseline ends this many bars before the bar it judges,
#: so the two or three bars of run-up into a spike cannot raise the bar it is
#: being measured against.
SPIKE_LAG = 5
RAW = False           # flipped by --raw; the control arm


def _hours(n):
    """Server hour per bar, recovered from position.

    `rule_search` hands candidates o/h/l/c and nothing else, so the hour is
    reconstructed from the bar index. H1 data is contiguous within a trading
    week, which is all the normaliser needs: it groups bars 24 apart, and a
    weekend break shifts the grouping by whole days, never within a day.
    """
    return np.arange(n) % 24


def normalise(v):
    """Express each bar's volume against the same bar-of-day's recent past.

    Causal by construction: the mean is taken over the SAME hour on previous
    days only, never including the bar itself. A bar with too little history
    behind it returns nan rather than a number, because a baseline of one
    observation is not a baseline.
    """
    v = np.asarray(v, float)
    n = len(v)
    out = np.full(n, np.nan)
    for t in range(24 * 2, n):
        past = v[max(0, t - 24 * SAME_HOUR_DAYS):t:24]
        if len(past) >= 5:
            m = past.mean()
            if m > 0:
                out[t] = v[t] / m
    return out


#: Volume keyed by the close series it belongs to. NOT a single global.
VOL_BY_KEY: dict = {}


def _key(c):
    """Identify a symbol by its own close series.

    `rule_search` hands a candidate o/h/l/c and nothing else, so the volume
    has to be looked up rather than passed -- and the lookup has to be per
    SYMBOL. The first version bound one module-level array at fetch time,
    which was wrong twice over: `rule_search` fetches every symbol into a dict
    BEFORE running any candidate, so the binding held only the last one, and
    `trim_to_years` then changed its length. The length guard refused the
    mismatch and every candidate went silent -- 0 of 16 judged.

    Length plus the two endpoint closes is enough: two majors do not share a
    bar count and both endpoints to full float precision.
    """
    c = np.asarray(c, float)
    return (len(c), float(c[0]), float(c[-1]))


def _vol(o, h, l, c):
    """This symbol's tick volume, or nan if it was never bound.

    Returning nan rather than a substitute is deliberate: a candidate with no
    volume must go SILENT and be visibly unjudged, never fall back to a
    price-only proxy that would quietly test a different hypothesis.
    """
    v = VOL_BY_KEY.get(_key(c))
    if v is None or len(v) != len(c):
        return np.full(len(c), np.nan)
    return np.asarray(v, float)


def _prep(o, h, l, c):
    v = _vol(o, h, l, c)
    return v if RAW else normalise(v)


def make_spike(mult, fade=False):
    """A bar whose volume exceeds a LAGGED baseline by `mult`.

    Direction is the bar's own, so a high-volume up bar is read as
    continuation and `fade` takes the other side -- a spike is equally
    readable as exhaustion, and this project does not get to assume which.
    """
    def f(o, h, l, c):
        v = _prep(o, h, l, c)
        n = len(c)
        base = np.full(n, np.nan)
        for t in range(SPIKE_LAG + 20, n):
            w = v[t - 20 - SPIKE_LAG:t - SPIKE_LAG]
            w = w[np.isfinite(w)]
            if len(w) >= 10:
                base[t] = w.mean()
        hot = np.isfinite(base) & np.isfinite(v) & (v > mult * base)
        direction = np.sign(np.asarray(c, float) - np.asarray(o, float))
        sig = np.where(hot, -direction if fade else direction, 0.0)
        sig[~np.isfinite(sig)] = 0.0
        return sig
    return f


def _ema(x, n):
    a = 2.0 / (n + 1.0)
    out = np.full(len(x), np.nan)
    run = None
    for t, val in enumerate(x):
        if not np.isfinite(val):
            continue
        run = val if run is None else a * val + (1 - a) * run
        out[t] = run
    return out


def make_force(n, fade=False):
    """Force Index: the bar's change weighted by its volume, then smoothed.

    Kaufman gives two readings of the same series and they are opposite: a
    2-bar smoothing traded mean-reverting, a 13-bar traded as a trend on the
    zero cross. Both are here because the source offers both.
    """
    def f(o, h, l, c):
        c = np.asarray(c, float)
        v = _prep(o, h, l, c)
        raw = np.concatenate(([0.0], np.diff(c))) * np.nan_to_num(v, nan=0.0)
        e = _ema(raw, n)
        sig = np.sign(e)
        sig[~np.isfinite(sig)] = 0.0
        return -sig if fade else sig
    return f


def make_accum(kind, n=20, fade=False):
    """Accumulation/Distribution or Chaikin, as a close-position weight x volume.

    **Kaufman lists Chaikin's Volume Accumulator and Intraday Intensity as two
    indicators and they are the same one.** His weights are
    `((C-L)/(H-L) - 0.5) * 2` and `((C-L) - (H-C)) / (H-L)`; both reduce to
    `(2C - H - L)/(H - L)`, and over 10,000 random bars they agree to 1.1e-16.
    Carrying both would have put a duplicate in the candidate list and
    inflated the Bonferroni denominator with a hypothesis already counted, so
    only one is kept.

    Accumulation/Distribution is genuinely different: it weights by
    `(C - O)/(H - L)`, which uses the OPEN and is therefore not a function of
    where the close sits in the bar alone.
    """
    def f(o, h, l, c):
        o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
        v = np.nan_to_num(_prep(o, h, l, c), nan=0.0)
        rng = h - l
        with np.errstate(divide="ignore", invalid="ignore"):
            if kind == "ad":
                w = (c - o) / rng
            else:                                    # chaikin == intensity
                w = (((c - l) / rng) - 0.5) * 2.0
        w[~np.isfinite(w)] = 0.0
        line = np.cumsum(w * v)
        sma = np.full(len(c), np.nan)
        if len(c) > n:
            sma[n - 1:] = np.convolve(line, np.ones(n) / n, mode="valid")
        sig = np.sign(line - sma)
        sig[~np.isfinite(sig)] = 0.0
        return -sig if fade else sig
    return f


def make_mfi(n=14, fade=False):
    """Money Flow Index: an RSI computed on typical price times volume."""
    def f(o, h, l, c):
        h, l, c = (np.asarray(x, float) for x in (h, l, c))
        v = np.nan_to_num(_prep(o, h, l, c), nan=0.0)
        tp = (h + l + c) / 3.0
        mf = tp * v
        up = np.zeros(len(c))
        dn = np.zeros(len(c))
        d = np.concatenate(([0.0], np.diff(tp)))
        up[d > 0] = mf[d > 0]
        dn[d < 0] = mf[d < 0]
        su = np.convolve(up, np.ones(n), mode="full")[:len(c)]
        sd = np.convolve(dn, np.ones(n), mode="full")[:len(c)]
        with np.errstate(divide="ignore", invalid="ignore"):
            mfi = 100.0 - 100.0 / (1.0 + su / sd)
        sig = np.where(mfi < 20, 1.0, np.where(mfi > 80, -1.0, 0.0))
        sig[~np.isfinite(mfi)] = 0.0
        sig[:n] = 0.0
        return -sig if fade else sig
    return f


def make_mfacil(fade=False):
    """Bill Williams' Market Facilitation Index: range per unit of volume.

    Traded on its four-state table: range and volume both rising is read as
    confirmation, and the other three states stand aside. That is the source's
    own rule and it makes the candidate sparse, which the firing check below
    is there to notice.
    """
    def f(o, h, l, c):
        h, l, c = (np.asarray(x, float) for x in (h, l, c))
        v = _prep(o, h, l, c)
        with np.errstate(divide="ignore", invalid="ignore"):
            mfi = (h - l) / v
        dv = np.concatenate(([0.0], np.diff(np.nan_to_num(v, nan=0.0))))
        dm = np.concatenate(([0.0], np.diff(np.nan_to_num(mfi, nan=0.0))))
        direction = np.sign(c - np.asarray(o, float))
        conf = (dv > 0) & (dm > 0)
        sig = np.where(conf, direction, 0.0)
        sig[~np.isfinite(sig)] = 0.0
        return -sig if fade else sig
    return f


def build_volume_candidates():
    """Sixteen candidates -- four spike, four force, four accumulation, two
    money-flow, two market-facilitation -- each with its inverse.

    A rule and its inverse cannot both be skill, and the gap between them is
    the cost of trading rather than the timing -- which is how every previous
    search here has read its own table.
    """
    c = []
    for m in (2.0, 3.0):
        c.append((f"vspike_ride_{m}", "volume_spike", make_spike(m)))
        c.append((f"vspike_fade_{m}", "volume_spike_fade", make_spike(m, fade=True)))
    for n in (2, 13):
        c.append((f"force_ride_{n}", "force_index", make_force(n)))
        c.append((f"force_fade_{n}", "force_index_fade", make_force(n, fade=True)))
    for kind in ("ad", "chaikin"):
        c.append((f"{kind}_ride", f"{kind}_line", make_accum(kind)))
        c.append((f"{kind}_fade", f"{kind}_line_fade", make_accum(kind, fade=True)))
    c.append(("mfi_ride", "money_flow", make_mfi()))
    c.append(("mfi_fade", "money_flow_fade", make_mfi(fade=True)))
    c.append(("mfacil_ride", "market_facilitation", make_mfacil()))
    c.append(("mfacil_fade", "market_facilitation_fade", make_mfacil(fade=True)))
    return c


def main():
    """Attach the tick volume, then hand the search to `rule_search`.

    The volume is bound to the module rather than threaded through the signal
    signature because `rule_search` owns that contract and every other search
    here uses it unchanged. `main` patches the loader so each symbol's volume
    is in place before its candidates run.
    """
    global RAW
    if "--raw" in sys.argv:
        RAW = True
        sys.argv.remove("--raw")
    rule_search.build_candidates = build_volume_candidates
    if "--out" not in sys.argv:
        sys.argv += ["--out", "reports/tick_volume_search"
                     + ("_raw" if RAW else "") + ".json"]

    # Bind at TRIM time, not fetch time: trim_to_years runs after the fetch
    # and returns the array the search actually uses, so binding earlier keys
    # the volume to a length that no longer exists.
    original_trim = rule_search.trim_to_years

    def trim_and_bind(rates, years):
        out = original_trim(rates, years)
        if out is not None and len(out) > 0:
            VOL_BY_KEY[_key(out["close"].astype(float))] =                 out["tick_volume"].astype(float)
        return out

    rule_search.trim_to_years = trim_and_bind
    return rule_search.main()


if __name__ == "__main__":
    raise SystemExit(main())
