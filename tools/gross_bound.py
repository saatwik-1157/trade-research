#!/usr/bin/env python3
"""Is the gross expectancy positive? It is bounded by a 0.94% tie assumption.

`CLAUDE.md` records an implied GROSS expectancy of +0.0064R, arrived at as a
residual: the observed -0.0235R a trade minus an assumed 0.0299R of drag. The
whole case for the account being one cost-reduction away from break-even rests
on that number, and it was never measured. This measures it.

A symmetric 1.5xATR bracket entered at random on a martingale should give
EXACTLY zero gross. Any departure is therefore attributable rather than
mysterious, and the attribution is the point.

**The tie rule is the entire question.** When one bar's range covers both the
stop and the target, the intrabar order is unknown. `rule_backtest.simulate`
books the LOSS, deliberately -- its docstring says assuming the win is the
classic way a backtest flatters itself. Ties are under 1% of trades and they
move the answer by more than the effect being argued about, so this reports
BOTH resolutions and treats the truth as bracketed rather than known.

    python tools/gross_bound.py
    python tools/gross_bound.py --seeds 6
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import atr_series, connect, fetch_rates

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD")


def bracket_r(o, h, l, c, sig, atr, tie_to_loss=True,
              sl_atr=1.5, tp_atr=1.5, max_hold=240):
    """R-multiples of a bracketed run, and how many bars covered both levels.

    Deliberately NOT a call into `simulate`: that function hard-codes the
    loss-wins-ties rule, and the whole purpose here is to vary it. The rest of
    the walk is the same -- entry at the next bar's open from a signal read on
    closed bars, flat before the next entry.
    """
    out, ties = [], 0
    i, n = 60, len(c)
    while i < n - 1:
        s = sig[i - 1]
        if s == 0 or not np.isfinite(atr[i - 1]) or atr[i - 1] <= 0:
            i += 1
            continue
        entry, a = o[i], atr[i - 1]
        sl, tp = ((entry - sl_atr * a, entry + tp_atr * a) if s > 0
                  else (entry + sl_atr * a, entry - tp_atr * a))
        px = None
        for j in range(i, min(i + max_hold, n)):
            hit_sl, hit_tp = ((l[j] <= sl, h[j] >= tp) if s > 0
                              else (h[j] >= sl, l[j] <= tp))
            if hit_sl and hit_tp:
                ties += 1
                px = sl if tie_to_loss else tp
                break
            if hit_sl:
                px = sl
                break
            if hit_tp:
                px = tp
                break
        if px is None:
            px = c[j]
        gross = (px - entry) if s > 0 else (entry - px)
        out.append(gross / (sl_atr * a))
        i = j + 1
    return out, ties


def summarise(r):
    r = np.asarray(r, float)
    se = r.std(ddof=1) / math.sqrt(len(r))
    return {"n": int(len(r)), "mean_r": float(r.mean()), "se": float(se),
            "t": float(r.mean() / se),
            "ci": [float(r.mean() - 1.96 * se), float(r.mean() + 1.96 * se)],
            "win_rate": float((r > 0).mean())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", type=int, default=50000)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--inferred", type=float, default=0.0064,
                    help="the gross CLAUDE.md infers, for comparison")
    ap.add_argument("--out", default="reports/gross_bound.json")
    args = ap.parse_args()

    mt5 = connect()
    data = {}
    for sym in SYMBOLS:
        r = fetch_rates(mt5, sym, args.bars, timeframe="H1")
        if r is not None and len(r) > 10000:
            data[sym] = tuple(r[k].astype(float)
                              for k in ("open", "high", "low", "close"))
    mt5.shutdown()
    if not data:
        return 1

    print(f"\n  gross expectancy of a 1.5xATR bracket entered at RANDOM")
    print(f"  {len(data)} majors, {args.seeds} seeds, no spread charged")
    print("  a symmetric bracket on a martingale should give EXACTLY zero")
    print("  " + "-" * 68)

    res = {}
    for tie_loss in (True, False):
        pooled, ties = [], 0
        for seed in range(args.seeds):
            rng = np.random.default_rng(1000 + seed)
            for _sym, (o, h, l, c) in data.items():
                a = atr_series(h, l, c)
                sig = rng.choice([-1.0, 1.0], size=len(c))
                rr, tt = bracket_r(o, h, l, c, sig, a, tie_to_loss=tie_loss)
                pooled += rr
                ties += tt
        st = summarise(pooled)
        st["ties"] = ties
        st["tie_share"] = ties / st["n"]
        key = "tie_to_loss" if tie_loss else "tie_to_win"
        res[key] = st
        label = ("LOSS (simulate's default, conservative)" if tie_loss
                 else "WIN  (the optimistic bound)")
        print(f"  tie -> {label}")
        print(f"    n {st['n']:,}   ties {ties:,} ({st['tie_share'] * 100:.2f}%)"
              f"   gross R {st['mean_r']:+.5f}   t {st['t']:+.2f}")
        print(f"    95% CI [{st['ci'][0]:+.5f}, {st['ci'][1]:+.5f}]"
              f"   win rate {st['win_rate'] * 100:.2f}%")

    lo = res["tie_to_loss"]["mean_r"]
    hi = res["tie_to_win"]["mean_r"]
    neutral = (lo + hi) / 2
    print("  " + "-" * 68)
    print(f"  the truth is bracketed: [{lo:+.5f}, {hi:+.5f}]R, "
          f"neutral 50/50 resolution {neutral:+.5f}R")
    print(f"  CLAUDE.md infers {args.inferred:+.5f}R, which is "
          f"{(args.inferred - lo) / (hi - lo) * 100:.0f}% of the way to the "
          f"OPTIMISTIC end")
    inside = res["tie_to_loss"]["ci"][0] <= args.inferred <= res["tie_to_loss"]["ci"][1]
    print(f"  is it inside the conservative interval? {'yes' if inside else 'NO'}")
    print("\n  believing a positive gross means assuming every ambiguous bar")
    print("  resolved in the position's favour. Ties are under 1% of trades and")
    print("  they move the answer by more than the effect being argued about.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"results": res, "neutral_r": neutral,
                   "inferred_r": args.inferred, "bracket": [lo, hi],
                   "symbols": list(data), "seeds": args.seeds}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
