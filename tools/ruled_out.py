#!/usr/bin/env python3
"""What size of edge has this project actually RULED OUT?

Twenty-five searches have come back null, and the record says so in prose.
What the prose does not say is the only thing a decision can be made on: **how
large an edge would each search have caught?** A null from a search that could
only have detected a 10-point edge says nothing about a 1-point one. This file
turns each null into a bound.

**Every input is read from a report on disk, never supplied here.** A bound
computed from remembered numbers would be exactly the generated-figure failure
this repository exists to prevent -- and building this file found that
`reports/gross_bound.json` had never been written, so the per-trade standard
deviation it needed existed only in a conversation until the tool was re-run.

**The conversion to R is exact, not assumed.** Every search judged here used a
symmetric 1.5xATR bracket, so each trade is close to +1R or -1R, and
`gross_bound` measures the per-trade standard deviation directly: 0.9999R over
165,888 trades. With that, an edge of mu R and n trades has a standard error of
sd/sqrt(n), and power follows.

**The gates modelled are the two this project applies to every candidate**: the
in-sample test at the search's own Bonferroni threshold, and the
out-of-sample test at 1.96, on independent samples, so joint power is their
product. The minimum detectable edge is the mu at which that joint power
reaches 80%.

**Read these as LOWER bounds on what was needed, not upper.** The permutation
null, the era blocks, the walk-forward and date clustering are further gates a
real survivor must pass, and none is modelled. Each one raises the edge needed,
so the true detectable edge is larger than printed and the exclusion is weaker
than printed. The direction of the error is stated because it is the
flattering one.

    python tools/ruled_out.py
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_search import z_for

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports")
POWER = 0.80

#: Distinct searches, one report each. Reruns of the same hypothesis are NOT
#: counted twice -- rule_search.json, _nullcheck, _rerun and _skiph0 are one
#: search, as are the three D1 files -- and control arms (tick volume raw) are
#: not searches at all. `fx_majors` marks where cost_profile's hurdle applies;
#: crosses, metals, indices and crypto have their own spreads, and applying the
#: majors' hurdle to them would be the metals error in another form.
SEARCHES = (
    ("rule_search_rerun.json", "41-rule baseline", True),
    ("rule_search_h4.json", "41 rules at H4", True),
    ("rule_search_d1_walk.json", "41 rules at D1", True),
    ("shape_search.json", "candle shape / vol regime", True),
    ("supertrend_search_h1.json", "Supertrend", True),
    ("calendar_rule_d1.json", "calendar rules", True),
    ("book_rules_search.json", "bookshelf rules", True),
    ("pugh_search.json", "Pugh 2- and 3-bar", True),
    ("tick_volume_search.json", "tick volume", True),
    ("rule_search_crosses.json", "8 non-USD crosses", False),
    ("rule_search_metals.json", "4 metals", False),
    ("rule_search_indices.json", "7 equity indices", False),
    ("rule_search_crypto.json", "7 crypto pairs", False),
    ("volume_search_d1.json", "crypto volume", False),
)


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def joint_power(mu, sd, n_is, z_is, n_oos, z_oos=1.96):
    """P(clear in-sample Bonferroni AND out-of-sample 1.96), true edge mu R."""
    if n_is <= 0 or n_oos <= 0:
        return 0.0
    p1 = phi(mu * math.sqrt(n_is) / sd - z_is)
    p2 = phi(mu * math.sqrt(n_oos) / sd - z_oos)
    return p1 * p2


def min_detectable(sd, n_is, z_is, n_oos, z_oos=1.96, power=POWER):
    """Smallest edge in R that clears both gates with the stated power.

    Bisection, because joint power is monotone in mu and the product has no
    closed-form inverse. Returns inf when the samples are empty.
    """
    if n_is <= 0 or n_oos <= 0:
        return float("inf")
    lo, hi = 0.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if joint_power(mid, sd, n_is, z_is, n_oos, z_oos) < power:
            lo = mid
        else:
            hi = mid
    return hi


def trades_needed(sd, target, n_is, z_is, n_oos, z_oos=1.96, power=POWER):
    """Scale a search's samples until its detectable edge equals `target`.

    The in- to out-of-sample ratio is held fixed, because that ratio is the
    search's own design. Returns the in-sample trades PER CANDIDATE the search
    would have needed to catch an edge exactly `target` R in size -- the data
    required before its null could say anything about an edge just large
    enough to pay the spread.
    """
    if n_is <= 0 or n_oos <= 0 or not target or target <= 0:
        return float("inf")
    ratio = n_oos / n_is
    lo, hi = 1.0, 1e9
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if min_detectable(sd, mid, z_is, mid * ratio, z_oos, power) > target:
            lo = mid
        else:
            hi = mid
    return hi


def load(name):
    with open(os.path.join(REPORTS, name), encoding="utf-8") as fh:
        return json.load(fh)


def per_trade_sd():
    """The per-trade standard deviation in R, from gross_bound's own report."""
    g = load("gross_bound.json")["results"]["tie_to_loss"]
    return g["se"] * math.sqrt(g["n"]), g["n"]


def hurdle_by_timeframe():
    """Cost in R per timeframe, which is spread over bracket.

    Breakeven win rate is w = 0.5 + s/(2R), so the cost in R is 2(w - 0.5) =
    s/R -- the `median_spread_over_bracket` cost_profile already records.
    """
    tf = load("cost_profile.json")["by_timeframe"]
    return {k: float(v["median_spread_over_bracket"]) for k, v in tf.items()}


def summarise(name):
    d = load(name)
    cands = [c for c in d["candidates"] if isinstance(c, dict) and "in_sample" in c]
    n_is = [c["in_sample"].get("trades", 0) or 0 for c in cands]
    n_oos = [c.get("out_of_sample", {}).get("trades", 0) or 0 for c in cands]
    v = d["verdict"]
    return {"timeframe": str(d.get("timeframe", "")),
            "k": int(v.get("candidates_judged", len(cands))),
            "z": float(v["bonferroni_z_threshold"]),
            "n_is": int(statistics.median(n_is)) if n_is else 0,
            "n_oos": int(statistics.median(n_oos)) if n_oos else 0,
            "sl": d.get("sl_atr"), "tp": d.get("tp_atr")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="reports/ruled_out.json")
    args = ap.parse_args()

    sd, sd_n = per_trade_sd()
    hurdles = hurdle_by_timeframe()
    print(f"\n  what each null search could have detected, at {POWER:.0%} power")
    print(f"  per-trade sd {sd:.4f}R from gross_bound ({sd_n:,} trades); "
          f"both gates modelled, others not")
    print("  hurdle = the spread in R at that timeframe, from cost_profile")
    print("  " + "-" * 96)
    print(f"  {'search':28}{'tf':>4}{'k':>5}{'IS n':>8}{'OOS n':>8}"
          f"{'MDE R':>9}{'MDE pts':>9}{'hurdle R':>10}{'MDE/hurdle':>12}")

    rows = []
    for fname, label, majors in SEARCHES:
        s = summarise(fname)
        if s["sl"] != s["tp"]:
            print(f"  {label:28} skipped: asymmetric bracket, the 1R conversion "
                  f"does not hold")
            continue
        mde = min_detectable(sd, s["n_is"], s["z"], s["n_oos"])
        tf = {"1d": "D1"}.get(s["timeframe"], s["timeframe"])
        hurdle = hurdles.get(tf) if majors else None
        ratio = mde / hurdle if hurdle else None
        need = (trades_needed(sd, hurdle, s["n_is"], s["z"], s["n_oos"])
                if hurdle else None)
        rows.append({"trades_needed_is": need,
                     "search": label, "report": fname, "timeframe": s["timeframe"],
                     "k": s["k"], "z_bonferroni": s["z"], "n_is": s["n_is"],
                     "n_oos": s["n_oos"], "mde_r": mde, "mde_winrate_pts": mde / 2 * 100,
                     "hurdle_r": hurdle, "mde_over_hurdle": ratio})
        print(f"  {label:28}{s['timeframe']:>4}{s['k']:>5}{s['n_is']:>8,}"
              f"{s['n_oos']:>8,}{mde:>9.4f}{mde / 2 * 100:>9.2f}"
              f"{hurdle if hurdle else float('nan'):>10.4f}"
              f"{(f'{ratio:.1f}x') if ratio else 'n/a':>12}")

    # Across the whole project the correction is over every candidate in every
    # search, not per search. That is the honest multiplicity, and it is worse.
    K = sum(r["k"] for r in rows)
    z_all = z_for(0.05 / K)
    for r in rows:
        r["mde_r_family"] = min_detectable(sd, r["n_is"], z_all, r["n_oos"])
    best = min(rows, key=lambda r: r["mde_r"])
    best_fx = min((r for r in rows if r["hurdle_r"]), key=lambda r: r["mde_over_hurdle"])

    print("  " + "-" * 96)
    print(f"  {len(rows)} distinct searches, {K} candidates in total; family-wide "
          f"Bonferroni z = {z_all:.3f} against the per-search {best['z_bonferroni']:.3f}")
    print(f"  best-powered: {best['search']} -- rules out edges above "
          f"{best['mde_r']:.4f}R ({best['mde_winrate_pts']:.2f} win-rate points) per "
          f"search, {best['mde_r_family']:.4f}R family-wide")
    print(f"  closest to its hurdle: {best_fx['search']} at "
          f"{best_fx['mde_over_hurdle']:.1f}x the spread it would have to pay")
    below = [r["search"] for r in rows if r["mde_over_hurdle"] and r["mde_over_hurdle"] < 1]
    print(f"  searches able to detect an edge JUST big enough to pay the spread: "
          f"{len(below)} of {sum(1 for r in rows if r['hurdle_r'])}")
    print("\n  in-sample trades PER CANDIDATE each search would have needed to see")
    print("  an edge exactly as large as the spread it has to pay:")
    for r in rows:
        if r["trades_needed_is"]:
            print(f"    {r['search']:28}{r['timeframe']:>4}  had {r['n_is']:>7,}"
                  f"  needed {r['trades_needed_is']:>11,.0f}"
                  f"  ({r['trades_needed_is'] / r['n_is']:.1f}x)")
    print("\n  these are LOWER bounds on the edge needed: the permutation null, era")
    print("  blocks, walk-forward and clustering are further gates and each raises it.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"per_trade_sd_r": sd, "sd_from_trades": sd_n, "power": POWER,
                   "family_candidates": K, "family_z": z_all,
                   "searches": rows,
                   "not_modelled": ["permutation null", "era blocks",
                                    "walk-forward", "date clustering"]},
                  fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
