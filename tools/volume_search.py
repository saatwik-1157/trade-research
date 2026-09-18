#!/usr/bin/env python
"""Search the one input this project has never measured: traded volume.

WHY THIS IS NOT A SEVENTH NULL DRESSED UP AS NEW GROUND
-------------------------------------------------------
Every family searched here is a pure function of OHLC. RSI, MA crosses,
Donchian, Bollinger, momentum and their inverses; candle shape and volatility
regime; eight exit structures; a 36-cell bracket sweep; an hour-of-day filter.
Volume appears in none of them -- `grep -n volume` over `rule_search.py`,
`rule_backtest.py`, `exit_search.py` and `crypto_market.py` returned nothing
before this run existed.

`CLAUDE.md` records volume as untested and gives the reason: MT5 reports
`real_volume` as 0 on spot FX and `tick_volume` as a count of quote updates
rather than size. That reasoning is right, and it is about MT5. A ccxt
exchange reports size actually traded, and `crypto_market.py` was already
downloading it in column 5 and discarding it. So the question is answerable
on exactly one of this project's five universes, and only there.

WHAT IS ACTUALLY BEING TESTED
-----------------------------
A conviction-weighted return: today's return multiplied by how unusual today's
volume is against its own recent baseline.

    r  = c[t]/c[t-1] - 1
    vr = v[t] / mean(v[t-n:t])        # baseline EXCLUDES today
    f  = r * vr

A 1% move on triple volume and a 3% move on a third of it score the same. The
claim under test is that this ordering carries information the return alone
does not.

**The return leg on its own is momentum at n=1, which is refuted here.** So
the novelty is strictly the weighting, and a run without the unweighted
control measures nothing -- a positive result would be indistinguishable from
the momentum family scoring on a universe where it was already searched. Two
of the six candidates are that control.

TWO DESIGN CHOICES THAT ARE MINE, NOT THE SOURCE'S
--------------------------------------------------
The construct comes from `ai-trader`'s ETF-flow snapshot, which cuts at an
absolute +/-2.5. Two departures, both recorded because a reader should be able
to tell what was inherited from what was decided:

1. **A trailing quantile, not an absolute cut.** 2.5 was written for US ETFs
   whose daily returns run about 1%. On BTC daily it is an ordinary Tuesday.
   Worse, an absolute cut makes the weighted and unweighted arms fire a
   DIFFERENT NUMBER OF TIMES, and trade frequency is the one lever this
   project has shown has a measured sign. A quantile fires both arms at the
   same rate, so the permutation null isolates the weighting rather than the
   exposure.

2. **`VMAX`, an upper clip on the volume ratio, PRE-REGISTERED at 5.0.** The
   source floors the ratio at 0.1 and leaves it unbounded above, so one
   listing event or halt-and-reopen produces a ratio that dominates every
   other bar in the sample. 5.0 is fixed here before the run. Choosing it
   after seeing results would make it a free parameter, and this project has
   already watched a 36-cell sweep hand its best score to a random rule.

THE PRIOR, WRITTEN BEFORE THE RUN
---------------------------------
Six families across five universes have failed here and the only effects ever
large enough to measure were negative and were cost. The honest prior is that
this fails too. What would count as a finding: the weighted arms clearing the
Bonferroni threshold, beating the permutation null, holding out of sample,
AND separating from the unweighted controls. The last is the one that matters;
without it there is no volume result, only a momentum result.

    python tools/volume_search.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rule_search  # noqa: E402

#: Pre-registered before the run. See the docstring.
VMAX = 5.0
VMIN = 0.1
#: Bars of history the trigger threshold is set from. 250 is about a trading
#: year at D1 and leaves ~2,750 usable bars of the 3,000 fetched.
LOOKBACK = 250


def conviction(c, v, n: int | None):
    """Return, optionally weighted by how unusual today's volume is.

    `n` None means the unweighted control: f is the bare 1-bar return, which
    is momentum at its shortest lookback and is the thing the weighting has to
    beat to have measured anything.

    The baseline EXCLUDES today. `mean(v[t-n:t])` is the n bars BEFORE t, so
    today's volume is compared against a window it is not part of -- including
    it would damp exactly the spike the rule exists to detect, and on a
    short window would damp it a lot.
    """
    r = np.full(c.shape, np.nan)
    r[1:] = c[1:] / c[:-1] - 1.0
    if n is None:
        return r
    if v is None:
        return np.full(c.shape, np.nan)

    vv = np.where(np.isfinite(v) & (v > 0), v, np.nan)
    base = np.full(c.shape, np.nan)
    csum = np.nancumsum(np.nan_to_num(vv, nan=0.0))
    cnt = np.cumsum(np.isfinite(vv).astype(float))
    for t in range(n, len(c)):
        total = csum[t - 1] - (csum[t - n - 1] if t - n - 1 >= 0 else 0.0)
        k = cnt[t - 1] - (cnt[t - n - 1] if t - n - 1 >= 0 else 0.0)
        if k > 0:
            base[t] = total / k

    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vv / base
    vr = np.clip(vr, VMIN, VMAX)
    return r * vr


def make_conviction(n: int | None, q: float, lookback: int = LOOKBACK):
    """Trade the tails of the score's own trailing distribution.

    The threshold is computed from bars STRICTLY BEFORE t and today is then
    compared against it. Including today would put the bar being judged into
    the distribution judging it, which is a look-ahead small enough to survive
    review and large enough to matter at the tails -- the one place this rule
    ever fires.
    """
    def f(o, h, l, c, v=None):
        score = conviction(c, v, n)
        sig = np.zeros(c.shape, dtype=int)
        if not np.any(np.isfinite(score)):
            # A market with no volume. Return no signal rather than a
            # substitute: MT5's tick count is a different quantity wearing
            # the same name, and pretending otherwise is the metals error.
            return sig
        for t in range(lookback, len(c)):
            w = score[t - lookback:t]
            w = w[np.isfinite(w)]
            if w.size < lookback // 2 or not np.isfinite(score[t]):
                continue
            hi = np.quantile(w, 1.0 - q)
            lo = np.quantile(w, q)
            if score[t] >= hi:
                sig[t] = 1
            elif score[t] <= lo:
                sig[t] = -1
        return sig
    return f


def build_volume_candidates():
    """Six candidates: two controls and four weighted. Threshold 2.638.

    Deliberately not twelve. The Bonferroni threshold is computed over the
    true candidate count, so adding the inverse of each would cost 0.23 of a
    t-stat for a question that is only worth asking if the primary shows a
    consistent sign. Run the inverses as a follow-up or not at all.
    """
    c = []
    for q in (0.10, 0.20):
        c.append((f"ret_q{int(q*100)}", "return_control",
                  make_conviction(None, q)))
    for n in (5, 20):
        for q in (0.10, 0.20):
            c.append((f"cvr_{n}_q{int(q*100)}", "conviction_volume",
                      make_conviction(n, q)))
    return c


def main():
    """Run `rule_search` against this candidate set, crypto only.

    `--source crypto` is forced rather than defaulted. On an MT5 market every
    weighted candidate would return an all-zero signal and be reported as
    having too few trades, which reads as a null when it is an absence of
    data -- the most misleading shape a result can take here.
    """
    argv = sys.argv[1:]
    if "--source" not in " ".join(argv):
        sys.argv = [sys.argv[0], "--source", "crypto", "--timeframe", "1d"] + argv
    rule_search.build_candidates = build_volume_candidates
    return rule_search.main()


if __name__ == "__main__":
    raise SystemExit(main())
