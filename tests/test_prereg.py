#!/usr/bin/env python
"""The pre-registration can only be trusted if its locks actually lock.

`prereg.py confirm` spends a holdout that exists once. Every check here guards
one way that could go wrong without looking wrong: a selection rule that
ranks on the wrong half, a decision rule that lets t = 1.96 exactly through
(this project's recurring tie bug), a holdout end taken from a D1 report, a
guard that misses a dirty file, a changed hash, or a result deleted from disk
but still in git history. No MT5 is needed; the git checks run in a temporary
repository.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from prereg import (  # noqa: E402
    REPRO_TRADES_TOL, Z_PRIMARY, decide, detectable_edge, eligible, guard,
    holdout_end, pool_candidates, power_at, r_points, reproduces, select_candidate,
    sha256,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:62s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def cand(name, is_t, is_net, oos_t, oos_net, n_is=500, n_oos=200):
    return {"name": name, "is_t": is_t, "is_net": is_net, "oos_t": oos_t,
            "oos_net": oos_net, "is_trades": n_is, "oos_trades": n_oos}


print()
print("Selection: both halves positive, ranked on the WEAKER half")
pool = [cand("strong_is_weak_oos", 3.0, 9.0, 0.2, 1.0),
        cand("balanced", 1.1, 5.0, 1.0, 4.0),
        cand("negative_oos", 4.0, 12.0, -0.5, -2.0),
        cand("zero_is", 0.0, 0.0, 2.5, 8.0)]
chosen, ranked = select_candidate(pool)
check("a negative half is ineligible", eligible(pool[2]), False)
check("a net of exactly zero is ineligible", eligible(pool[3]), False)
check("the weaker half decides, not the stronger", chosen["name"], "balanced")
check("only the two eligible are ranked", [c["name"] for c in ranked],
      ["balanced", "strong_is_weak_oos"])
tie = [cand("fewer", 1.0, 3.0, 1.5, 3.0, 300, 100), cand("more", 1.5, 3.0, 1.0, 3.0, 900, 300)]
check("an exact tie on min t goes to more trades", select_candidate(tie)[0]["name"], "more")
check("nothing eligible selects nothing", select_candidate([pool[2]])[0], None)

print()
print("The bracket in points does not depend on the spread")
check("win 210 / loss -230 is R = 220", r_points([{"avg_win_points": 210.0,
      "avg_loss_points": -230.0, "trades": 10}]), 220.0)
check("views are weighted by trades",
      r_points([{"avg_win_points": 100.0, "avg_loss_points": -100.0, "trades": 3},
                {"avg_win_points": 200.0, "avg_loss_points": -200.0, "trades": 1}]), 125.0)

print()
print("Power: the closed form, and the solver's own identity")
check("n = 10,000 detects (1.96 + 0.8416)/100", round(detectable_edge(10_000), 6),
      round((1.96 + 0.8416212335729143) / 100, 6))
check("power at the detectable edge is 0.80", round(power_at(detectable_edge(1766), 1766), 6), 0.8)
check("no trades detects nothing", detectable_edge(0), float("inf"))

print()
print("The decision rule, at its boundaries")
ok_date = {"date_cluster_significant": True, "t_stat_clustered_by_date": 2.5, "signs_disagree": False}
eras3 = [{"trades": 50, "expectancy_points_net": v} for v in (3.0, 1.0, -2.0, 4.0)]
check("t of exactly 1.96 FAILS the primary",
      decide({"t_stat": Z_PRIMARY, "expectancy_points_net": 5.0}, ok_date, eras3)["verdict"],
      "NOT CONFIRMED")
check("t just above, date and 3 of 4 eras: CONFIRMED",
      decide({"t_stat": 1.97, "expectancy_points_net": 5.0}, ok_date, eras3)["verdict"],
      "CONFIRMED")
eras2 = [{"trades": 50, "expectancy_points_net": v} for v in (3.0, 0.0, -2.0, 4.0)]
check("an era at exactly zero is not positive: PRIMARY ONLY",
      decide({"t_stat": 3.0, "expectancy_points_net": 5.0}, ok_date, eras2)["verdict"],
      "PRIMARY ONLY")
empty = [{"trades": 0, "expectancy_points_net": None}] + eras3[:3]
check("an era with no trades does not count as positive",
      decide({"t_stat": 3.0, "expectancy_points_net": 5.0}, ok_date, empty)["eras_positive"], 2)
check("disagreeing signs under clustering: PRIMARY ONLY",
      decide({"t_stat": 3.0, "expectancy_points_net": 5.0},
             dict(ok_date, signs_disagree=True), eras3)["verdict"], "PRIMARY ONLY")
check("a negative clustered t: PRIMARY ONLY",
      decide({"t_stat": 3.0, "expectancy_points_net": 5.0},
             dict(ok_date, t_stat_clustered_by_date=-2.5), eras3)["verdict"], "PRIMARY ONLY")

print()
print("The reproduction gate stops a drifted computation before the holdout")
want = {"trades": 1000, "net": 10.0}
check("exactly at the trade tolerance passes",
      reproduces(want, int(1000 * (1 + REPRO_TRADES_TOL)), 10.0)[0], True)
check("one trade past it fails", reproduces(want, int(1000 * (1 + REPRO_TRADES_TOL)) + 1, 10.0)[0],
      False)
check("a sign flip fails however close", reproduces(want, 1000, -0.5)[0], False)
check("a net of exactly zero fails", reproduces(want, 1000, 0.0)[0], False)
check("net 25% off passes, 26% fails",
      (reproduces(want, 1000, 12.5)[0], reproduces(want, 1000, 12.6)[0]), (True, False))

with tempfile.TemporaryDirectory() as tmp:
    print()
    print("The holdout end comes from H1 reports only, and the earliest date in them")
    json.dump({"timeframe": "H1", "window": {"from": "2023-07-10T00:00:00"},
               "note": "an era began 2023-06-05"}, open(os.path.join(tmp, "a.json"), "w"))
    json.dump({"timeframe": "D1", "window": {"from": "2016-08-25T00:00:00"}},
              open(os.path.join(tmp, "b.json"), "w"))
    check("a D1 report's earlier date is ignored; any H1 date counts",
          holdout_end(tmp), "2023-06-05T00:00:00")

    print()
    print("The pool reads only judged candidates")
    rep = {"window": {"from": "2023-07-10T00:00:00", "to": "2026-09-18T23:00:00",
                      "bars_per_symbol": 19855},
           "candidates": [
               {"name": "judged", "family": "f",
                "in_sample": {"trades": 100, "t_stat": 1.0, "expectancy_points_net": 2.0,
                              "avg_win_points": 200.0, "avg_loss_points": -210.0},
                "out_of_sample": {"trades": 40, "t_stat": 0.5, "expectancy_points_net": 1.0,
                                  "avg_win_points": 180.0, "avg_loss_points": -190.0}},
               {"name": "too_few", "family": "f",
                "in_sample": {"trades": 99, "t_stat": 9.0, "expectancy_points_net": 50.0},
                "out_of_sample": {"trades": 40, "t_stat": 9.0, "expectancy_points_net": 50.0}}]}
    json.dump(rep, open(os.path.join(tmp, "r.json"), "w"))
    got = pool_candidates(tmp, (("r.json", "m", "b"),))
    check("99 in-sample trades is not judged, 100 is", [c["name"] for c in got], ["judged"])

with tempfile.TemporaryDirectory() as repo:
    print()
    print("The guard: committed, clean, unchanged, and never run before")

    def git(*a):
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *a], check=True, capture_output=True)

    git("init", "-q")
    os.makedirs(os.path.join(repo, "tools"))
    os.makedirs(os.path.join(repo, "prereg"))
    code = os.path.join(repo, "tools", "x.py")
    open(code, "w").write("print(1)\n")
    plan = {"analysis_sha256": {"tools/x.py": sha256(code)}}
    json.dump(plan, open(os.path.join(repo, "prereg", "plan.json"), "w"))
    check("an uncommitted plan is refused",
          any("not committed" in p for p in guard(repo, plan)), True)
    git("add", "-A")
    git("commit", "-q", "-m", "plan")
    check("committed and clean: no objection", guard(repo, plan), [])

    open(code, "w").write("print(2)\n")
    probs = guard(repo, plan)
    check("an edited analysis file is refused as dirty", any("uncommitted" in p for p in probs), True)
    check("and as changed from the plan's hash", any("changed since" in p for p in probs), True)
    git("commit", "-q", "-am", "edit after registering")
    check("committing the edit does not launder it",
          any("changed since" in p for p in guard(repo, plan)), True)
    open(code, "w").write("print(1)\n")
    git("commit", "-q", "-am", "restore")
    check("restoring the registered code clears it", guard(repo, plan), [])

    res = os.path.join(repo, "prereg", "result.json")
    open(res, "w").write("{}")
    check("a result on disk refuses a second run",
          any("already been run" in p for p in guard(repo, plan)), True)
    git("add", "-A")
    git("commit", "-q", "-m", "result")
    git("rm", "-q", "prereg/result.json")
    git("commit", "-q", "-m", "delete the result")
    check("deleting it does not reopen the test: history remembers",
          any("git history" in p for p in guard(repo, plan)), True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall pre-registration checks passed")
