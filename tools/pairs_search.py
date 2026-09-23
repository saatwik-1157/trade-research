#!/usr/bin/env python3
"""Spread reversion between two majors: the relative-value mechanism, tested.

Twelve searches in CLAUDE.md and not one of them is a two-leg spread trade.
`cross_search.py` is the closest and it is a different mechanism: it RANKS the
seven foreign currencies and holds the top n against the bottom n, so its
signal is relative strength and its weights are +-1/n. A pairs trade fits a
HEDGE RATIO between two specific series and bets that their fitted spread is
stationary. Ranking needs no stationarity claim; this does, and that claim is
the thing being tested.

**Two currencies sharing a dollar leg is the cleanest cointegration story
available here**, which is why the mechanism deserves a run rather than a
dismissal: AUDUSD and NZDUSD are two commodity currencies against one dollar,
so the dollar cancels in the spread and what is left is AUD against NZD.

**The cost is DOUBLE and this is the first thing to get right.** A single-leg
trade crosses the spread twice, entering and exiting. A pair crosses it FOUR
times -- both legs in, both legs out -- so the hurdle this has to clear is
about twice the one every other search in this file faced. `cost_hurdle.py`
puts the single-leg drag at 0.0146R on the three cheapest majors; a pair is
not a cheaper way to trade, it is a more expensive one that has to earn its
way back.

**Three lookahead traps, all of which manufacture an edge rather than break
anything visibly.**

  * **The hedge ratio.** Fitting beta by OLS over the whole sample and then
    trading the resulting spread is the classic one. The spread is
    constructed to be mean-zero across exactly the period being traded, so it
    reverts by construction. Beta here is refitted on a TRAILING window that
    ends before the bar being traded.
  * **The z-score.** Its mean and standard deviation are the same trap one
    level down, and are taken over a trailing window ending at t-1.
  * **The bar itself.** A position decided at t must be decided from data
    strictly before t, and earn t's return. `spread_z` touches nothing at or
    after t, and a test asserts it algebraically rather than hoping.

**The control that matters here is NOT the shuffle.** Two independent random
walks regress on each other with a significant t-statistic about 75% of the
time -- the Granger-Newbold spurious regression -- so "these two series look
cointegrated" is the null's normal behaviour, not evidence against it. The
decisive control is therefore a SYNTHETIC pair: two independent random walks
with the volatilities of the real pair, traded by the identical code. If the
synthetic pairs score like the real ones, the mechanism is spurious
regression and nothing else.

    python tools/pairs_search.py
    python tools/pairs_search.py --synthetic-control
    python tools/pairs_search.py --self-test
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from cross_search import FOREIGN_IS_BASE, align, foreign_returns
from rule_backtest import choose_spread, connect, fetch_rates
from rule_search import z_for

TRADING_HOURS = 252.0 * 24.0


def hedge_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """OLS slope of `a` on `b`, no intercept beyond de-meaning.

    A function so the caller cannot quietly fit it over the whole sample:
    every call site passes an explicit slice, and the test drives this with a
    known ratio.
    """
    bc = b - b.mean()
    denom = float(np.dot(bc, bc))
    if denom <= 0.0:
        return float("nan")
    return float(np.dot(a - a.mean(), bc) / denom)


def spread_z(pa: np.ndarray, pb: np.ndarray, fit: int, look: int,
             refit: int) -> tuple[np.ndarray, np.ndarray]:
    """z-score of the fitted spread, and the beta used, both knowable at t-1.

    Column t uses prices up to and including t-1 and NOTHING at or after t.
    Beta is refitted every `refit` bars on the trailing `fit` bars; between
    refits the previously fitted beta is carried, which is what a desk would
    do and is strictly more conservative than refitting continuously.
    """
    n = len(pa)
    z = np.full(n, np.nan)
    betas = np.full(n, np.nan)
    beta = float("nan")
    start = fit + look + 1
    for t in range(start, n):
        if not np.isfinite(beta) or (t - start) % refit == 0:
            beta = hedge_ratio(pa[t - fit - 1:t - 1], pb[t - fit - 1:t - 1])
        if not np.isfinite(beta):
            continue
        s_hist = pa[t - look - 1:t - 1] - beta * pb[t - look - 1:t - 1]
        sd = float(s_hist.std())
        if sd <= 0.0:
            continue
        s_now = pa[t - 1] - beta * pb[t - 1]
        z[t] = (s_now - float(s_hist.mean())) / sd
        betas[t] = beta
    return z, betas


def pair_pnl(ra: np.ndarray, rb: np.ndarray, z: np.ndarray, betas: np.ndarray,
             cost_a: float, cost_b: float, entry: float,
             exit_: float) -> tuple[np.ndarray, int]:
    """Net return per bar of the two-leg book, and the number of round trips.

    Weights are gross-normalised to 1 so the series is comparable with
    `cross_search`'s portfolios, and cost is charged on TURNOVER of each leg
    -- `|dw| * spread` -- so a position held through a bar is not re-charged.
    Entering and exiting therefore costs four crossings in total, which is the
    point of the exercise.
    """
    n = len(z)
    out = np.zeros(n)
    wa = wb = 0.0
    trips = 0
    for t in range(n):
        zt = z[t]
        if np.isfinite(zt):
            beta = betas[t]
            gross = 1.0 + abs(beta)
            if zt >= entry:                       # spread rich: short it
                na, nb = -1.0 / gross, beta / gross
            elif zt <= -entry:                    # spread cheap: long it
                na, nb = 1.0 / gross, -beta / gross
            elif abs(zt) <= exit_:
                na = nb = 0.0
            else:
                na, nb = wa, wb                   # inside the band: hold
            if na != wa or nb != wb:
                out[t] -= abs(na - wa) * cost_a + abs(nb - wb) * cost_b
                if wa == 0.0 and na != 0.0:
                    trips += 1
                wa, wb = na, nb
        out[t] += wa * ra[t] + wb * rb[t]
    return out, trips


def sharpe_t(series: np.ndarray) -> tuple[float, float]:
    """Annualised Sharpe and the t-statistic of the mean. nan on a dead book."""
    live = series[np.isfinite(series)]
    if live.size < 100 or live.std() <= 0.0:
        return float("nan"), float("nan")
    sharpe = float(live.mean() / live.std() * math.sqrt(TRADING_HOURS))
    t = float(live.mean() / (live.std() / math.sqrt(live.size)))
    return sharpe, t


# ============================================================ self-test ====
# Run BEFORE believing any null this file reports. A search that cannot find
# an effect that is really there has not measured absence, it has measured its
# own blindness -- and `cross_search.py` is the only other search here that
# checked before concluding.

def self_test() -> int:
    failed: list[str] = []

    def check(label, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {label:56s} got={got} want={want}")
        if not ok:
            failed.append(label)

    rng = np.random.default_rng(7)

    # 1. The fitter recovers a ratio it is handed.
    b = np.cumsum(rng.normal(0, 1, 4000))
    a = 1.75 * b + rng.normal(0, 0.01, 4000)
    check("hedge_ratio recovers a planted 1.75", round(hedge_ratio(a, b), 2), 1.75)

    # 2. CAUSALITY, asserted algebraically rather than hoped for. A spike in
    #    the price at bar k must be invisible to z at k and visible at k+1.
    n = 3000
    pa = np.cumsum(rng.normal(0, 0.001, n))
    pb = np.cumsum(rng.normal(0, 0.001, n))
    z0, _ = spread_z(pa, pb, fit=200, look=100, refit=50)
    k = 2000
    bumped = pa.copy()
    bumped[k] += 0.05
    z1, _ = spread_z(bumped, pb, fit=200, look=100, refit=50)
    check("a bump at bar k leaves z AT k unchanged",
          bool(np.isclose(z0[k], z1[k], equal_nan=True)), True)
    check("and changes z at k+1, so the signal is live not blind",
          bool(not np.isclose(z0[k + 1], z1[k + 1], equal_nan=True)), True)

    # 3. POWER: a genuinely cointegrated pair must be found. pa tracks pb with
    #    a stationary, strongly mean-reverting error, which is the textbook
    #    case the mechanism claims to exploit.
    n = 20000
    pb = np.cumsum(rng.normal(0, 0.001, n))
    err = np.zeros(n)
    for i in range(1, n):
        err[i] = 0.90 * err[i - 1] + rng.normal(0, 0.002)
    pa = 1.30 * pb + err
    ra, rb = np.diff(pa, prepend=pa[0]), np.diff(pb, prepend=pb[0])
    z, betas = spread_z(pa, pb, fit=1000, look=200, refit=250)
    pnl, trips = pair_pnl(ra, rb, z, betas, 0.0, 0.0, 2.0, 0.5)
    _s, t_planted = sharpe_t(pnl)
    print(f"        planted cointegration: t={t_planted:+.2f} over {trips} trips")
    check("a real cointegrated pair is found", bool(t_planted > 5.0), True)

    # 4. RESTRAINT: two independent random walks must NOT be. This is the
    #    spurious-regression case, and it is the one that would quietly turn
    #    into a finding.
    pa = np.cumsum(rng.normal(0, 0.001, n))
    pb = np.cumsum(rng.normal(0, 0.001, n))
    ra, rb = np.diff(pa, prepend=pa[0]), np.diff(pb, prepend=pb[0])
    z, betas = spread_z(pa, pb, fit=1000, look=200, refit=250)
    pnl, _ = pair_pnl(ra, rb, z, betas, 0.0, 0.0, 2.0, 0.5)
    _s, t_noise = sharpe_t(pnl)
    print(f"        independent random walks: t={t_noise:+.2f}")
    check("independent walks are not", bool(abs(t_noise) < 3.0), True)

    # 5. Cost is charged on BOTH legs, four crossings a round trip.
    z = np.full(50, np.nan)
    z[10] = 3.0       # enter
    z[20] = 0.0       # exit
    betas = np.full(50, 1.0)
    flat = np.zeros(50)
    free, _ = pair_pnl(flat, flat, z, betas, 0.0, 0.0, 2.0, 0.5)
    charged, trips = pair_pnl(flat, flat, z, betas, 0.001, 0.001, 2.0, 0.5)
    check("one round trip is counted once", trips, 1)
    check("and it is charged on all four crossings",
          round(float(free.sum() - charged.sum()), 6), 0.002)

    if failed:
        print(f"\n  {len(failed)} check(s) failed")
        return 1
    print("\n  all pairs-search self-checks passed")
    return 0


# =============================================================== the run ===

LOOKBACKS = (50, 200)
ENTRY, EXIT_, FIT, REFIT = 2.0, 0.5, 1000, 250


def load(mt5, want: int, timeframe: str):
    """Foreign-currency log-price indices and per-leg costs, on a common window."""
    series, costs = {}, {}
    for sym in FOREIGN_IS_BASE:
        rates = fetch_rates(mt5, sym, want, timeframe=timeframe)
        if rates is None or len(rates) < 2000:
            print(f"  {sym}: too few bars, dropped")
            continue
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spread, _note = choose_spread(rates, info, tick, source="median")
        series[sym] = (rates["time"], rates["close"])
        # Cost as a RETURN, because the book is measured in log returns. A
        # spread in price units divided by price is the only conversion that
        # makes USDJPY's 0.015 and EURUSD's 0.00002 the same quantity -- the
        # metals error one level down.
        costs[sym] = float(spread) / float(np.median(rates["close"]))
    syms, _common, closes = align(series)
    prices = {}
    for i, s in enumerate(syms):
        prices[s] = np.cumsum(foreign_returns(closes[i], FOREIGN_IS_BASE[s]))
    return syms, prices, costs


def synthetic(prices: dict, seed: int) -> dict:
    """Independent random walks with each real series' own volatility.

    The control that decides this search. Two unrelated random walks regress
    on each other with a significant slope most of the time, so a spread that
    looks tradable is the NULL's ordinary behaviour here rather than evidence
    against it. Matching the volatility and destroying every relationship is
    the only way to tell the two apart.
    """
    rng = np.random.default_rng(seed)
    out = {}
    for s, p in prices.items():
        vol = float(np.diff(p).std())
        out[s] = np.cumsum(rng.normal(0.0, vol, len(p)))
    return out


def run(prices: dict, costs: dict, split: float, label: str) -> list[dict]:
    rows = []
    for a, b in itertools.combinations(sorted(prices), 2):
        pa, pb = prices[a], prices[b]
        ra = np.diff(pa, prepend=pa[0])
        rb = np.diff(pb, prepend=pb[0])
        cut = int(len(pa) * split)
        for look in LOOKBACKS:
            z, betas = spread_z(pa, pb, FIT, look, REFIT)
            pnl, trips = pair_pnl(ra, rb, z, betas, costs.get(a, 0.0),
                                  costs.get(b, 0.0), ENTRY, EXIT_)
            s_in, t_in = sharpe_t(pnl[:cut])
            s_out, t_out = sharpe_t(pnl[cut:])
            rows.append({
                "pair": f"{a}/{b}", "lookback": look, "trips": trips,
                "sharpe_in": s_in, "t_in": t_in,
                "sharpe_out": s_out, "t_out": t_out,
                "mean_beta": float(np.nanmean(betas)) if np.isfinite(betas).any()
                else float("nan"),
                "arm": label,
            })
    return rows


def report(rows: list[dict], label: str) -> dict:
    live = [r for r in rows if r["t_in"] == r["t_in"]]
    if not live:
        print(f"  {label}: no candidate produced a usable book")
        return {}
    thresh = z_for(0.05 / len(live))
    best = max(live, key=lambda r: r["t_in"])
    cleared = sum(1 for r in live if r["t_out"] >= 1.96)
    print(f"\n  {label}: {len(live)} candidates, Bonferroni threshold {thresh:.3f}")
    print(f"  {'pair':18}{'look':>6}{'trips':>8}{'t in':>9}{'t out':>9}"
          f"{'Sharpe out':>12}")
    for r in sorted(live, key=lambda r: -r["t_in"])[:5]:
        print(f"  {r['pair']:18}{r['lookback']:>6}{r['trips']:>8,}"
              f"{r['t_in']:>+9.2f}{r['t_out']:>+9.2f}{r['sharpe_out']:>+12.2f}")
    med = float(np.median([r["sharpe_out"] for r in live
                           if r["sharpe_out"] == r["sharpe_out"]]))
    print(f"  best in-sample {best['t_in']:+.2f} ({best['pair']}, "
          f"look {best['lookback']}), out {best['t_out']:+.2f}")
    print(f"  {cleared} of {len(live)} cleared 1.96 out of sample "
          f"against {len(live) * 0.025:.1f} expected by chance")
    print(f"  median out-of-sample Sharpe {med:+.3f}")
    return {"candidates": len(live), "bonferroni": thresh,
            "best_t_in": best["t_in"], "best_pair": best["pair"],
            "best_t_out": best["t_out"], "cleared_out": cleared,
            "expected_by_chance": len(live) * 0.025,
            "median_sharpe_out": med}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--split", type=float, default=0.7)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--out", default="reports/pairs_search.json")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    mt5 = connect()
    syms, prices, costs = load(mt5, args.bars, args.timeframe)
    mt5.shutdown()
    if len(syms) < 2:
        print("  fewer than two symbols survived; nothing to pair")
        return 1
    n = len(next(iter(prices.values())))
    print(f"\n  spread reversion, {len(syms)} majors -> "
          f"{len(syms) * (len(syms) - 1) // 2} pairs x {len(LOOKBACKS)} lookbacks, "
          f"{n:,} {args.timeframe} bars on a common window")
    print(f"  entry |z|>{ENTRY}, exit |z|<{EXIT_}, beta refit every {REFIT} "
          f"bars on the trailing {FIT}")
    print("  cost is FOUR spread crossings a round trip, both legs in and out")

    real = run(prices, costs, args.split, "real")
    r_sum = report(real, "REAL PAIRS")
    ctrl = run(synthetic(prices, 20260923), costs, args.split, "synthetic")
    c_sum = report(ctrl, "SYNTHETIC CONTROL (independent walks, matched vol)")

    print("\n  Two independent random walks regress on each other with a\n"
          "  significant slope most of the time, so the synthetic arm is the\n"
          "  gate that matters. Read the real arm AGAINST it, not against zero.")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"real": r_sum, "synthetic": c_sum,
                   "rows": real + ctrl,
                   "config": {"timeframe": args.timeframe, "bars": n,
                              "entry": ENTRY, "exit": EXIT_, "fit": FIT,
                              "refit": REFIT, "lookbacks": list(LOOKBACKS),
                              "split": args.split}}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
