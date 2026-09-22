#!/usr/bin/env python3
"""Which live trades made money, which lost it, and which of that is real.

The obvious way to read 952 live trades is to rank the symbols, keep the
winners and drop the losers. This file exists because that is the single
most reliable way to lose money with a spreadsheet, and because the answer
it gives is knowable in advance: **the entries are `random`.** There is no
signal in them by construction, so any pattern in WHICH random entries won
is noise unless something outside the outcomes explains it.

So the question is not "which symbol won" -- it is "is the spread between
symbols bigger than chance produces, and is any of it explained by a
quantity measured independently of the outcomes". Exactly one such quantity
exists here, and it is cost. The spread is a property of the broker and the
bracket, known before a trade is taken, and `cost_hurdle.py` already prices
it. Everything else on the row is the outcome itself.

Three tests, in the order that matters:

  * **Cost attribution.** Each trade's own spread over its own stop distance
    is its cost in R. Compare a symbol's observed mean R to minus that. What
    the cost explains is REAL and actionable -- drop an expensive symbol and
    the saving persists. What is left over is the residual.
  * **A permutation test on the ranking.** Shuffle the symbol labels across
    trades and recompute the best-minus-worst spread, thousands of times.
    If the real spread sits inside that distribution, the ranking is what
    random labels look like and "our best pair" means nothing.
  * **The payoff decomposition.** A high win rate with a low payoff is not
    an edge, it is a harvest rule. Split the record by how each trade ENDED
    and the mechanism is visible in one table.

`--suggest` prints what the arithmetic supports and, explicitly, what it
does not. Nothing here tunes a parameter until a number looks better.

    python tools/trade_autopsy.py
    python tools/trade_autopsy.py --suggest --out reports/trade_autopsy.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401

LEDGER = "data/track_record.jsonl"

# MT5 DEAL_REASON, read off the package's own constants rather than written
# from memory. The first version of this file had 3=SL and 4=TP, which is
# wrong by one and produced a table reading "stop loss: +297.31 net, 93.9%
# win" and "take profit: -474.78, 0% win" -- the harvest's numbers under the
# stop's name and vice versa. An enum guessed rather than read is the same
# defect mt5_retcodes.py exists to prevent, and it inverted the single most
# important table here.
REASON = {0: "client close", 1: "mobile", 2: "web", 3: "harvest (expert)",
          4: "stop loss", 5: "take profit", 6: "stop out", 7: "rollover",
          8: "variation margin", 9: "split"}


def load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def usable(rows: list[dict]) -> list[dict]:
    """Trades with an R-multiple and a bracket, which is what pools.

    A trade with no order-log entry has no stop distance, so neither its R
    nor its cost in R can be formed. Dropping it is not the same as treating
    it as zero, and the count is reported.
    """
    out = []
    for r in rows:
        br = r.get("bracket") or {}
        if r.get("r_multiple") is None or not br.get("sl_distance"):
            continue
        out.append(r)
    return out


def t_of(x: np.ndarray) -> float | None:
    if len(x) < 3 or x.std(ddof=1) == 0:
        return None
    return round(float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))), 2)


def group(rows: list[dict], key) -> dict:
    out = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return dict(out)


def summarise(rs: list[dict]) -> dict:
    r = np.array([x["r_multiple"] for x in rs], dtype=float)
    net = float(sum(x["net_profit"] for x in rs))
    wins = sum(1 for x in rs if x["net_profit"] > 0)
    return {
        "trades": len(rs),
        "net": round(net, 2),
        "mean_r": round(float(r.mean()), 4),
        "win_rate": round(wins / len(rs), 3),
        "t": t_of(r),
    }


def permutation_spread(rows: list[dict], rounds: int, seed: int = 0) -> dict:
    """How big a best-minus-worst spread do random symbol labels produce?

    The ranking is the thing being judged, so the null has to destroy the
    link between symbol and outcome while preserving everything else: the
    same trades, the same group sizes, the same overall mean. Shuffling the
    labels does exactly that.
    """
    r = np.array([x["r_multiple"] for x in rows], dtype=float)
    labels = np.array([x["symbol"] for x in rows])
    syms, counts = np.unique(labels, return_counts=True)

    def spread_of(lab):
        means = [r[lab == s].mean() for s in syms]
        return float(max(means) - min(means))

    real = spread_of(labels)
    rng = np.random.default_rng(seed)
    null = np.empty(rounds)
    shuffled = labels.copy()
    for i in range(rounds):
        rng.shuffle(shuffled)
        null[i] = spread_of(shuffled)
    beat = int((null >= real).sum())
    return {
        "real_spread_r": round(real, 4),
        "null_mean_spread_r": round(float(null.mean()), 4),
        "null_p95_spread_r": round(float(np.percentile(null, 95)), 4),
        "rounds": rounds,
        "null_at_least_as_extreme": beat,
        "p_value": round((beat + 1) / (rounds + 1), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--rounds", type=int, default=5000)
    ap.add_argument("--hurdle", default="reports/cost_hurdle.json",
                    help="where to read each symbol's measured spread from")
    ap.add_argument("--suggest", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()

    raw = load(args.ledger)
    rows = usable(raw)
    rep: dict = {"ledger": args.ledger, "trades_in_ledger": len(raw),
                 "trades_usable": len(rows), "data_gaps": []}
    if len(raw) != len(rows):
        rep["data_gaps"].append(
            f"{len(raw) - len(rows)} trade(s) have no bracket or no R-multiple "
            "and are excluded rather than counted as zero")

    # Cost per symbol in R, measured independently of any outcome.
    #
    # cost_hurdle.py reports a breakeven WIN RATE rather than a raw spread,
    # and for a symmetric bracket that is the same quantity in the units
    # wanted here: w = 0.5 + s/(2R), so s/R = 2(w - 0.5). Every trade pays
    # it, win or lose. Reading the published figure rather than re-deriving
    # a spread keeps ONE measured quantity instead of two that can disagree.
    drag: dict[str, float] = {}
    try:
        hurdle = json.load(open(args.hurdle, encoding="utf-8"))

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, dict):
                        w = v.get("breakeven_win_rate_median_spread")
                        if w is not None:
                            drag[k] = round(2.0 * (float(w) - 0.5), 4)
                        walk(v)
        walk(hurdle)
    except (OSError, ValueError) as exc:
        rep["data_gaps"].append(f"{args.hurdle}: {exc}")

    if not drag:
        rep["data_gaps"].append(
            "no measured cost available, so cost attribution is SKIPPED "
            "rather than estimated from the outcomes it is meant to explain")
    else:
        rep["cost_r_per_trade"] = dict(sorted(drag.items(), key=lambda kv: kv[1]))
        rep["cost_note"] = (
            "cost in R = 2 x (breakeven win rate - 0.5), from cost_hurdle.py's "
            "measured median spread. It assumes the symmetric 1.0 reward:risk "
            "bracket, which is the large majority of these trades.")

    by_symbol = {s: summarise(rs)
                 for s, rs in group(rows, lambda r: r["symbol"]).items()}
    for sym, d in by_symbol.items():
        if sym in drag:
            d["cost_r"] = -drag[sym]
            d["residual_r"] = round(d["mean_r"] - d["cost_r"], 4)
    rep["by_symbol"] = dict(sorted(by_symbol.items(),
                                   key=lambda kv: -kv[1]["mean_r"]))
    rep["by_direction"] = {k: summarise(v) for k, v in
                           group(rows, lambda r: r["direction"]).items()}
    rep["by_exit"] = {REASON.get(k, str(k)): summarise(v) for k, v in
                      group(rows, lambda r: r.get("close_reason")).items()}
    rep["by_hour"] = {str(k): summarise(v) for k, v in
                      sorted(group(rows,
                                   lambda r: int(r["open_time"][11:13])).items())}
    rep["permutation"] = permutation_spread(rows, args.rounds)
    rep["overall"] = summarise(rows)

    # THE question: does measured cost explain the measured loss?
    #
    # Trade-weighted, because the symbols are not traded equally and the
    # expensive ones are not the most traded. If the residual is small
    # relative to the drag, the record is not a strategy failing - it is the
    # spread being paid, and no rearrangement of the same trades escapes it.
    if drag:
        per_trade = np.array([drag[r["symbol"]] for r in rows
                              if r["symbol"] in drag], dtype=float)
        obs = np.array([r["r_multiple"] for r in rows
                        if r["symbol"] in drag], dtype=float)
        predicted = -float(per_trade.mean())
        residual = obs + per_trade          # observed minus (minus cost)
        rep["cost_attribution"] = {
            "trades": int(len(obs)),
            "observed_mean_r": round(float(obs.mean()), 4),
            "predicted_cost_r": round(predicted, 4),
            "residual_mean_r": round(float(residual.mean()), 4),
            "residual_t": t_of(residual),
            "share_of_loss_explained_by_cost": (
                round(predicted / float(obs.mean()), 3)
                if obs.mean() < 0 else None),
        }

    # What the harvest would have to collect to break even, holding the stop
    # rate fixed. Arithmetic, not a fitted parameter - and see the caveat the
    # suggestion prints beside it.
    ex_counts = {REASON.get(k, str(k)): v for k, v in
                 group(rows, lambda r: r.get("close_reason")).items()}
    harvest = ex_counts.get("harvest (expert)")
    stopped = ex_counts.get("stop loss")
    if harvest and stopped:
        n = len(rows)
        p_stop = len(stopped) / n
        p_harv = len(harvest) / n
        mean_stop = float(np.mean([r["r_multiple"] for r in stopped]))
        needed = (-p_stop * mean_stop) / p_harv if p_harv else None
        rep["harvest_arithmetic"] = {
            "harvest_share": round(p_harv, 3),
            "harvest_mean_r": round(float(np.mean(
                [r["r_multiple"] for r in harvest])), 4),
            "stop_share": round(p_stop, 3),
            "stop_mean_r": round(mean_stop, 4),
            "harvest_r_needed_to_offset_stops": round(needed, 4) if needed else None,
        }

    # ------------------------------------------------------------- printing
    o = rep["overall"]
    print(f"\n  {len(rows)} usable trades of {len(raw)}   "
          f"mean {o['mean_r']:+.4f}R   win {o['win_rate']*100:.1f}%   t {o['t']}")

    print("\n  BY SYMBOL -- and what cost explains")
    print("  " + "-" * 74)
    head = f"  {'symbol':9}{'trades':>7}{'net':>9}{'mean R':>9}{'win':>7}"
    if drag:
        head += f"{'cost R':>9}{'residual':>10}"
    print(head)
    for sym, d in rep["by_symbol"].items():
        line = (f"  {sym:9}{d['trades']:>7}{d['net']:>9.2f}"
                f"{d['mean_r']:>9.4f}{d['win_rate']*100:>6.1f}%")
        if "cost_r" in d:
            line += f"{d['cost_r']:>9.4f}{d['residual_r']:>10.4f}"
        print(line)

    pm = rep["permutation"]
    print(f"\n  IS THE RANKING REAL?  best-minus-worst = {pm['real_spread_r']:.4f}R")
    print(f"    shuffling the symbol labels {pm['rounds']} times gives a mean "
          f"spread of {pm['null_mean_spread_r']:.4f}R,")
    print(f"    95th percentile {pm['null_p95_spread_r']:.4f}R, and "
          f"{pm['null_at_least_as_extreme']} of {pm['rounds']} runs were at "
          f"least as extreme.")
    print(f"    p = {pm['p_value']}  ->  "
          + ("the ranking is INSIDE what random labels produce"
             if pm["p_value"] > 0.05 else
             "the ranking is larger than random labels produce"))

    print("\n  BY EXIT -- the mechanism, not an edge")
    print("  " + "-" * 74)
    print(f"  {'ended by':26}{'trades':>7}{'net':>9}{'mean R':>9}{'win':>7}")
    for k, d in sorted(rep["by_exit"].items(), key=lambda kv: -kv[1]["trades"]):
        print(f"  {k:26}{d['trades']:>7}{d['net']:>9.2f}"
              f"{d['mean_r']:>9.4f}{d['win_rate']*100:>6.1f}%")

    ca = rep.get("cost_attribution")
    if ca:
        print("\n  DOES COST EXPLAIN THE LOSS?")
        print("  " + "-" * 74)
        print(f"    observed        {ca['observed_mean_r']:+.4f}R a trade")
        print(f"    predicted cost  {ca['predicted_cost_r']:+.4f}R a trade   "
              "(measured spread, independent of these outcomes)")
        print(f"    residual        {ca['residual_mean_r']:+.4f}R   "
              f"t = {ca['residual_t']}")
        if ca["share_of_loss_explained_by_cost"] is not None:
            print(f"    -> cost accounts for "
                  f"{ca['share_of_loss_explained_by_cost']*100:.0f}% of the loss")

    ha = rep.get("harvest_arithmetic")
    if ha:
        print("\n  THE HARVEST ARITHMETIC")
        print("  " + "-" * 74)
        print(f"    {ha['harvest_share']*100:.0f}% of trades harvest at "
              f"{ha['harvest_mean_r']:+.4f}R")
        print(f"    {ha['stop_share']*100:.0f}% are stopped at "
              f"{ha['stop_mean_r']:+.4f}R")
        print(f"    the harvest would need "
              f"{ha['harvest_r_needed_to_offset_stops']:+.4f}R to offset the "
              "stops, against " f"{ha['harvest_mean_r']:+.4f}R today")

    print("\n  BY DIRECTION")
    for k, d in rep["by_direction"].items():
        print(f"    {k:6} {d['trades']:>5} trades  {d['mean_r']:+.4f}R  t {d['t']}")

    if args.suggest:
        print("\n  WHAT THE ARITHMETIC SUPPORTS")
        print("  " + "-" * 74)
        for line in suggestions(rep):
            print(f"  {line}")

    if rep["data_gaps"]:
        print()
        for g in rep["data_gaps"]:
            print(f"  gap: {g}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
        print(f"\n  wrote {args.out}")
    return 0


def suggestions(rep: dict) -> list[str]:
    """Only what survives the tests above. Ranking is not one of them."""
    out = []
    pm = rep["permutation"]
    if pm["p_value"] > 0.05:
        out.append(
            "DO NOT drop or favour a symbol on its live ranking. The "
            f"best-minus-worst spread of {pm['real_spread_r']:.4f}R is what "
            f"shuffled labels produce (p = {pm['p_value']}), so the ranking "
            "carries no information about which pair to trade.")
    else:
        out.append(
            f"The symbol ranking is larger than chance (p = {pm['p_value']}). "
            "That is necessary and not sufficient - check the residual column "
            "before acting, because cost explains a ranking without any pair "
            "being better to trade.")

    costs = {s: d["cost_r"] for s, d in rep["by_symbol"].items() if "cost_r" in d}
    if costs:
        worst = min(costs, key=lambda s: costs[s])
        best = max(costs, key=lambda s: costs[s])
        out.append(
            f"Cost IS real and is known before a trade: {best} pays "
            f"{-costs[best]:.4f}R a trade against {worst} at "
            f"{-costs[worst]:.4f}R. Trading the cheap end is defensible "
            "because the saving is a property of the broker, not of these "
            "outcomes - but it saves the DIFFERENCE, not the observed net.")

    ex = rep["by_exit"]
    stops = ex.get("stop loss")
    client = ex.get("harvest (expert)")
    if stops and client:
        out.append(
            f"The mechanism, not an edge: {client['trades']} trades closed by "
            f"the harvest at {client['mean_r']:+.4f}R against "
            f"{stops['trades']} stopped out at {stops['mean_r']:+.4f}R. The "
            "high win rate IS this asymmetry. No rule that keeps it can be "
            "fixed by picking symbols.")
    out.append(
        "The entries are `random`, so there is no signal to concentrate. The "
        "only lever with a measured sign is cost, and it points at trading "
        "LESS, not at trading different pairs.")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
