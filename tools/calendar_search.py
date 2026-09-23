#!/usr/bin/env python3
"""Calendar effects on the FX majors: day of week, month, and turn of month.

Kaufman devotes a chapter to seasonality and calendar patterns, and it is the
most prominent idea in the reference works on this shelf that this repository
has never tested ON THIS UNIVERSE. `patterns.py` already does day-of-week and
month-of-year with FDR control, but it pulls equity tickers through yfinance;
it has never seen a currency pair. The seasonality literature is largely about
agriculturals, where a physical supply cycle gives the effect a mechanism. FX
has no harvest, so the candidate mechanisms are different and worth naming
before testing: month-end corporate and index-rebalance flows, weekend risk
being carried on a Friday and released on a Monday, and the fact that a
currency is a rate between two calendars rather than a thing that is grown.

**A calendar rule is the cleanest test this project can run, and that is the
reason to run it.** Every family searched so far has free parameters -- a
lookback, a threshold, a bracket -- so a null result always carries "maybe
another setting works". Day-of-week has five buckets and no parameters at all.
There is nothing to tune, so the search space is exactly as large as it looks
and the Bonferroni correction is honest rather than a floor on a much larger
hidden search.

The null shuffles the CALENDAR LABEL across observations, holding the returns
and the bucket sizes fixed. That destroys the link between the date and the
move while preserving everything else, which is the same construction
`trade_autopsy.py` uses on symbol labels and `cross_search.py` uses on weights.

    python tools/calendar_search.py
    python tools/calendar_search.py --timeframe H1 --years 3
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import SYMBOLS, choose_spread, connect, fetch_rates
from rule_search import z_for

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def t_of(x: np.ndarray) -> float | None:
    x = x[np.isfinite(x)]
    if len(x) < 30 or x.std(ddof=1) == 0:
        return None
    return round(float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))), 2)


def buckets_for(times: np.ndarray, kind: str) -> tuple[np.ndarray, list[str]]:
    """Calendar label per bar. The stamps are server time rendered as UTC.

    A bar's label must come from the bar's OWN date, so the return being
    tested is the one that follows the label rather than the one that made
    it. `returns_after` handles that alignment; this only assigns the label.
    """
    dts = [datetime.fromtimestamp(int(t), tz=timezone.utc) for t in times]
    if kind == "dow":
        return np.array([d.weekday() for d in dts]), DOW
    if kind == "month":
        return np.array([d.month - 1 for d in dts]), MONTHS
    if kind == "turn_of_month":
        # Last 2 and first 3 trading days of a month against the rest: the
        # window the flow argument actually names, rather than a tuned one.
        lab = []
        for d in dts:
            lab.append(0 if (d.day >= 27 or d.day <= 3) else 1)
        return np.array(lab), ["turn", "rest"]
    raise SystemExit(f"unknown bucket kind {kind}")


def returns_after(closes: np.ndarray) -> np.ndarray:
    """Bar-to-bar return as a fraction of price.

    Percent rather than points, because seven pairs with different point
    sizes cannot be pooled in points -- that is the metals error this
    repository documents, and it is one line away here.
    """
    return np.diff(closes) / closes[:-1]


def days_spanned(times: np.ndarray) -> np.ndarray:
    """Calendar days each return covers. Not all buckets are one day.

    THE WEEKEND IS THE WHOLE PROBLEM WITH A DAY-OF-WEEK STUDY. The return
    from Friday's close to Monday's close spans THREE calendar days while
    every other return spans one, so the Friday-to-Monday bucket gets three
    times the exposure and a larger mean by arithmetic rather than by
    anomaly. Measured here before correcting it: that bucket showed +0.0306%
    against Tuesday's +0.0123%, which looks like a strong effect and is
    +0.0102% a day against +0.0123% a day -- lower, not higher.

    The first version of this file also LABELLED that return with Friday,
    the day it starts from. The convention, and the only labelling that makes
    the buckets comparable, is to name a return for the bar it ENDS on: the
    Friday-to-Monday move is the Monday observation. So this file now labels
    by the closing bar and reports the per-day figure beside the raw one.
    """
    return np.maximum(np.diff(times) / 86400.0, 1e-9)


def clustered_t(vals: np.ndarray, dates: np.ndarray) -> tuple[int, float | None]:
    """t across DATES, not across pooled observations.

    The seven majors share a dollar leg, so one Friday move opens a return in
    all seven and a pooled t-statistic counts it seven times. Measured here:
    the pooled Friday t is 4.00 on 3,668 observations, which are really ~524
    dates seen seven times each -- and the inflation is about sqrt(7) = 2.6x.
    This is the single most documented trap in this repository
    (`rule_search.clustered_by_date` exists for it, and it dissolved the two
    best results the project ever had). A calendar search is MORE exposed to
    it than a rule search, not less, because every symbol shares the label by
    construction: it is the same Friday for all of them.

    So the pooled figure is reported as a diagnostic and this is the one that
    decides anything.
    """
    means = []
    for d in np.unique(dates):
        v = vals[dates == d]
        if len(v):
            means.append(float(v.mean()))
    m = np.asarray(means)
    if len(m) < 30 or m.std(ddof=1) == 0:
        return len(m), None
    return len(m), round(float(m.mean() / (m.std(ddof=1) / math.sqrt(len(m)))), 2)


def permutation_p(vals: np.ndarray, labels: np.ndarray, rounds: int,
                  seed: int = 0) -> dict:
    """Best-minus-worst bucket mean, against shuffled calendar labels."""
    uniq = np.unique(labels)

    def spread(lab):
        m = [vals[lab == u].mean() for u in uniq if (lab == u).sum() > 2]
        return float(max(m) - min(m)) if len(m) > 1 else 0.0

    real = spread(labels)
    rng = np.random.default_rng(seed)
    sh = labels.copy()
    null = np.empty(rounds)
    for i in range(rounds):
        rng.shuffle(sh)
        null[i] = spread(sh)
    beat = int((null >= real).sum())
    return {
        "real_spread_pct": round(100 * real, 5),
        "null_mean_spread_pct": round(100 * float(null.mean()), 5),
        "null_p95_spread_pct": round(100 * float(np.percentile(null, 95)), 5),
        "rounds": rounds,
        "p_value": round((beat + 1) / (rounds + 1), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--timeframe", default="D1")
    ap.add_argument("--years", type=float, default=10.0)
    ap.add_argument("--rounds", type=int, default=5000)
    ap.add_argument("--path")
    ap.add_argument("--out")
    args = ap.parse_args()

    bars_per_year = 252 if args.timeframe == "D1" else 252 * 24
    want = int(args.years * bars_per_year) + 100

    mt5 = connect(args.path)
    rep: dict = {"timeframe": args.timeframe, "years": args.years,
                 "rounds": args.rounds, "unit": "percent of price",
                 "by_symbol": {}, "pooled": {}, "data_gaps": []}
    spreads_pct: dict[str, float] = {}
    series: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        info = mt5.symbol_info(sym)
        rates = fetch_rates(mt5, sym, want, timeframe=args.timeframe, min_bars=300)
        if rates is None or info is None or len(rates) < 300:
            rep["data_gaps"].append(f"{sym}: insufficient history")
            continue
        tick = mt5.symbol_info_tick(sym)
        sp, _note = choose_spread(rates, info, tick, "median")
        closes = np.asarray(rates["close"], dtype=float)
        series[sym] = (np.asarray(rates["time"], dtype="int64"), closes)
        spreads_pct[sym] = float(sp) / float(closes[-1])
    mt5.shutdown()

    if not series:
        raise SystemExit("no usable symbols: " + "; ".join(rep["data_gaps"]))

    for kind in ("dow", "month", "turn_of_month"):
        pooled_vals, pooled_lab, pooled_date, pooled_span = [], [], [], []
        per_sym = {}
        for sym, (times, closes) in series.items():
            lab_all, names = buckets_for(times, kind)
            r = returns_after(closes)
            # Label by the CLOSING bar, not the opening one: the
            # Friday-to-Monday move is the Monday observation.
            lab = lab_all[1:]
            span = days_spanned(times)
            rows = {}
            for u in np.unique(lab):
                v = r[lab == u]
                if len(v) < 30:
                    continue
                rows[names[u]] = {"n": int(len(v)),
                                  "mean_pct": round(100 * float(v.mean()), 5),
                                  "t": t_of(v)}
            per_sym[sym] = rows
            pooled_vals.append(r)
            pooled_lab.append(lab)
            # The calendar DATE of each observation, so the pooled figure can
            # be re-read across dates rather than across symbol-days.
            pooled_date.append((times[1:] // 86400).astype("int64"))
            pooled_span.append(span)

        vals = np.concatenate(pooled_vals)
        labs = np.concatenate(pooled_lab)
        dates = np.concatenate(pooled_date)
        spans = np.concatenate(pooled_span)
        _all_names = buckets_for(next(iter(series.values()))[0], kind)[1]
        pooled_rows = {}
        for u in np.unique(labs):
            v = vals[labs == u]
            if len(v) < 30:
                continue
            # Cost is what any of this has to beat. The mean spread across
            # the pairs, in percent, is charged once per round trip.
            cost = float(np.mean(list(spreads_pct.values())))
            n_dates, t_cl = clustered_t(v, dates[labs == u])
            sp_u = spans[labs == u]
            pooled_rows[_all_names[u]] = {
                "n": int(len(v)),
                "n_dates": n_dates,
                "days_spanned": round(float(sp_u.mean()), 2),
                "mean_pct": round(100 * float(v.mean()), 5),
                "mean_pct_per_day": round(100 * float((v / sp_u).mean()), 5),
                "t_pooled": t_of(v),
                "t_by_date": t_cl,
                "net_of_spread_pct": round(100 * (abs(float(v.mean())) - cost), 5),
            }
        # Era blocks and an out-of-sample split. EVERY candidate this
        # repository has ever liked died here rather than at the null:
        # donchian_fade_55 cleared its pooled t and was positive in 3 of 5
        # eras, and the H4 exit grid looked like an effect until every exit
        # turned out positive in the same one era. A calendar effect that is
        # real should be visible in most blocks, not carried by one.
        eras = {}
        order = np.argsort(dates)
        edges = np.linspace(0, len(order), 5).astype(int)
        for bi, (a, b) in enumerate(zip(edges[:-1], edges[1:], strict=True)):
            idx = order[a:b]
            ev, el, ed = vals[idx], labs[idx], dates[idx]
            row = {}
            for u in np.unique(el):
                v = ev[el == u]
                if len(v) < 30:
                    continue
                _n, t_cl = clustered_t(v, ed[el == u])
                row[_all_names[u]] = {"mean_pct": round(100 * float(v.mean()), 5),
                                      "t_by_date": t_cl}
            eras[f"era{bi + 1}"] = row

        cut = len(order) // 2
        halves = {}
        for name, idx in (("first_half", order[:cut]), ("second_half", order[cut:])):
            hv, hl, hd = vals[idx], labs[idx], dates[idx]
            row = {}
            for u in np.unique(hl):
                v = hv[hl == u]
                if len(v) < 30:
                    continue
                _n, t_cl = clustered_t(v, hd[hl == u])
                row[_all_names[u]] = {"mean_pct": round(100 * float(v.mean()), 5),
                                      "t_by_date": t_cl}
            halves[name] = row

        rep["pooled"][kind] = {
            "buckets": pooled_rows,
            "permutation": permutation_p(vals, labs, args.rounds),
            "bonferroni_z": z_for(0.05 / max(1, len(pooled_rows))),
            "eras": eras,
            "halves": halves,
        }
        rep["by_symbol"][kind] = per_sym

    cost = float(np.mean(list(spreads_pct.values())))
    rep["mean_spread_pct_round_trip"] = round(100 * cost, 5)

    print(f"\n  calendar effects, {args.timeframe}, {len(series)} FX majors, "
          f"{args.years:.0f} years")
    print(f"  a round trip costs {100*cost:.4f}% of price; any bucket mean "
          "smaller than that is unreachable")
    for kind in ("dow", "month", "turn_of_month"):
        blk = rep["pooled"][kind]
        print(f"\n  {kind.upper()}")
        print("  " + "-" * 66)
        print(f"  {'bucket':9}{'n':>7}{'days':>6}{'mean %':>10}"
              f"{'per day':>10}{'t pooled':>10}{'t by date':>11}")
        for name, row in blk["buckets"].items():
            print(f"  {name:9}{row['n']:>7}{row['days_spanned']:>6.1f}"
                  f"{row['mean_pct']:>10.4f}{row['mean_pct_per_day']:>10.4f}"
                  f"{str(row['t_pooled']):>10}{str(row['t_by_date']):>11}")
        if kind == "dow":
            print("    era stability (mean %, by quarter of the window):")
            names = list(blk["buckets"])
            print("      " + "".join(f"{n:>10}" for n in names))
            for ename, row in blk["eras"].items():
                print(f"      " + "".join(
                    f"{row[n]['mean_pct']:>10.4f}" if n in row else f"{'-':>10}"
                    for n in names) + f"   {ename}")
            print("    out of sample (first half / second half):")
            for hname, row in blk["halves"].items():
                print(f"      " + "".join(
                    f"{row[n]['mean_pct']:>10.4f}" if n in row else f"{'-':>10}"
                    for n in names) + f"   {hname}")
        pm = blk["permutation"]
        print(f"    spread {pm['real_spread_pct']:.5f}%  vs shuffled mean "
              f"{pm['null_mean_spread_pct']:.5f}%  p = {pm['p_value']}"
              + ("   -> inside the null" if pm["p_value"] > 0.05
                 else "   -> larger than the null"))

    if rep["data_gaps"]:
        print()
        for g in rep["data_gaps"]:
            print(f"  gap: {g}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
        print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
