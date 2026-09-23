#!/usr/bin/env python3
"""Do round numbers stop price? The one externally published FX claim on the shelf.

Nineteenth search. Every other candidate in this project came from a trading
book with no evidence behind it; this one has a citation. Kaufman points at
Carol Osler, *Support for Resistance: Technical Analysis and Intraday Exchange
Rates* (FRBNY Economic Policy Review, July 2000), which found that support and
resistance levels published by six trading firms predicted intraday price
interruptions, and that the levels stayed informative for about five days. The
folklore version is that stop orders cluster at round numbers -- the "big
figure" -- so price pierces them and snaps back.

**The two books disagree on the sign and neither resolves it**: one has round
numbers as reversal levels, the other trades round-figure BREAKOUTS. So the
test is two-sided and the null decides.

**A rounding artefact in the first version of this measurement produced a
finding, and removing it reversed the answer. It is documented here because it
is the most instructive part.** Converting a price to whole pips with
`np.rint` invokes numpy's banker's rounding: a value exactly at half a pip
goes to the nearest EVEN pip. About 10% of this broker's quotes sit exactly on
a half pip, so every one of them was pushed to an even digit. Measured, the
last whole-pip digit came out **1.07x expected on even digits and 0.93x on
odd**, identically in all seven majors, and the digit profiles correlated
**+0.607** across pairs -- which reads exactly like a real, shared
microstructure effect. It was numpy. Converting through integer tenths of a
pip, where no tie exists, the even/odd split collapses to 1.0014 / 0.9986, the
cross-pair correlation falls to **+0.084**, and the chi-square against uniform
drops from 3,861 to 510.

**The null is already inside the data, which is what makes the clustering test
clean.** The mass at digit *d* is precisely what the mass at "00" would be if
the round number sat at *d* instead. So no shuffling is required: ranking digit
00 among all 100 is a complete two-sided test against the same price path, the
same lingering and the same sample.

    python tools/round_number_search.py
    python tools/round_number_search.py --spacing 50
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
from rule_backtest import choose_spread, connect, fetch_rates

# A pip is the fourth decimal, or the second for a JPY cross. Stated rather
# than derived from `point`, because this broker quotes a fractional fifth
# digit and `point` is the fractional pip, not the pip.
PIP = {"EURUSD": 1e-4, "GBPUSD": 1e-4, "AUDUSD": 1e-4, "NZDUSD": 1e-4,
       "USDCHF": 1e-4, "USDCAD": 1e-4, "USDJPY": 1e-2}


def to_pips(px, pip):
    """Whole pips as integers, with NO rounding tie.

    `np.rint(px / pip)` is wrong here and the error is invisible: numpy rounds
    a half to the nearest EVEN integer, and about a tenth of this broker's
    quotes sit exactly on a half pip, so every one of them lands on an even
    digit. Going through integer tenths of a pip removes the tie entirely.
    """
    return np.rint(np.asarray(px, float) / (pip / 10.0)).astype(np.int64) // 10


def digit_profile(highs, lows, pip, spacing):
    """How often each position within a `spacing`-pip cycle holds an extreme."""
    h = to_pips(highs, pip) % spacing
    low = to_pips(lows, pip) % spacing
    v = np.bincount(h, minlength=spacing) + np.bincount(low, minlength=spacing)
    return v.astype(float)


def neighbourhood(rel, spacing, width):
    """Mass within `width` pips either side of the round level, per position.

    Osler's claim is about price being INTERRUPTED near a level, not about an
    extreme printing exactly on it, so the band is the fair reading and the
    exact digit is the sharp one. Both are reported.
    """
    idx = [(i % spacing) for i in range(-width, width + 1)]
    return float(rel[idx].mean())


def pierce_test(times, highs, lows, closes, pip, spacing, offset, horizon):
    """Reversal after price pierces a level, at `offset` pips from round.

    A pierce is a bar whose high crosses a level from below, or whose low
    crosses one from above. The score is the forward return SIGNED AGAINST the
    pierce, so a positive number means price came back -- the folklore claim.
    `offset` moves the whole grid off the round number, which is the control:
    identical mechanics, a level nobody watches.
    """
    hp, lp, cp = to_pips(highs, pip), to_pips(lows, pip), to_pips(closes, pip)
    n = len(cp)
    scores = []
    for t in range(1, n - horizon):
        prev = cp[t - 1]
        # the nearest grid level strictly above the previous close
        up = ((prev - offset) // spacing + 1) * spacing + offset
        dn = ((prev - offset) // spacing) * spacing + offset
        fwd = math.log(closes[t + horizon] / closes[t])
        if hp[t] >= up > prev:
            scores.append(-fwd)          # pierced up; reversal is a fall
        elif lp[t] <= dn < prev:
            scores.append(fwd)           # pierced down; reversal is a rise
    return np.array(scores)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", type=int, default=50000)
    ap.add_argument("--spacing", type=int, default=100, help="pips between levels")
    ap.add_argument("--width", type=int, default=3, help="pips either side")
    ap.add_argument("--horizon", type=int, default=1, help="bars ahead")
    ap.add_argument("--out", default="reports/round_number_search.json")
    args = ap.parse_args()
    sp = args.spacing

    mt5 = connect()
    data, costs = {}, {}
    for sym, pip in PIP.items():
        r = fetch_rates(mt5, sym, args.bars, timeframe="H1")
        if r is None or len(r) < 10000:
            print(f"  {sym}: too few bars")
            continue
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spread, _n = choose_spread(r, info, tick, source="median")
        costs[sym] = float(spread) / float(np.median(r["close"]))
        data[sym] = r
    mt5.shutdown()
    if not data:
        return 1

    total = np.zeros(sp)
    profiles = {}
    for sym, r in data.items():
        v = digit_profile(r["high"], r["low"], PIP[sym], sp)
        profiles[sym] = v / v.mean()
        total += v
    rel = total / total.mean()
    exp = total.sum() / sp
    chi2 = float(((total - exp) ** 2 / exp).sum())
    rank0 = int((rel > rel[0]).sum()) + 1

    print(f"\n  round numbers, {len(data)} majors, {sum(len(r) for r in data.values()):,}"
          f" H1 bars, levels every {sp} pips")
    print("  " + "-" * 70)
    print(f"  chi2 against uniform ({sp - 1} df): {chi2:.1f}")
    print(f"  extremes exactly ON the round level: {rel[0]:.4f}x expected, "
          f"**rank {rank0} of {sp}**")
    if sp >= 50:
        print(f"  exactly on the half level:           {rel[sp // 2]:.4f}x expected, "
              f"rank {int((rel > rel[sp // 2]).sum()) + 1} of {sp}")
    band = neighbourhood(rel, sp, args.width)
    print(f"  within {args.width} pips either side:  {band:.4f}x expected")
    order = np.argsort(-rel)
    print(f"  most-visited positions: "
          f"{[(int(d), round(float(rel[d]), 3)) for d in order[:5]]}")

    # The even/odd check that caught the rounding artefact stays in the output.
    last = np.array([rel[d::10].mean() for d in range(10)])
    print(f"  even/odd last pip digit: {last[::2].mean():.4f} / {last[1::2].mean():.4f}"
          f"   (a banker's-rounding artefact gave 1.07 / 0.93)")

    print("  " + "-" * 70)
    print(f"  {'grid offset':14}{'pierces':>10}{'mean reversal %':>18}{'t':>8}")
    arms = []
    for offset in (0, 7, 13, 23, 37, 50, 61, 83):
        allsc = []
        for sym, r in data.items():
            allsc.append(pierce_test(r["time"], r["high"].astype(float),
                                     r["low"].astype(float),
                                     r["close"].astype(float),
                                     PIP[sym], sp, offset, args.horizon))
        s = np.concatenate(allsc)
        t = float(s.mean() / (s.std(ddof=1) / math.sqrt(len(s)))) if len(s) > 30 else float("nan")
        tag = "  <- ROUND" if offset == 0 else ""
        print(f"  {('+' + str(offset) + ' pips'):14}{len(s):>10,}"
              f"{s.mean() * 100:>+18.4f}{t:>+8.2f}{tag}")
        arms.append({"offset": offset, "n": int(len(s)),
                     "mean_reversal_pct": float(s.mean() * 100), "t": t})

    round_t = arms[0]["t"]
    others = [a["t"] for a in arms[1:] if a["t"] == a["t"]]
    beat = sum(1 for t in others if abs(t) >= abs(round_t))
    mean_cost = float(np.mean(list(costs.values()))) * 2 * 100
    print("  " + "-" * 70)
    print(f"  {beat} of {len(others)} arbitrary offsets score |t| at least as "
          f"large as the round level's {abs(round_t):.2f}")
    print(f"  round-level reversal {arms[0]['mean_reversal_pct']:+.4f}% against a "
          f"round-trip cost of {mean_cost:.4f}%")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"spacing": sp, "chi2_vs_uniform": chi2,
                   "on_level_rel": float(rel[0]), "on_level_rank": rank0,
                   "band_rel": band, "band_width": args.width,
                   "even_odd": [float(last[::2].mean()), float(last[1::2].mean())],
                   "profile": [float(x) for x in rel],
                   "pierce_arms": arms, "offsets_beating_round": beat,
                   "cost_round_trip_pct": mean_cost,
                   "horizon": args.horizon}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
