#!/usr/bin/env python3
"""Test exit structures, including ones that do not cap the winner.

`rule_search.py` fixed the exit at SL=TP=1.5xATR. That bracket caps a winner at
1.5xATR while a loser runs to the same distance, which is structurally hostile
to trend-following: the whole premise of a trend rule is that the occasional
large winner pays for many small losers, and a fixed target forbids exactly
that. Reporting "breakouts lose" from that setup measures the exit, not the
rule.

Four exit families are tested here, three of which let a winner run:

  bracket   fixed SL and TP in ATR multiples (the existing baseline)
  trail     ATR trailing stop, no target at all
  time      fixed stop, exit at a bar count regardless of profit
  breakeven stop moves to entry once price has travelled 1 ATR, then holds

Every entry rule is paired with a permutation null under the SAME exit, so the
comparison is timing skill and not the exit's own behaviour: a trailing stop
applied to random signals also produces a long right tail, and comparing a real
trend rule against a fixed-bracket null would credit the exit to the rule.

Correction is over entry x exit, because that is the number of things tried.

    python tools/exit_search.py --out reports/exit_search.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rule_backtest import (SYMBOLS, atr_series, choose_spread, connect,
                           fetch_rates, stats)
from rule_search import (block_edges, block_summary, block_views,
                         build_candidates, clustered_by_date, permute, z_for)


def simulate_exit(o, h, l, c, sig, atr, spread, exit_kind, params, max_hold=480):
    """Walk bars, enter on the next open, close by the given exit rule.

    A bar that covers both the stop and the target books the loss - intrabar
    order is unknown, and resolving it favourably is how a backtest flatters
    itself. The trailing stop only ever ratchets in the favourable direction,
    and is checked against the same bar's extreme that could have stopped it,
    so a single bar cannot both extend the trail and avoid the stop.
    """
    trades = []
    i, n = 60, len(c)
    sl_atr = params.get("sl_atr", 1.5)
    tp_atr = params.get("tp_atr")
    trail_atr = params.get("trail_atr")
    hold_bars = params.get("hold_bars")
    be_trigger = params.get("be_trigger")

    while i < n - 1:
        s = sig[i - 1]
        if s == 0 or not np.isfinite(atr[i - 1]) or atr[i - 1] <= 0:
            i += 1
            continue
        entry, a = o[i], atr[i - 1]
        long = s > 0
        stop = entry - sl_atr * a if long else entry + sl_atr * a
        target = None
        if tp_atr:
            target = entry + tp_atr * a if long else entry - tp_atr * a
        moved_to_be = False

        exit_px = exit_reason = None
        j = i
        for j in range(i, min(i + max_hold, n)):
            hi, lo = h[j], l[j]
            # Stop is always checked first: it is the adverse outcome and the
            # bar's path is unknown.
            if (long and lo <= stop) or (not long and hi >= stop):
                exit_px, exit_reason = stop, ("be" if moved_to_be else "sl")
                break
            if target is not None and ((long and hi >= target) or (not long and lo <= target)):
                exit_px, exit_reason = target, "tp"
                break
            if be_trigger and not moved_to_be:
                reached = (hi - entry if long else entry - lo)
                if reached >= be_trigger * a:
                    stop, moved_to_be = entry, True
            if trail_atr:
                cand = (hi - trail_atr * a) if long else (lo + trail_atr * a)
                stop = max(stop, cand) if long else min(stop, cand)
            if hold_bars and (j - i + 1) >= hold_bars:
                exit_px, exit_reason = c[j], "time"
                break
        if exit_px is None:
            exit_px, exit_reason = c[j], "timeout"

        gross = (exit_px - entry) if long else (entry - exit_px)
        trades.append({"gross": float(gross), "net": float(gross - spread),
                       "bars": j - i + 1, "reason": exit_reason, "entry_idx": i})
        i = j + 1
    return trades


EXITS = [
    ("bracket_1.5_1.5", "bracket", {"sl_atr": 1.5, "tp_atr": 1.5}),
    ("bracket_1.0_3.0", "bracket", {"sl_atr": 1.0, "tp_atr": 3.0}),
    ("bracket_2.0_4.0", "bracket", {"sl_atr": 2.0, "tp_atr": 4.0}),
    ("trail_2.0", "trail", {"sl_atr": 2.0, "trail_atr": 2.0}),
    ("trail_3.0", "trail", {"sl_atr": 3.0, "trail_atr": 3.0}),
    ("time_24", "time", {"sl_atr": 3.0, "hold_bars": 24}),
    ("time_120", "time", {"sl_atr": 3.0, "hold_bars": 120}),
    ("breakeven_trail", "breakeven", {"sl_atr": 2.0, "trail_atr": 3.0, "be_trigger": 1.0}),
]


def collect(market, sigs, kind, params):
    """Every trade, tagged with symbol and entry time.

    exit_search used to pool straight into two buckets, which is why it could
    not answer the question that killed `time_120`: an exit holding positions a
    long time has enough variance that one split says little. Tagging lets the
    same trades be read by split AND by era without simulating twice.
    """
    rows = []
    for sym, m in market.items():
        for t in simulate_exit(m["o"], m["h"], m["l"], m["c"], sigs[sym], m["atr"],
                               m["spread"], kind, params):
            rows.append({"symbol": sym, "entry_idx": t["entry_idx"],
                         "entry_time": int(m["time"][t["entry_idx"]]),
                         "net": t["net"] / m["point"], "gross": t["gross"] / m["point"],
                         "bars": t["bars"], "reason": t["reason"]})
    return rows


def run(market, sigs, split_idx, kind, params):
    rows = collect(market, sigs, kind, params)
    ins = [r for r in rows if r["entry_idx"] < split_idx]
    oos = [r for r in rows if r["entry_idx"] >= split_idx]
    return stats(ins, 1.0), stats(oos, 1.0)


def main():
    ap = argparse.ArgumentParser(description="Search exit structures with a matched null.")
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--timeframe", default="H1", choices=["H1", "H4", "D1"],
                    help="the untested cell is a trend exit at a slow timeframe: "
                         "the fixed-bracket search covered H4 and D1, and the "
                         "exit search covered only H1, so no run has yet let a "
                         "winner run where cost_profile.py puts the spread "
                         "hurdle lowest (D1 at 0.27x of H1)")
    ap.add_argument("--split", type=float, default=0.70)
    ap.add_argument("--families", default="breakout,ma_cross,momentum,rsi_reversion",
                    help="candidate families to test (trend families are the point)")
    ap.add_argument("--spread-source", default="median", choices=["median", "p90", "live"])
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--null-rounds", type=int, default=2)
    ap.add_argument("--blocks", type=int, default=4,
                    help="cut history into this many equal-duration eras. One "
                         "split cannot settle a long-holding exit: `time_120` "
                         "showed +26 median out of sample and fell apart into "
                         "four blocks. 0 or 1 disables")
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    families = {f.strip() for f in args.families.split(",") if f.strip()}
    result = {"timeframe": args.timeframe, "spread_source": args.spread_source,
              "split": args.split, "exits": [e[0] for e in EXITS], "data_gaps": []}

    market = {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            result["data_gaps"].append(f"{sym}: could not select")
            continue
        rates = fetch_rates(mt5, sym, args.bars, args.timeframe, min_bars=300)
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        if rates is None or info is None:
            result["data_gaps"].append(f"{sym}: insufficient history")
            continue
        spread, note = choose_spread(rates, info, tick, args.spread_source)
        if spread <= 0:
            result["data_gaps"].append(f"{sym}: {note}")
        o, h, l, c = (rates["open"].astype(float), rates["high"].astype(float),
                      rates["low"].astype(float), rates["close"].astype(float))
        market[sym] = {"o": o, "h": h, "l": l, "c": c, "atr": atr_series(h, l, c),
                       "time": rates["time"].astype("int64"),
                       "point": info.point, "spread": spread, "n": len(c)}
    mt5.shutdown()
    if not market:
        raise SystemExit("no usable symbols: " + "; ".join(result["data_gaps"]))

    split_idx = int(next(iter(market.values()))["n"] * args.split)
    candidates = [(n, f, fn) for n, f, fn in build_candidates() if f in families]
    result["entries_tested"] = len(candidates)
    result["combinations_tested"] = len(candidates) * len(EXITS)

    edges = block_edges(market, args.blocks) if args.blocks > 1 else None
    if edges:
        result["eras"] = {
            "count": args.blocks,
            "from": str(np.datetime64(int(edges[0]), "s")),
            "to": str(np.datetime64(int(edges[-1]), "s")),
            "note": ("An exit that holds a long time has enough variance that a "
                     "single split says little - `time_120` showed +26 median "
                     "out of sample and did not survive being cut into four."),
        }

    rows, null_rounds = [], [[] for _ in range(args.null_rounds)]
    for idx, (name, family, fn) in enumerate(candidates):
        sigs = {s: fn(m["o"], m["h"], m["l"], m["c"]) for s, m in market.items()}
        perms = [{s: permute(sig, seed=idx * 977 + r * 13 + 1) for s, sig in sigs.items()}
                 for r in range(args.null_rounds)]
        for ename, kind, params in EXITS:
            tr = collect(market, sigs, kind, params)
            ins_rows = [r for r in tr if r["entry_idx"] < split_idx]
            oos_rows = [r for r in tr if r["entry_idx"] >= split_idx]
            ins, oos = stats(ins_rows, 1.0), stats(oos_rows, 1.0)
            oos.update(clustered_by_date(oos_rows))
            row = {"entry": name, "family": family, "exit": ename,
                   "in_sample": ins, "out_of_sample": oos}
            if edges:
                row["blocks"] = block_views(tr, edges)
                row["block_summary"] = block_summary(row["blocks"])
            rows.append(row)
            # Null shares the exit, so the exit's own shape is not credited
            # to the entry rule.
            for r in range(args.null_rounds):
                nins, _ = run(market, perms[r], split_idx, kind, params)
                null_rounds[r].append(nins.get("t_stat", 0.0)
                                      if nins.get("trades", 0) >= args.min_trades else None)

    valid = [r for r in rows if r["in_sample"].get("trades", 0) >= args.min_trades]
    result["combinations_judged"] = len(valid)
    result["combinations"] = rows

    if not valid:
        result["verdict"] = {"note": "no combination produced enough trades to judge"}
    else:
        best = max(valid, key=lambda r: r["in_sample"]["t_stat"])
        null_best = [max([t for t in rnd if t is not None], default=0.0)
                     for rnd in null_rounds]
        z_crit = z_for(0.05 / len(valid))
        survivors = [f"{r['entry']}/{r['exit']}" for r in valid
                     if (r["out_of_sample"].get("t_stat") or 0) > 1.96]
        result["verdict"] = {
            "combinations_judged": len(valid),
            "bonferroni_z_threshold": z_crit,
            "best": f"{best['entry']} / {best['exit']}",
            "best_in_sample_t": best["in_sample"]["t_stat"],
            "best_out_of_sample_t": best["out_of_sample"].get("t_stat"),
            "best_out_of_sample_expectancy": best["out_of_sample"].get("expectancy_points_net"),
            "permutation_null_best_t_per_round": null_best,
            "beats_bonferroni": bool(best["in_sample"]["t_stat"] > z_crit),
            "beats_permutation_null": bool(best["in_sample"]["t_stat"] > max(null_best))
            if null_best else None,
            "holds_out_of_sample": bool((best["out_of_sample"].get("t_stat") or 0) > 1.96),
            "positive_out_of_sample_at_1_96": survivors,
            "expected_by_chance_at_1_96": round(0.025 * len(valid), 1),
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
