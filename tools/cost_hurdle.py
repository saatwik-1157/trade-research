#!/usr/bin/env python3
"""What a rule would have to achieve just to break even against the spread.

This asks the question that comes before "does this rule work": how large does
an edge have to be before it survives the cost of trading at all. The answer is
a property of the broker and the bracket, not of any strategy, so it can be
computed exactly rather than searched for.

For a symmetric bracket the breakeven win rate is

    w = (SL + s) / (SL + TP)

with s the spread. At SL = TP = R that is 0.5 + s/(2R): every point of spread
has to be paid for with win rate above a coin flip, and the tighter the bracket
the more it costs. Trade frequency then multiplies whatever gap remains.

    python tools/cost_hurdle.py
    python tools/cost_hurdle.py --sl-atr 1.5 --tp-atr 1.5 --out reports/cost_hurdle.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rule_backtest import SYMBOLS, atr_series, connect, fetch_rates

FREQUENCIES = [50, 100, 250, 500, 1000, 2000]


def spread_stats(rates):
    """Spread distribution in points, from bars that actually recorded one.

    A large share of bars carry spread 0. That is not a free trade - it is an
    unrecorded value, and averaging it in would halve the apparent cost of
    trading. Only non-zero bars are used, and the share dropped is reported so
    the reader can judge how much of the history that leaves.
    """
    sp = rates["spread"].astype(float)
    recorded = sp[sp > 0]
    if len(recorded) == 0:
        return None
    return {
        "bars_total": int(len(sp)),
        "bars_recorded": int(len(recorded)),
        "recorded_share": round(float(len(recorded) / len(sp)), 3),
        "median_points": float(np.median(recorded)),
        "mean_points": round(float(recorded.mean()), 2),
        "p75_points": float(np.percentile(recorded, 75)),
        "p90_points": float(np.percentile(recorded, 90)),
        "max_points": float(recorded.max()),
    }


def main():
    ap = argparse.ArgumentParser(description="Breakeven hurdle imposed by the spread.")
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--tp-atr", type=float, default=1.5)
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--measured", default="reports/rule_backtest.json",
                    help="backtest output to compare measured win rates against")
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    result = {"sl_atr": args.sl_atr, "tp_atr": args.tp_atr, "lot": args.lot,
              "timeframe": "H1", "symbols": {}, "data_gaps": []}

    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            result["data_gaps"].append(f"{sym}: could not select")
            continue
        rates = fetch_rates(mt5, sym, args.bars)
        info = mt5.symbol_info(sym)
        if rates is None or info is None:
            result["data_gaps"].append(f"{sym}: insufficient history")
            continue

        sp = spread_stats(rates)
        if sp is None:
            result["data_gaps"].append(f"{sym}: no bar recorded a spread")
            continue

        atr = atr_series(rates["high"].astype(float), rates["low"].astype(float),
                         rates["close"].astype(float))
        atr_pts = float(np.nanmedian(atr)) / info.point

        sl_pts, tp_pts = args.sl_atr * atr_pts, args.tp_atr * atr_pts
        s_med, s_p90 = sp["median_points"], sp["p90_points"]

        # Value of one point for this lot size, in account currency.
        tick_size = info.trade_tick_size or info.point
        point_value = info.trade_tick_value * (info.point / tick_size) * args.lot

        def hurdle(s):
            return (sl_pts + s) / (sl_pts + tp_pts)

        result["symbols"][sym] = {
            "spread": sp,
            "median_atr_points": round(atr_pts, 1),
            "bracket": {"sl_points": round(sl_pts, 1), "tp_points": round(tp_pts, 1)},
            "breakeven_win_rate_median_spread": round(hurdle(s_med), 4),
            "breakeven_win_rate_p90_spread": round(hurdle(s_p90), 4),
            "cost_per_trade_account_ccy": round(s_med * point_value, 4),
            "point_value_per_lot_set": round(point_value, 5),
            "annual_cost_account_ccy": {
                str(n): round(n * s_med * point_value, 2) for n in FREQUENCIES
            },
        }

    mt5.shutdown()

    # Compare the hurdle against what the rules actually managed.
    measured_path = args.measured
    if not os.path.isabs(measured_path):
        measured_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), measured_path)
    if os.path.exists(measured_path):
        with open(measured_path, encoding="utf-8") as fh:
            m = json.load(fh)
        comparison = {}
        for rule, block in m.get("rules", {}).items():
            rows = {}
            for sym, stats_ in block.get("per_symbol", {}).items():
                if sym not in result["symbols"] or "win_rate" not in stats_:
                    continue
                need = result["symbols"][sym]["breakeven_win_rate_median_spread"]
                got = stats_["win_rate"]
                rows[sym] = {"measured_win_rate": got, "breakeven_needed": need,
                             "gap": round(got - need, 4)}
            if rows:
                gaps = [r["gap"] for r in rows.values()]
                comparison[rule] = {
                    "per_symbol": rows,
                    "symbols_clearing_hurdle": sum(1 for g in gaps if g > 0),
                    "symbols_tested": len(gaps),
                    "mean_gap": round(float(np.mean(gaps)), 4),
                }
        # How much evidence would settle it? An edge the size of the hurdle
        # still has to be demonstrated, and a win rate that close to a coin
        # flip needs a great many trades before the difference is separable
        # from noise. z=1.96 two-sided, z=0.84 for 80% power, p~0.5.
        for rule, block in comparison.items():
            deltas = [abs(r["gap"]) for r in block["per_symbol"].values() if r["gap"]]
            delta = float(np.mean(deltas)) if deltas else 0.0
            if delta > 0:
                n = (1.96 + 0.84) ** 2 * 0.25 / delta ** 2
                block["edge_size_win_rate_points"] = round(delta * 100, 2)
                block["trades_to_demonstrate_at_80pct_power"] = int(round(n, -2))
        result["measured_vs_hurdle"] = comparison
    else:
        result["data_gaps"].append(
            f"{args.measured} not found - no measured win rates to compare against")

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
