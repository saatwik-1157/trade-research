#!/usr/bin/env python3
"""Where the spread hurdle is smallest: by symbol, by hour, by timeframe.

`cost_hurdle.py` says how large an edge must be to clear the spread. This asks
the complementary question - what can be done to lower the bar instead of
searching harder for an edge to clear it.

Two levers, both measurable and neither requiring a signal:

  * Hour of day. Spread is not constant; it widens across the illiquid hours
    and around the rollover. Trading only the cheap hours cuts cost with no
    claim about direction attached.
  * Timeframe. The hurdle is spread relative to the bracket, and the bracket
    scales with ATR. ATR grows with bar length far faster than spread does, so
    the same rule on a longer bar pays proportionally less to trade.

Neither lever is an edge. Both shrink the denominator of the problem, which is
the one move in this repository with a proven sign.

    python tools/cost_profile.py --out reports/cost_profile.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rule_backtest import SYMBOLS, atr_series, connect

TIMEFRAMES = ("H1", "H4", "D1")


def tf_const(mt5, name):
    return {"H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}[name]


def fetch(mt5, symbol, tf_name, want):
    tf = tf_const(mt5, tf_name)
    for count in (want, 50000, 20000, 10000, 5000, 2000, 1000):
        if count > want:
            continue
        r = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if r is not None and len(r) >= 300:
            return r
    return None


def recorded_spread(rates, since_epoch=None):
    """Bars that recorded a spread, optionally restricted to a date window.

    Two traps here, both of which quietly corrupt a timeframe comparison:

    A 0 is missing data, not a free trade, so those bars are dropped.

    More seriously, the D1 series on this feed reaches back to 1971 - decades
    before EURUSD existed - and those backfilled bars carry a placeholder
    spread (median 50 points against 3 for a real quote). Left in, they make
    the daily timeframe look several times more expensive than the hourly one,
    which is the opposite of the truth. Every timeframe is therefore measured
    over the same recent window, so the comparison is like for like rather
    than a comparison of how much synthetic history each one carries.
    """
    sp = rates["spread"].astype(float)
    if since_epoch is not None:
        sp = sp[rates["time"].astype("int64") >= since_epoch]
    return sp[sp > 0]


def main():
    ap = argparse.ArgumentParser(description="Spread and ATR profile by hour and timeframe.")
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = {"sl_atr": args.sl_atr, "data_gaps": [], "by_symbol": {},
              "by_hour": {}, "by_timeframe": {}}

    hour_pool: dict[int, list[float]] = {h: [] for h in range(24)}

    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            result["data_gaps"].append(f"{sym}: could not select")
            continue
        info = mt5.symbol_info(sym)
        block = {"per_timeframe": {}}

        # The H1 series defines the window; every timeframe is cut to it so
        # the comparison is not distorted by backfilled history.
        h1 = fetch(mt5, sym, "H1", args.bars)
        if h1 is None:
            result["data_gaps"].append(f"{sym}: no H1 history to set the window")
            continue
        since = int(h1["time"].astype("int64")[0])
        block["window_from"] = str(np.datetime64(since, "s"))

        for tf in TIMEFRAMES:
            rates = fetch(mt5, sym, tf, args.bars)
            if rates is None or info is None:
                result["data_gaps"].append(f"{sym} {tf}: insufficient history")
                continue
            sp = recorded_spread(rates, since)
            if len(sp) == 0:
                result["data_gaps"].append(f"{sym} {tf}: no bar recorded a spread")
                continue
            keep = rates["time"].astype("int64") >= since
            rates = rates[keep]
            if len(rates) < 100:
                result["data_gaps"].append(
                    f"{sym} {tf}: only {len(rates)} bars inside the common window")
                continue
            atr = atr_series(rates["high"].astype(float), rates["low"].astype(float),
                             rates["close"].astype(float))
            atr_pts = float(np.nanmedian(atr)) / info.point
            s_med = float(np.median(sp))
            bracket = args.sl_atr * atr_pts
            # Excess win rate the spread demands on a symmetric bracket.
            excess = s_med / (2 * bracket) if bracket > 0 else None
            block["per_timeframe"][tf] = {
                "bars": int(len(rates)),
                "median_spread_points": s_med,
                "median_atr_points": round(atr_pts, 1),
                "bracket_points": round(bracket, 1),
                "spread_over_bracket": round(s_med / bracket, 5) if bracket else None,
                "breakeven_win_rate": round(0.5 + excess, 4) if excess is not None else None,
            }

            if tf == "H1":
                # Hour attribution only makes sense on the intraday series.
                hours = ((rates["time"].astype("int64") // 3600) % 24).astype(int)
                sp_all = rates["spread"].astype(float)
                by_hour = {}
                for h in range(24):
                    vals = sp_all[(hours == h) & (sp_all > 0)]
                    if len(vals):
                        by_hour[str(h)] = {"median_points": float(np.median(vals)),
                                           "bars": int(len(vals))}
                        hour_pool[h].append(float(np.median(vals)) / s_med)
                block["by_hour_h1"] = by_hour

        result["by_symbol"][sym] = block

    mt5.shutdown()

    # Hour profile normalised per symbol, so a wide-spread pair does not
    # dominate the shape.
    rel = {}
    for h, vals in hour_pool.items():
        if vals:
            rel[str(h)] = round(float(np.median(vals)), 3)
    result["by_hour"] = {
        "relative_to_symbol_median": rel,
        "note": ("1.0 is that symbol's own median spread. Hours below 1.0 are "
                 "cheaper than typical; the ranking matters more than the level."),
    }
    if rel:
        ordered = sorted(rel.items(), key=lambda kv: kv[1])
        result["by_hour"]["cheapest_hours"] = [int(h) for h, _ in ordered[:6]]
        result["by_hour"]["dearest_hours"] = [int(h) for h, _ in ordered[-6:]]

    # Timeframe summary pooled across symbols.
    for tf in TIMEFRAMES:
        ratios, hurdles = [], []
        for sym, b in result["by_symbol"].items():
            cell = b["per_timeframe"].get(tf)
            if cell and cell["spread_over_bracket"] is not None:
                ratios.append(cell["spread_over_bracket"])
                hurdles.append(cell["breakeven_win_rate"])
        if ratios:
            result["by_timeframe"][tf] = {
                "median_spread_over_bracket": round(float(np.median(ratios)), 5),
                "median_breakeven_win_rate": round(float(np.median(hurdles)), 4),
                "symbols": len(ratios),
            }
    base = result["by_timeframe"].get("H1", {}).get("median_spread_over_bracket")
    if base:
        for tf, cell in result["by_timeframe"].items():
            cell["cost_vs_h1"] = round(cell["median_spread_over_bracket"] / base, 3)

    text = json.dumps(result, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
