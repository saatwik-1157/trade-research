#!/usr/bin/env python3
"""One candidate, chosen before the data, tested once on history no rule has been judged on.

`ruled_out.py` showed why twenty-five searches could not find a spread-sized
edge: each tested dozens of candidates, paid a Bonferroni threshold near 3,
and had a few thousand trades per candidate. This is the other design. **One
candidate, chosen mechanically from what the searches already saw, tested
once, at z = 1.96, on H1 history that no rule candidate has been evaluated
on.** With k = 1 there is no correction to pay, and every bar before the
earliest H1 rule search is available as a holdout.

It runs in two steps, and the order is the whole point.

  select    reads the existing reports -- nothing from MT5, no holdout bar --
            applies the selection rule below and writes `prereg/plan.json`:
            the candidate, the holdout window, the cost rule, the decision
            rule, the projected power, and a SHA-256 of every file the
            analysis will run. That file and this code are then committed.

  rehearse  after select, BEFORE committing: fetches both windows, checks
            the holdout depth and spreads, and runs the reproduction gate --
            the whole MT5 path confirm will take, minus every holdout trade.
            The plan hashes this file, so a bug found in that path after
            committing could never be fixed; this finds it while it can.

  confirm   refuses to run unless the plan and every analysis file are
            committed, unmodified and hash-identical to the plan, and refuses
            to run at all if a result has ever existed, on disk or anywhere in
            git history. It first reproduces the candidate on its selection
            window -- if the code no longer computes what was selected, it
            stops before a single holdout trade is computed -- then runs the
            holdout once and writes `prereg/result.json`.

**The selection rule.** The pool is every candidate the six H1 FX-majors
bracketed searches judged (at least 100 in-sample trades). Eligible: net
expectancy above zero in BOTH the in-sample and the out-of-sample part of its
search. Ranked by the WEAKER of its two t-statistics -- the conjunction
statistic, which rewards agreement between the halves rather than one strong
half -- ties broken by more trades. Tick-volume candidates are in the pool but
cannot be rebuilt here without their volume binding; if one is ever selected,
confirm refuses rather than substituting.

The rule was written after reading the selection-period rankings, which is
legitimate for one reason only: the holdout has not been looked at. Any
selection procedure yields a valid single test on untouched data; what makes
the test invalid is choosing with the holdout in view, and the two-step
structure is what prevents it.

**What "untouched" means here, stated precisely.** No rule CANDIDATE has been
evaluated on H1 FX-majors bars before the holdout end. The period is not
unseen data: `gross_bound` (random entries), `gotobi` (a USDJPY time-of-day
window), `path_order`, `round_number` and `spread_timing` read 50,000 H1 bars;
`rule_backtest` defaults to the live strategy's rules over the same depth; and
the D1 searches, including the 41 rules at D1, cover it at daily resolution.
None of those is the selected hypothesis, so none can have selected it -- but
the claim is "untouched for this hypothesis", not "never read".

**The decision rule, fixed here before any holdout trade exists.**

  PRIMARY    pooled holdout net t > 1.96 and net expectancy > 0. Strictly
             greater: a t of exactly 1.96 fails.
  SECONDARY  date-clustered t significant, positive and agreeing in sign with
             the pooled mean; AND net positive in at least 3 of 4
             equal-duration eras of the holdout.

  CONFIRMED     primary and both secondaries
  PRIMARY ONLY  primary passes, a secondary fails: a pooled result carried by
                crowded dates or one era -- recorded as NOT an edge
  NOT CONFIRMED primary fails; the result also states the smallest net edge
                the realised holdout could have detected at 80% power, so a
                null says how large an edge it excludes and no more

**Cost.** The spread charged per symbol is the LARGER of its recorded median
over the holdout and over the selection window: the cheaper of two costs is
the flattering one. Financing is not charged, as it was not in the searches
the candidate came from; that also flatters, and is stated for that reason.

    python tools/prereg.py select
    python tools/prereg.py rehearse     # needs MT5; no holdout trade
    python tools/prereg.py confirm      # needs MT5; once, ever
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import math
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REPORTS = os.path.join(REPO, "reports")
PLAN = "prereg/plan.json"
RESULT = "prereg/result.json"

#: (report, module, candidate builder). The builder is what confirm calls to
#: rebuild the selected candidate; None means it cannot be rebuilt faithfully.
POOL = (
    ("rule_search_rerun.json", "rule_search", "build_candidates"),
    ("shape_search.json", "shape_search", "build_shape_candidates"),
    ("supertrend_search_h1.json", "supertrend_search", "build_supertrend_candidates"),
    ("book_rules_search.json", "book_rules_search", "build_book_candidates"),
    ("pugh_search.json", "pugh_search", "build_pugh_candidates"),
    ("tick_volume_search.json", "tick_volume_search", None),
)
#: What every search in the pool used. confirm uses the same, not its own.
SPEC = {"timeframe": "H1", "sl_atr": 1.5, "tp_atr": 1.5, "split": 0.7,
        "spread_source": "median", "skip_hours": [], "cost_swap": False,
        "symbols": ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD"]}

MIN_TRADES = 100
Z_PRIMARY = 1.96
Z_POWER = 0.8416212335729143      # standard normal quantile at 0.80
ERAS = 4
ERAS_POSITIVE_MIN = 3
HOLDOUT_BARS = 50_000             # the depth fetch_rates reliably returns at H1
MIN_HOLDOUT_YEARS = 3.0
REPRO_TRADES_TOL = 0.03
REPRO_EXPECTANCY_TOL = 0.25
BASE_FILES = ("tools/prereg.py", "tools/rule_search.py", "tools/rule_backtest.py")
YEAR_S = 365.25 * 86400


# ------------------------------------------------------------------ selection
def to_epoch(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp())


def from_epoch(t: float) -> str:
    return dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def r_points(views) -> float | None:
    """The bracket in points, from booked wins and losses.

    With a symmetric bracket a win nets R - s and a loss -R - s, so half their
    difference is R whatever the spread. Weighted by trades across the views.
    """
    num = den = 0.0
    for v in views:
        w, lo, n = v.get("avg_win_points"), v.get("avg_loss_points"), v.get("trades") or 0
        if w is None or lo is None or not n:
            continue
        num += (w - lo) / 2.0 * n
        den += n
    return num / den if den else None


def pool_candidates(reports_dir: str = REPORTS, pool=POOL):
    """Every judged candidate in the pool, with the figures selection reads."""
    out = []
    for fname, module, builder in pool:
        path = os.path.join(reports_dir, fname)
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        for c in d["candidates"]:
            ins, oos = c.get("in_sample") or {}, c.get("out_of_sample") or {}
            if (ins.get("trades") or 0) < MIN_TRADES:
                continue
            out.append({
                "report": fname, "report_sha256": sha256(path),
                "module": module, "builder": builder,
                "name": c["name"], "family": c.get("family"),
                "window": d["window"],
                "is_trades": ins["trades"], "is_t": ins["t_stat"],
                "is_net": ins["expectancy_points_net"],
                "oos_trades": oos.get("trades") or 0, "oos_t": oos.get("t_stat"),
                "oos_net": oos.get("expectancy_points_net"),
                "r_points": r_points((ins, oos)),
            })
    return out


def eligible(c) -> bool:
    return (c["is_net"] is not None and c["is_net"] > 0
            and c["oos_net"] is not None and c["oos_net"] > 0
            and c["is_t"] is not None and c["oos_t"] is not None)


def rank_key(c):
    """Weaker half first, then sample size. Sorted descending."""
    return (min(c["is_t"], c["oos_t"]), c["is_trades"] + c["oos_trades"])


def select_candidate(cands):
    ranked = sorted((c for c in cands if eligible(c)), key=rank_key, reverse=True)
    return (ranked[0] if ranked else None), ranked


def holdout_end(reports_dir: str = REPORTS) -> str:
    """Midnight on the earliest date any H1 report mentions.

    Taking the earliest date ANYWHERE in an H1 report, not just its window,
    can only move the end earlier and shorten the holdout -- the safe
    direction when the question is which bars no rule has been judged on.
    """
    earliest = None
    for name in sorted(os.listdir(reports_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(reports_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        if not re.search(r'"timeframe":\s*"H1"', text):
            continue
        for d in re.findall(r"(20\d\d-\d\d-\d\d)", text):
            earliest = d if earliest is None or d < earliest else earliest
    if earliest is None:
        raise SystemExit("no H1 report found; the holdout end cannot be fixed")
    return earliest + "T00:00:00"


def detectable_edge(n: float, sd: float = 1.0, z: float = Z_PRIMARY) -> float:
    """Smallest true edge a one-gate test of n trades sees with 80% power."""
    return float("inf") if n <= 0 else (z + Z_POWER) * sd / math.sqrt(n)


def power_at(mu: float, n: float, sd: float = 1.0, z: float = Z_PRIMARY) -> float:
    if n <= 0:
        return 0.0
    return 0.5 * (1.0 + math.erf((mu * math.sqrt(n) / sd - z) / math.sqrt(2.0)))


# --------------------------------------------------------------- the decision
def decide(pooled: dict, by_date: dict, eras: list) -> dict:
    """Apply the fixed decision rule to holdout figures. Pure, so it is tested."""
    t = pooled.get("t_stat") or 0.0
    net = pooled.get("expectancy_points_net") or 0.0
    primary = bool(t > Z_PRIMARY and net > 0)
    date_ok = bool(by_date.get("date_cluster_significant")
                   and (by_date.get("t_stat_clustered_by_date") or 0) > 0
                   and not by_date.get("signs_disagree"))
    positive = sum(1 for e in eras if e.get("trades") and (e.get("expectancy_points_net") or 0) > 0)
    eras_ok = positive >= ERAS_POSITIVE_MIN
    if not primary:
        label = "NOT CONFIRMED"
    elif date_ok and eras_ok:
        label = "CONFIRMED"
    else:
        label = "PRIMARY ONLY"
    return {"verdict": label, "primary": primary, "date_clustering_ok": date_ok,
            "eras_positive": positive, "eras_ok": eras_ok}


def reproduces(report_figs: dict, got_trades: int, got_net: float) -> tuple[bool, str]:
    """Does the code still compute what was selected, on the selection window?"""
    want_n, want_net = report_figs["trades"], report_figs["net"]
    if want_n <= 0:
        return False, "the plan records no selection trades"
    dn = abs(got_trades - want_n) / want_n
    if dn > REPRO_TRADES_TOL:
        return False, f"trades {got_trades} against {want_n} selected ({dn:.1%} apart)"
    if got_net * want_net <= 0:
        return False, f"net expectancy {got_net:.2f} has a different sign from {want_net:.2f}"
    de = abs(got_net - want_net) / abs(want_net)
    if de > REPRO_EXPECTANCY_TOL:
        return False, f"net expectancy {got_net:.2f} against {want_net:.2f} ({de:.0%} apart)"
    return True, f"trades {got_trades} vs {want_n}, net {got_net:.2f} vs {want_net:.2f}"


# ------------------------------------------------------------------ the guard
def _git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True,
                          text=True, check=True).stdout


def guard(repo: str, plan: dict, plan_rel: str = PLAN, result_rel: str = RESULT) -> list[str]:
    """Every reason confirm must not run. Empty means it may, once."""
    problems = []
    files = [plan_rel] + sorted(plan["analysis_sha256"])
    tracked = set(_git(repo, "ls-files", "--", *files).split())
    for f in files:
        if f not in tracked:
            problems.append(f"{f} is not committed")
    dirty = _git(repo, "status", "--porcelain", "--", *files).strip()
    if dirty:
        problems.append("uncommitted changes to the plan or the analysis:\n" + dirty)
    for f, h in plan["analysis_sha256"].items():
        p = os.path.join(repo, f)
        if not os.path.exists(p) or sha256(p) != h:
            problems.append(f"{f} has changed since the pre-registration")
    if os.path.exists(os.path.join(repo, result_rel)):
        problems.append(f"{result_rel} exists: this test has already been run")
    elif _git(repo, "log", "--all", "--format=%h", "--", result_rel).strip():
        problems.append(f"{result_rel} is in git history: this test has already been run")
    return problems


# --------------------------------------------------------------------- select
def select(args) -> int:
    cands = pool_candidates(args.reports)
    chosen, ranked = select_candidate(cands)
    if chosen is None:
        raise SystemExit("no candidate is net-positive in both halves; nothing to pre-register")
    end = holdout_end(args.reports)
    w = chosen["window"]
    sel_s = to_epoch(w["to"]) - to_epoch(w["from"])
    bars_per_s = w["bars_per_symbol"] / sel_s
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    bars_after = (now - to_epoch(end)) * bars_per_s
    hold_bars = max(0.0, HOLDOUT_BARS - bars_after)
    sel_trades = chosen["is_trades"] + chosen["oos_trades"]
    proj_n = sel_trades * hold_bars / w["bars_per_symbol"]
    hold_from_est = to_epoch(end) - hold_bars / bars_per_s
    net_sel = (chosen["is_net"] * chosen["is_trades"]
               + chosen["oos_net"] * chosen["oos_trades"]) / sel_trades
    net_r = net_sel / chosen["r_points"]
    hurdle = None
    cp = os.path.join(args.reports, "cost_profile.json")
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as fh:
            hurdle = float(json.load(fh)["by_timeframe"]["H1"]["median_spread_over_bracket"])

    power = {"projected_holdout_trades": round(proj_n),
             "projected_holdout_years": round(hold_bars / bars_per_s / YEAR_S, 2),
             "per_trade_sd_r_assumed": 1.0,
             "detectable_net_edge_r_at_80pct": round(detectable_edge(proj_n), 4),
             "selection_net_edge_r": round(net_r, 4),
             "power_at_selection_edge": round(power_at(net_r, proj_n), 3),
             "power_at_half_selection_edge": round(power_at(net_r / 2, proj_n), 3)}
    if hurdle:
        power["h1_spread_r"] = hurdle
        power["power_at_net_edge_of_one_spread"] = round(power_at(hurdle, proj_n), 3)
        power["detectable_over_spread"] = round(detectable_edge(proj_n) / hurdle, 2)

    files = list(BASE_FILES) + [f"tools/{chosen['module']}.py"]
    files = sorted(set(files))
    plan = {
        "pre_registered_at_utc": from_epoch(now),
        "hypothesis": (f"{chosen['name']} ({chosen['family']}, from {chosen['report']}) has "
                       "positive net expectancy on H1 FX-majors history no rule "
                       "candidate has been evaluated on."),
        "candidate": {k: chosen[k] for k in ("name", "family", "report", "module", "builder")},
        "selection": {
            "pool_reports": {f: sha256(os.path.join(args.reports, f)) for f, _, _ in POOL},
            "pool_size": len(cands), "eligible": len(ranked),
            "rule": ("net expectancy > 0 in both in-sample and out-of-sample; rank by "
                     "min(in-sample t, out-of-sample t), ties by total trades"),
            "window": w,
            "figures": {"in_sample": {"trades": chosen["is_trades"], "t": chosen["is_t"],
                                      "net": chosen["is_net"]},
                        "out_of_sample": {"trades": chosen["oos_trades"], "t": chosen["oos_t"],
                                          "net": chosen["oos_net"]},
                        "combined": {"trades": sel_trades, "net": round(net_sel, 4)},
                        "r_points": round(chosen["r_points"], 2)},
            "ranked_eligible": [{k: c[k] for k in ("name", "report", "is_trades", "is_t",
                                                   "is_net", "oos_trades", "oos_t", "oos_net")}
                                for c in ranked],
        },
        "spec": SPEC,
        "holdout": {"to_exclusive": end,
                    "from": f"earliest bar of the last {HOLDOUT_BARS:,} H1 bars; "
                            f"estimated {from_epoch(hold_from_est)[:10]}",
                    "min_years_per_symbol": MIN_HOLDOUT_YEARS,
                    "untouched_means": "no rule candidate evaluated on these H1 bars; "
                                       "see the module docstring for what has read them"},
        "cost": "per symbol, the larger of the holdout and selection-window median recorded spread; no financing",
        "decision": {"primary": f"pooled net t > {Z_PRIMARY} and net expectancy > 0",
                     "secondary": [f"date-clustered t significant, positive, signs agree",
                                   f"net positive in >= {ERAS_POSITIVE_MIN} of {ERAS} "
                                   "equal-duration holdout eras"],
                     "labels": ["CONFIRMED", "PRIMARY ONLY", "NOT CONFIRMED"]},
        "reproduction_gate": {"trades_tolerance": REPRO_TRADES_TOL,
                              "expectancy_tolerance": REPRO_EXPECTANCY_TOL,
                              "note": "run on the selection window before any holdout trade"},
        "power": power,
        "analysis_sha256": {f: sha256(os.path.join(REPO, f)) for f in files},
    }

    print(f"\n  pool {len(cands)} candidates from {len(POOL)} H1 searches; "
          f"{len(ranked)} net-positive in both halves")
    for c in ranked[:5]:
        print(f"    {c['name']:26}{c['report']:26} IS t {c['is_t']:>5}  n {c['is_trades']:>5}"
              f"   OOS t {c['oos_t']:>5}  n {c['oos_trades']:>5}   min t "
              f"{min(c['is_t'], c['oos_t']):>5}")
    print(f"  selected: {chosen['name']}")
    print(f"  holdout: {from_epoch(hold_from_est)[:10]} (est.) to {end} exclusive")
    for k, v in power.items():
        print(f"    {k:40} {v}")
    if args.dry_run:
        print("  dry run: plan not written")
        return 0
    out = os.path.join(REPO, PLAN)
    if _git(REPO, "log", "--all", "--format=%h", "--", PLAN).strip():
        raise SystemExit(f"{PLAN} is committed; the committed plan is the record and "
                         "is never rewritten")
    if os.path.exists(out) and not args.force:
        raise SystemExit(f"{PLAN} already exists; a plan is written once")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
    print(f"  wrote {PLAN} -- commit it and the analysis files before confirm")
    return 0


# -------------------------------------------------------------------- confirm
def build_market(mt5, rates_by_sym, spread_by_sym):
    import numpy as np
    from rule_backtest import atr_series
    market = {}
    for sym, r in rates_by_sym.items():
        o, h, l, c = (r["open"].astype(float), r["high"].astype(float),
                      r["low"].astype(float), r["close"].astype(float))
        t = r["time"].astype("int64")
        market[sym] = {"o": o, "h": h, "l": l, "c": c, "atr": atr_series(h, l, c),
                       "time": t, "entry_blocked": np.zeros(len(c), dtype=bool),
                       "swap": None, "triple_dow": None,
                       "point": mt5.symbol_info(sym).point, "spread": spread_by_sym[sym],
                       "n": len(c), "from": from_epoch(t[0]), "to": from_epoch(t[-1])}
    return market


def run_candidate(market, fn):
    import numpy as np
    from rule_search import _call_signal, collect_trades
    sigs = {s: np.where(m["entry_blocked"], 0, _call_signal(fn, m)) for s, m in market.items()}
    return collect_trades(market, sigs, SPEC["sl_atr"], SPEC["tp_atr"])


def load(plan, path=None):
    """Fetch, split and cost both windows, and rebuild the candidate.

    Shared by rehearse and confirm so the path confirm runs is the path that
    was rehearsed. Returns the holdout market but computes no holdout trade.
    """
    cand = plan["candidate"]
    if not cand["builder"]:
        raise SystemExit(f"{cand['name']} cannot be rebuilt without its volume binding; refused")
    built = getattr(importlib.import_module(cand["module"]), cand["builder"])()
    fn = {n: f for n, _, f in built}.get(cand["name"])
    if fn is None:
        raise SystemExit(f"{cand['name']} is no longer built by {cand['module']}; refused")

    from rule_backtest import choose_spread, connect, fetch_rates

    w = plan["selection"]["window"]
    s_from, s_to = to_epoch(w["from"]), to_epoch(w["to"])
    h_to = to_epoch(plan["holdout"]["to_exclusive"])
    mt5 = connect(path)
    sel_rates, hold_rates, spread = {}, {}, {}
    for sym in SPEC["symbols"]:
        if not mt5.symbol_select(sym, True):
            raise SystemExit(f"{sym}: could not select; refused rather than run on fewer symbols")
        r = fetch_rates(mt5, sym, HOLDOUT_BARS, SPEC["timeframe"], min_bars=300)
        if r is None:
            raise SystemExit(f"{sym}: no history returned")
        t = r["time"].astype("int64")
        sel_rates[sym] = r[(t >= s_from) & (t <= s_to)]
        hold_rates[sym] = r[t < h_to]
        yrs = (h_to - int(t[0])) / YEAR_S if len(hold_rates[sym]) else 0.0
        if yrs < MIN_HOLDOUT_YEARS:
            raise SystemExit(f"{sym}: {yrs:.2f} years of holdout, below {MIN_HOLDOUT_YEARS}; refused")
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        s_sel, _ = choose_spread(sel_rates[sym], info, tick, SPEC["spread_source"])
        s_hold, _ = choose_spread(hold_rates[sym], info, tick, SPEC["spread_source"])
        spread[sym] = {"selection": s_sel, "holdout": s_hold, "charged": max(s_sel, s_hold)}
    sel_mkt = build_market(mt5, sel_rates, {s: v["selection"] for s, v in spread.items()})
    hold_mkt = build_market(mt5, hold_rates, {s: v["charged"] for s, v in spread.items()})
    mt5.shutdown()
    return fn, sel_mkt, hold_mkt, spread


def reproduction_gate(plan, fn, sel_mkt):
    """Nothing after this runs if the code has drifted from what was selected.

    The Pugh tie fix is exactly such a drift: a report written before it and
    code run after it would select one rule and test another.
    """
    from rule_backtest import stats
    got = stats(run_candidate(sel_mkt, fn), 1.0)
    ok, note = reproduces(plan["selection"]["figures"]["combined"],
                          got.get("trades", 0), got.get("expectancy_points_net") or 0.0)
    print(f"  reproduction on the selection window: {note}")
    return ok, note


def rehearse(args) -> int:
    """The whole data path and the reproduction gate, without one holdout trade.

    Run BEFORE the plan is committed. The plan hashes this file, so a bug found
    in confirm's MT5 path after committing could never be fixed; rehearsing
    finds it while the plan can still be rewritten. It reads holdout BAR
    COUNTS and spreads, never a holdout return.
    """
    with open(os.path.join(REPO, PLAN), encoding="utf-8") as fh:
        plan = json.load(fh)
    fn, sel_mkt, hold_mkt, spread = load(plan, args.path)
    for s, m in hold_mkt.items():
        print(f"  {s}: holdout {m['from']} to {m['to']}, {m['n']:,} bars; spread points "
              + ", ".join(f"{k} {v / m['point']:.1f}" for k, v in spread[s].items()))
    ok, _ = reproduction_gate(plan, fn, sel_mkt)
    print("  rehearsal passed: commit the plan, then confirm" if ok
          else "  rehearsal FAILED: fix the code, re-run select --force, rehearse again")
    return 0 if ok else 1


def confirm(args) -> int:
    with open(os.path.join(REPO, PLAN), encoding="utf-8") as fh:
        plan = json.load(fh)
    problems = guard(REPO, plan)
    if problems:
        raise SystemExit("confirm refused:\n  - " + "\n  - ".join(problems))
    cand = plan["candidate"]
    import numpy as np
    from rule_backtest import stats
    from rule_search import block_edges, block_views, clustered_by_date, per_symbol_views

    fn, sel_mkt, hold_mkt, spread = load(plan, args.path)
    ok, note = reproduction_gate(plan, fn, sel_mkt)
    if not ok:
        raise SystemExit("reproduction failed; the holdout was NOT run and remains unused")

    rows = run_candidate(hold_mkt, fn)
    pooled = stats(rows, 1.0)
    by_date = clustered_by_date(rows)
    eras = block_views(rows, block_edges(hold_mkt, ERAS))
    net_r = np.array([r["net"] / (SPEC["sl_atr"] * hold_mkt[r["symbol"]]["atr"][r["entry_idx"] - 1]
                                  / hold_mkt[r["symbol"]]["point"]) for r in rows])
    sd_r = float(net_r.std(ddof=1)) if len(net_r) > 1 else float("nan")
    result = {
        "run_at_utc": from_epoch(dt.datetime.now(dt.timezone.utc).timestamp()),
        "plan_commit": _git(REPO, "log", "-1", "--format=%H", "--", PLAN).strip(),
        "candidate": cand["name"],
        **decide(pooled, by_date, eras),
        "holdout_window": {s: {"from": m["from"], "to": m["to"], "bars": m["n"]}
                           for s, m in hold_mkt.items()},
        "spread_points": {s: {k: round(v / hold_mkt[s]["point"], 2) for k, v in d.items()}
                          for s, d in spread.items()},
        "reproduction": note, "pooled": pooled, "by_date": by_date, "eras": eras,
        "per_symbol": per_symbol_views(rows),
        "net_r_mean": round(float(net_r.mean()), 5) if len(net_r) else None,
        "net_r_sd": round(sd_r, 4),
        "detectable_net_edge_r_at_80pct": round(detectable_edge(len(rows), sd_r), 4),
    }
    out = os.path.join(REPO, RESULT)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(f"  {result['verdict']}: holdout t {pooled.get('t_stat')} on {pooled.get('trades')} trades, "
          f"net {pooled.get('expectancy_points_net')} points ({result['net_r_mean']}R)")
    print(f"  wrote {RESULT} -- commit it; confirm will never run again")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--reports", default=REPORTS)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="overwrite an UNCOMMITTED plan; a committed one is the record")
    for name in ("rehearse", "confirm"):
        sub.add_parser(name).add_argument("--path")
    args = ap.parse_args()
    return {"select": select, "rehearse": rehearse, "confirm": confirm}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
