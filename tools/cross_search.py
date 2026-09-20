#!/usr/bin/env python3
"""Cross-sectional currency portfolios: rank the majors, long the top, short the bottom.

Every search in this repository so far has been TIME-SERIES. Each symbol is
judged on its own history, a signal fires or it does not, and the seven
results are pooled. That construction has a confound this project keeps
rediscovering and keeps having to correct for: the seven majors share a dollar
leg, so one dollar move opens correlated trades in all of them, a pooled
t-statistic counts that move seven times, and `clustered_by_date` exists
entirely to undo it. It has dissolved the two best results the project ever
produced.

A cross-sectional portfolio removes that leg BY CONSTRUCTION rather than
correcting for it afterwards. Rank the seven foreign currencies against the
dollar, go long the strongest n and short the weakest n, and a pure dollar
move lifts or drops every leg together and cancels. What survives is relative
currency strength, which is a different quantity from anything searched here
and the one the factor literature actually trades.

Three things follow from that and they are why this file exists rather than
another `--flag` on `rule_search.py`:

  * **The observation unit changes.** A portfolio has ONE return per bar, not
    seven correlated trades, so there is no date clustering to undo. The
    t-statistic is computed on the portfolio series directly and is the honest
    one, not a pooled figure needing a correction applied to it afterwards.
  * **The null has to change with it.** Shuffling a signal through TIME, which
    is right for a time-series rule, would leave the cross-sectional structure
    intact. The null here permutes WEIGHTS ACROSS SYMBOLS at each rebalance:
    the portfolio holds the same number of legs, the same gross exposure and
    close to the same turnover, and only the question of WHICH currency is
    strongest is destroyed. Anything the real portfolio earns over that null
    is ranking skill and nothing else.
  * **Cost has to be charged on turnover, not on trades.** A leg held through
    a rebalance costs nothing. `sum(|w_new - w_old| * spread)` is the honest
    charge and it is the one that makes the slow variants look as good as they
    should - frequency is the only lever this repository has ever shown to
    have a sign, and it points down.

Direction convention, which is the easiest thing to get silently wrong here.
Four majors quote the foreign currency as BASE (EURUSD, GBPUSD, AUDUSD,
NZDUSD) and three quote the dollar as base (USDJPY, USDCAD, USDCHF). A long
USDJPY is a SHORT yen. So the pair's return is negated for those three before
anything is ranked; skipping that step ranks three currencies backwards and
produces a result that is half signal and half sign error.

    python tools/cross_search.py
    python tools/cross_search.py --timeframe H1 --years 3 --out reports/cross_h1.json
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
from rule_backtest import SYMBOLS, choose_spread, connect, fetch_rates
from rule_search import t_crit_95, z_for

TRADING_DAYS = 252.0

# Which side of the quote the FOREIGN currency sits on. A pair not listed here
# is refused rather than assumed: guessing this is a sign error that looks like
# a result.
FOREIGN_IS_BASE = {
    "EURUSD": True, "GBPUSD": True, "AUDUSD": True, "NZDUSD": True,
    "USDJPY": False, "USDCAD": False, "USDCHF": False,
}


def foreign_returns(closes: np.ndarray, foreign_is_base: bool) -> np.ndarray:
    """Log return of the FOREIGN currency against the dollar.

    Negated where the dollar is the base, because a long USDJPY is a short yen
    and ranking it as though it were a long yen inverts three of the seven.
    """
    r = np.diff(np.log(closes))
    return r if foreign_is_base else -r


def align(series_by_symbol: dict) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Intersect the timestamps so every column is the same bar.

    Symbols come back from the terminal with different bar counts - the
    existing searches record that as a data gap and index the split off the
    first symbol. A cross-section cannot do that: ranking column i of one
    symbol against column i of another when the columns are different bars is
    not a ranking at all.
    """
    common = None
    for _sym, (times, _c) in series_by_symbol.items():
        common = times if common is None else np.intersect1d(common, times)
    syms = sorted(series_by_symbol)
    closes = np.vstack([
        series_by_symbol[s][1][np.isin(series_by_symbol[s][0], common)]
        for s in syms
    ])
    return syms, common, closes


def portfolio(rets: np.ndarray, signal: np.ndarray, spreads: np.ndarray,
              n_legs: int, hold: int, sign: int) -> np.ndarray:
    """Daily portfolio returns, net of turnover cost.

    `rets` is (symbols, bars) of foreign-currency returns; `signal` the same
    shape, already lagged so that row t is knowable at t. Weights are set at
    each rebalance and held, so a leg surviving a rebalance is not charged.
    """
    n_sym, n_bar = rets.shape
    weights = np.zeros(n_sym)
    out = np.zeros(n_bar)
    for t in range(n_bar):
        if t % hold == 0:
            s = signal[:, t]
            if np.all(np.isfinite(s)):
                order = np.argsort(s)
                new = np.zeros(n_sym)
                new[order[-n_legs:]] = sign / n_legs      # strongest
                new[order[:n_legs]] = -sign / n_legs      # weakest
                out[t] -= float(np.sum(np.abs(new - weights) * spreads))
                weights = new
        out[t] += float(np.dot(weights, rets[:, t]))
    return out


def momentum_signal(rets: np.ndarray, k: int) -> np.ndarray:
    """Cumulative return over the k bars ENDING AT t-1, as a (symbols, bars) grid.

    A function rather than three lines inline, because it is the one place in
    this file a lookahead can hide and a test cannot check code it does not
    call. `test_cross_search.py` restated this construction instead of
    importing it, and an off-by-one introduced HERE was therefore invisible to
    it -- the test passed while the tool read the current bar's own return and
    scored an in-sample t of 2.47 against a 2.241 Bonferroni threshold,
    clearing the permutation null as well. Only the out-of-sample gate caught
    it. The test now imports this.

    Column t must be knowable at t: it sums rets[t-k .. t-1] and touches
    nothing at or after t.
    """
    cum = np.cumsum(rets, axis=1)
    sig = np.full_like(rets, np.nan)
    sig[:, k + 1:] = cum[:, k:-1] - cum[:, :-k - 1]
    return sig


def permuted_signal(signal: np.ndarray, hold: int, seed: int) -> np.ndarray:
    """Shuffle the ranking ACROSS SYMBOLS at each rebalance.

    The portfolio still holds n long and n short, still turns over at the same
    cadence and still pays the same order of spread. Only the identity of the
    strongest currency is destroyed, which is precisely the claim being tested.
    Shuffling through time instead would leave the cross-section intact and
    test nothing.
    """
    rng = np.random.default_rng(seed)
    out = signal.copy()
    for t in range(0, signal.shape[1], hold):
        out[:, t] = rng.permutation(signal[:, t])
    return out


def tstat(series: np.ndarray) -> tuple[float | None, float]:
    """t on the portfolio's own returns. One observation per bar, no pooling."""
    s = series[np.isfinite(series)]
    if len(s) < 30 or s.std(ddof=1) == 0:
        return None, 0.0
    t = float(s.mean() / (s.std(ddof=1) / math.sqrt(len(s))))
    return round(t, 2), float(s.mean())


def annualised(series: np.ndarray, bars_per_year: float) -> dict:
    s = series[np.isfinite(series)]
    if len(s) < 30:
        return {}
    vol = float(s.std(ddof=1)) * math.sqrt(bars_per_year)
    mean = float(s.mean()) * bars_per_year
    eq = np.cumsum(s)
    peak = np.maximum.accumulate(eq)
    return {
        "return_pct_annual": round(100.0 * mean, 2),
        "vol_pct_annual": round(100.0 * vol, 2),
        "sharpe": round(mean / vol, 3) if vol else None,
        "worst_drawdown_pct": round(100.0 * float(np.min(eq - peak)), 2),
    }


def build_candidates(lookbacks, legs, holds):
    for k in lookbacks:
        for n in legs:
            for h in holds:
                for sign, tag in ((1, "mom"), (-1, "rev")):
                    yield (f"{tag}_k{k}_n{n}_h{h}", k, n, h, sign)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--timeframe", default="D1")
    ap.add_argument("--years", type=float, default=10.0)
    ap.add_argument("--split", type=float, default=0.70)
    ap.add_argument("--null-rounds", type=int, default=10,
                    help="rounds of the cross-sectional shuffle. Three is not "
                         "enough to estimate best-of-N and it flipped a "
                         "verdict once in this project already")
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--lookbacks", default="5,20,60")
    ap.add_argument("--legs", default="2,3")
    ap.add_argument("--holds", default="5,20")
    ap.add_argument("--path")
    ap.add_argument("--out")
    args = ap.parse_args()

    bars_per_year = TRADING_DAYS if args.timeframe == "D1" else TRADING_DAYS * 24
    want = int(args.years * bars_per_year) + 100

    mt5 = connect(args.path)
    result = {"timeframe": args.timeframe, "years": args.years,
              "split": args.split, "null_rounds": args.null_rounds,
              "blocks": args.blocks, "construction": "dollar-neutral cross-section",
              "data_gaps": []}

    series, spread_pct = {}, {}
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        if sym not in FOREIGN_IS_BASE:
            result["data_gaps"].append(
                f"{sym}: which side the foreign currency sits on is not "
                "recorded, and guessing it inverts the ranking; excluded")
            continue
        info = mt5.symbol_info(sym)
        rates = fetch_rates(mt5, sym, want, timeframe=args.timeframe, min_bars=300)
        if rates is None or info is None or len(rates) < 300:
            result["data_gaps"].append(f"{sym}: insufficient history")
            continue
        tick = mt5.symbol_info_tick(sym)
        spread_price, _note = choose_spread(rates, info, tick, "median")
        closes = np.asarray(rates["close"], dtype=float)
        series[sym] = (np.asarray(rates["time"], dtype="int64"), closes)
        spread_pct[sym] = float(spread_price) / float(closes[-1])
    mt5.shutdown()

    if len(series) < 4:
        raise SystemExit("a cross-section needs at least four currencies: "
                         + "; ".join(result["data_gaps"]))

    syms, times, closes = align(series)
    rets = np.vstack([foreign_returns(closes[i], FOREIGN_IS_BASE[s])
                      for i, s in enumerate(syms)])
    spreads = np.array([spread_pct[s] for s in syms])
    times = times[1:]                            # returns lose the first bar
    n_bar = rets.shape[1]
    result["window"] = {
        "symbols": syms, "bars": int(n_bar),
        "from": str(np.datetime64(int(times[0]), "s")),
        "to": str(np.datetime64(int(times[-1]), "s")),
    }

    cut = int(n_bar * args.split)
    lookbacks = [int(x) for x in args.lookbacks.split(",")]
    legs = [int(x) for x in args.legs.split(",")]
    holds = [int(x) for x in args.holds.split(",")]

    signals = {k: momentum_signal(rets, k) for k in lookbacks}

    candidates, null_rounds = [], [[] for _ in range(args.null_rounds)]
    for name, k, n, h, sign in build_candidates(lookbacks, legs, holds):
        if 2 * n > len(syms):
            continue
        series_full = portfolio(rets, signals[k], spreads, n, h, sign)
        t_in, _ = tstat(series_full[:cut])
        t_out, _ = tstat(series_full[cut:])
        row = {
            "name": name, "lookback": k, "legs_per_side": n, "hold_bars": h,
            "direction": "momentum" if sign > 0 else "reversal",
            "in_sample": {"t_stat": t_in},
            "out_of_sample": {"t_stat": t_out, **annualised(series_full[cut:], bars_per_year)},
            "full": annualised(series_full, bars_per_year),
        }
        if args.blocks > 1:
            edges = np.linspace(0, n_bar, args.blocks + 1).astype(int)
            row["era_t"] = [tstat(series_full[a:b])[0]
                            for a, b in zip(edges[:-1], edges[1:])]
        candidates.append(row)

        for r in range(args.null_rounds):
            shuffled = portfolio(rets, permuted_signal(signals[k], h, seed=r * 997 + k),
                                 spreads, n, h, sign)
            null_rounds[r].append(tstat(shuffled[:cut])[0])

    valid = [c for c in candidates if c["in_sample"]["t_stat"] is not None]
    if not valid:
        raise SystemExit("no candidate produced a usable series")
    best = max(valid, key=lambda c: c["in_sample"]["t_stat"])
    null_best = [max([t for t in rnd if t is not None], default=0.0)
                 for rnd in null_rounds]
    z_crit = z_for(0.05 / len(valid))
    oos_survivors = [c["name"] for c in valid
                     if (c["out_of_sample"]["t_stat"] or 0) > 1.96]

    result["candidates"] = candidates
    result["verdict"] = {
        "candidates_judged": len(valid),
        "bonferroni_z_threshold": z_crit,
        "best_candidate": best["name"],
        "best_in_sample_t": best["in_sample"]["t_stat"],
        "best_out_of_sample_t": best["out_of_sample"]["t_stat"],
        "best_out_of_sample_sharpe": best["out_of_sample"].get("sharpe"),
        "permutation_null_best_t_per_round": null_best,
        "permutation_null_best_t_mean": round(float(np.mean(null_best)), 2),
        "beats_bonferroni": bool(best["in_sample"]["t_stat"] > z_crit),
        "beats_permutation_null": bool(best["in_sample"]["t_stat"] > max(null_best)),
        "holds_out_of_sample": bool((best["out_of_sample"]["t_stat"] or 0) > 1.96),
        "out_of_sample_survivors": oos_survivors,
        "expected_survivors_by_chance": round(0.05 * len(valid), 1),
        "median_out_of_sample_sharpe": round(float(np.median(
            [c["out_of_sample"].get("sharpe") or 0.0 for c in valid])), 3),
        "note": ("One observation per bar on a dollar-neutral portfolio, so "
                 "there is no date clustering to undo - the shared dollar leg "
                 "is removed by construction rather than corrected for. The "
                 "null permutes weights across symbols, holding leg count, "
                 "gross exposure and turnover fixed."),
    }

    print(f"\n  cross-sectional currency portfolios, {args.timeframe}, "
          f"{len(syms)} currencies, {n_bar} bars")
    print(f"  {result['window']['from']} to {result['window']['to']}")
    print("  " + "-" * 74)
    print(f"  {'candidate':18} {'in t':>7} {'out t':>7} {'out SR':>8} "
          f"{'ret%':>7} {'vol%':>7} {'maxDD%':>8}")
    for c in sorted(valid, key=lambda r: -(r["in_sample"]["t_stat"] or 0))[:12]:
        o = c["out_of_sample"]
        print(f"  {c['name']:18} {c['in_sample']['t_stat']:>7} "
              f"{str(o['t_stat']):>7} {str(o.get('sharpe')):>8} "
              f"{str(o.get('return_pct_annual')):>7} "
              f"{str(o.get('vol_pct_annual')):>7} "
              f"{str(o.get('worst_drawdown_pct')):>8}")
    v = result["verdict"]
    print(f"\n  best in sample      {v['best_candidate']} at t={v['best_in_sample_t']}")
    print(f"  Bonferroni needs    {v['bonferroni_z_threshold']}  -> "
          f"{'CLEARS' if v['beats_bonferroni'] else 'fails'}")
    print(f"  shuffle reached     {max(null_best)} (mean {v['permutation_null_best_t_mean']})"
          f"  -> {'CLEARS' if v['beats_permutation_null'] else 'fails'}")
    print(f"  out of sample       t={v['best_out_of_sample_t']}  -> "
          f"{'HOLDS' if v['holds_out_of_sample'] else 'fails'}")
    print(f"  survivors           {len(oos_survivors)} against "
          f"{v['expected_survivors_by_chance']} expected by chance")
    print(f"  median out-of-sample Sharpe across candidates: "
          f"{v['median_out_of_sample_sharpe']}\n")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
        print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
