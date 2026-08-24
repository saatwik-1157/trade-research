#!/usr/bin/env python3
"""Sweep SL/TP bracket ratios and test whether any of them beats a coin flip.

A grid search over exit brackets will always produce a best cell. The whole
question is whether that cell is signal or the largest draw from a pile of
noise, so this tool does three things a bare sweep does not:

  1. Splits the history. Cells are ranked on the in-sample half only; the
     winner's out-of-sample result is what gets reported.
  2. Corrects for multiple testing. Searching a whole grid and quoting the
     best t-stat against 1.96 is how noise gets published.
  3. Runs the identical grid on the `random` rule. Its best-of-grid t-stat is
     an empirical null: whatever `random` achieves by luck alone is the bar a
     real rule has to clear, and it is not 1.96.

    python tools/bracket_sweep.py --rule rsi_reversion --out reports/bracket_sweep.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rule_backtest import (SIGNALS, SYMBOLS, atr_series, connect, fetch_rates,
                           simulate, stats)

SL_GRID = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
TP_GRID = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]


def pooled_stats(trades_by_symbol, lo=None, hi=None):
    """Pool per-symbol trades into point-normalised stats, optionally by slice."""
    pooled = []
    for point, trades in trades_by_symbol:
        for t in trades:
            if lo is not None and t["entry_idx"] < lo:
                continue
            if hi is not None and t["entry_idx"] >= hi:
                continue
            pooled.append({"net": t["net"] / point, "gross": t["gross"] / point,
                           "bars": t["bars"], "reason": t["reason"]})
    return stats(pooled, 1.0)


def main():
    ap = argparse.ArgumentParser(description="Sweep SL/TP brackets with an OOS holdout.")
    ap.add_argument("--rule", default="rsi_reversion",
                    choices=["rsi_reversion", "sma_cross"])
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--split", type=float, default=0.70,
                    help="fraction of history used in-sample")
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    result = {
        "timeframe": "H1", "rule": args.rule, "split": args.split,
        "sl_grid": SL_GRID, "tp_grid": TP_GRID,
        "cells_tested": len(SL_GRID) * len(TP_GRID),
        "data_gaps": [], "grid": {}, "verdict": {},
    }

    market = {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            result["data_gaps"].append(f"{sym}: could not select")
            continue
        rates = fetch_rates(mt5, sym, args.bars)
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        if rates is None or info is None:
            result["data_gaps"].append(f"{sym}: insufficient history")
            continue
        spread = (tick.ask - tick.bid) if tick and tick.ask > tick.bid else info.spread * info.point
        if spread <= 0:
            spread = info.spread * info.point
        if spread <= 0:
            result["data_gaps"].append(
                f"{sym}: live spread quoted as 0 - costs understated for this symbol")
        o, h, l, c = (rates["open"].astype(float), rates["high"].astype(float),
                      rates["low"].astype(float), rates["close"].astype(float))
        market[sym] = {"o": o, "h": h, "l": l, "c": c, "atr": atr_series(h, l, c),
                       "point": info.point, "spread": spread, "n": len(c),
                       "from": str(np.datetime64(int(rates["time"][0]), "s")),
                       "to": str(np.datetime64(int(rates["time"][-1]), "s"))}
    mt5.shutdown()

    if not market:
        raise SystemExit("no usable symbols: " + "; ".join(result["data_gaps"]))

    any_sym = next(iter(market.values()))
    split_idx = int(any_sym["n"] * args.split)
    result["window"] = {"from": any_sym["from"], "to": any_sym["to"],
                        "bars_per_symbol": any_sym["n"],
                        "in_sample_bars": split_idx,
                        "out_of_sample_bars": any_sym["n"] - split_idx}

    # `random` shares the grid so its best cell measures luck, not skill.
    for rule in (args.rule, "random"):
        sigs = {s: SIGNALS[rule](m["o"], m["h"], m["l"], m["c"]) for s, m in market.items()}
        cells = []
        for sl in SL_GRID:
            for tp in TP_GRID:
                per_symbol = []
                for s, m in market.items():
                    tr = simulate(m["o"], m["h"], m["l"], m["c"], sigs[s], m["atr"],
                                  m["spread"], sl, tp)
                    per_symbol.append((m["point"], tr))
                ins = pooled_stats(per_symbol, hi=split_idx)
                oos = pooled_stats(per_symbol, lo=split_idx)
                cells.append({"sl_atr": sl, "tp_atr": tp,
                              "in_sample": ins, "out_of_sample": oos})
        result["grid"][rule] = cells

    n_cells = result["cells_tested"]
    # Bonferroni on a two-sided 5% test across the grid.
    bonferroni_p = 0.05 / n_cells
    # inverse normal via Acklam-free route: bisect the survival function
    lo_z, hi_z = 0.0, 8.0
    for _ in range(200):
        mid = (lo_z + hi_z) / 2
        sf2 = math.erfc(mid / math.sqrt(2))          # two-sided tail
        if sf2 > bonferroni_p:
            lo_z = mid
        else:
            hi_z = mid
    z_crit = round((lo_z + hi_z) / 2, 3)

    def best_by_is(cells):
        ranked = sorted(cells, key=lambda x: x["in_sample"].get("t_stat", 0), reverse=True)
        return ranked[0]

    summary = {}
    for rule, cells in result["grid"].items():
        valid = [c for c in cells if c["in_sample"].get("trades", 0) > 30]
        if not valid:
            summary[rule] = {"note": "no cell produced enough trades"}
            continue
        best = best_by_is(valid)
        # Signed, not absolute. A large negative t is a configuration that
        # loses reliably; scoring it as a hit would report cost drag as edge.
        max_t = max(c["in_sample"].get("t_stat", 0) for c in valid)
        worst_t = min(c["in_sample"].get("t_stat", 0) for c in valid)
        summary[rule] = {
            "worst_in_sample_t": round(worst_t, 2),
            "best_cell_by_in_sample": {"sl_atr": best["sl_atr"], "tp_atr": best["tp_atr"]},
            "in_sample_t": best["in_sample"]["t_stat"],
            "in_sample_expectancy": best["in_sample"]["expectancy_points_net"],
            "in_sample_trades": best["in_sample"]["trades"],
            "out_of_sample_t": best["out_of_sample"].get("t_stat"),
            "out_of_sample_expectancy": best["out_of_sample"].get("expectancy_points_net"),
            "out_of_sample_trades": best["out_of_sample"].get("trades"),
            "best_in_sample_t_across_grid": round(max_t, 2),
        }

    rule_max = summary.get(args.rule, {}).get("best_in_sample_t_across_grid", 0) or 0
    rand_max = summary.get("random", {}).get("best_in_sample_t_across_grid", 0) or 0
    oos_t = summary.get(args.rule, {}).get("out_of_sample_t") or 0

    result["verdict"] = {
        "cells_tested": n_cells,
        "bonferroni_alpha": round(bonferroni_p, 5),
        "bonferroni_z_threshold": z_crit,
        "naive_threshold": 1.96,
        "rule_best_in_sample_t": rule_max,
        "random_best_in_sample_t": rand_max,
        "rule_beats_bonferroni": bool(rule_max > z_crit),
        "rule_beats_random_search": bool(rule_max > rand_max),
        "winner_holds_out_of_sample": bool(oos_t > 1.96),
        "summary": summary,
        "reading": (
            "A cell only counts as evidence if it clears the Bonferroni "
            "threshold, exceeds what the random rule achieved searching the "
            "same grid, AND survives out of sample. Failing any one of the "
            "three means the cell is a draw from noise."
        ),
    }

    text = json.dumps(result, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        print(json.dumps(result["verdict"], indent=2))


if __name__ == "__main__":
    main()
