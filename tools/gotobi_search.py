#!/usr/bin/env python3
"""The gotobi effect: Japanese settlement days into the 10:00 JST Tokyo fix.

Seventeenth search, and the first with an institutional mechanism specific
enough to say in advance which controls must come back empty.

Japanese importers settle on days ending in 5 and 0 -- the 5th, 10th, 15th,
20th, 25th and 30th, called *gotobi* -- and must buy dollars by the Tokyo fix
at **10:00 JST**. The claim (Archer, `Getting Started in Currency Trading`) is
that the resulting excess dollar demand lifts USDJPY into the fix.

**This is not the calendar family that was refuted here.** Day-of-week and
turn-of-month died when Tuesday and Wednesday CONTROLS scored +86.0 and +83.2
against Thursday's +86.6 -- going long anything in that window earned about
86 points, so the "effect" was directional drift seen from inside a grid. The
difference now is that the mechanism names its own boundaries, and every
boundary is a control:

  * **A day control.** Non-gotobi days, same pair, same window. If the effect
    is settlement flow it must not be there.
  * **A PAIR control, which is the decisive one.** The mechanism is Japanese
    importers buying dollars. It says nothing about EURUSD, GBPUSD or AUDUSD,
    so a gotobi effect in those pairs would mean the finding is a calendar
    artefact rather than settlement flow. This is the control the day-of-week
    search lacked and the reason it fooled itself.
  * **An HOUR control.** The same gotobi days over a window that does not
    contain the fix. Settlement flow is supposed to be concentrated, not a
    property of the whole day.
  * **A permutation null**, shuffling which dates carry the label while
    holding the count and the window fixed.

**The clock is the trap here and it is the one this repository keeps
falling into.** MT5 stamps a bar with the SERVER's wall clock rendered as a
UTC epoch, and this broker is UTC+3. JST is UTC+9. So 10:00 JST is 01:00 UTC
is **server hour 4**, and reading `datetime.now()` or the local hour would
put the window in the wrong place entirely and produce a confident null about
the wrong four hours.

**Power, stated before the result.** 405 gotobi dates over 2018-2026. One
standard error of a 4-hour USDJPY return at that sample is roughly 0.0075%,
so an effect must reach about **0.015%** to be measurable and about
**0.007%** -- the spread -- to be worth trading. Those two numbers are close,
which means a real but small effect and a null are not fully separable here.
That is said in advance rather than discovered afterwards.

    python tools/gotobi_search.py
    python tools/gotobi_search.py --window 0 4
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import choose_spread, connect, fetch_rates

# Server hour 4 == 01:00 UTC == 10:00 JST. Derived, not guessed: the broker
# is UTC+3 and JST is UTC+9, so JST = server + 6.
FIX_SERVER_HOUR = 4
GOTOBI_DAYS = (5, 10, 15, 20, 25, 30)
PAIR = "USDJPY"
CONTROL_PAIRS = ("EURUSD", "GBPUSD", "AUDUSD", "USDCHF")


def server_hour(times: np.ndarray) -> np.ndarray:
    return (times // 3600) % 24


def day_of_month(times: np.ndarray) -> np.ndarray:
    return np.array([dt.datetime.fromtimestamp(int(x), dt.UTC).day for x in times])


def date_key(times: np.ndarray) -> np.ndarray:
    return np.array([dt.datetime.fromtimestamp(int(x), dt.UTC).date() for x in times])


def window_returns(times, closes, start_h, end_h):
    """One log return per date, from the close at `start_h` to the close at
    `end_h`, both server hours. A date missing either bar is dropped rather
    than filled -- a filled bar is a price nobody traded."""
    sh = server_hour(times)
    keys = date_key(times)
    out_dates, out_rets = [], []
    a_idx = {k: i for i, (k, h) in enumerate(zip(keys, sh)) if h == start_h}
    b_idx = {k: i for i, (k, h) in enumerate(zip(keys, sh)) if h == end_h}
    for k in sorted(set(a_idx) & set(b_idx)):
        i, j = a_idx[k], b_idx[k]
        if j > i and closes[i] > 0 and closes[j] > 0:
            out_dates.append(k)
            out_rets.append(math.log(closes[j] / closes[i]))
    return np.array(out_dates), np.array(out_rets)


def welch(a: np.ndarray, b: np.ndarray):
    """Difference of means with Welch's t, which does not assume equal
    variance -- gotobi and non-gotobi days need not be equally volatile."""
    if len(a) < 20 or len(b) < 20:
        return float("nan"), float("nan")
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    if va + vb <= 0:
        return float("nan"), float("nan")
    return float(a.mean() - b.mean()), float((a.mean() - b.mean()) / math.sqrt(va + vb))


def arm(times, closes, start_h, end_h, label):
    dates, rets = window_returns(times, closes, start_h, end_h)
    if len(dates) == 0:
        return None
    doms = np.array([d.day for d in dates])
    is_goto = np.isin(doms, GOTOBI_DAYS)
    g, ng = rets[is_goto], rets[~is_goto]
    diff, t = welch(g, ng)
    return {"label": label, "n_gotobi": int(is_goto.sum()),
            "n_other": int((~is_goto).sum()),
            "mean_gotobi_pct": float(g.mean() * 100) if len(g) else float("nan"),
            "mean_other_pct": float(ng.mean() * 100) if len(ng) else float("nan"),
            "diff_pct": diff * 100 if diff == diff else float("nan"),
            "t": t, "rets": rets, "is_goto": is_goto}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", type=int, default=50000)
    ap.add_argument("--window", type=int, nargs=2, default=[0, FIX_SERVER_HOUR],
                    help="server hours, start and end (4 == 10:00 JST)")
    ap.add_argument("--null-rounds", type=int, default=2000)
    ap.add_argument("--out", default="reports/gotobi_search.json")
    args = ap.parse_args()
    s_h, e_h = args.window

    mt5 = connect()
    rates = fetch_rates(mt5, PAIR, args.bars, timeframe="H1")
    info, tick = mt5.symbol_info(PAIR), mt5.symbol_info_tick(PAIR)
    spread, _note = choose_spread(rates, info, tick, source="median")
    cost_pct = float(spread) / float(np.median(rates["close"])) * 100
    controls = {}
    for sym in CONTROL_PAIRS:
        cr = fetch_rates(mt5, sym, args.bars, timeframe="H1")
        if cr is not None and len(cr) > 5000:
            controls[sym] = cr
    mt5.shutdown()

    t0 = dt.datetime.fromtimestamp(int(rates["time"][0]), dt.UTC).date()
    t1 = dt.datetime.fromtimestamp(int(rates["time"][-1]), dt.UTC).date()
    print(f"\n  gotobi: {PAIR} server hours {s_h}->{e_h} "
          f"(hour {FIX_SERVER_HOUR} == 10:00 JST), {t0} to {t1}")
    print(f"  one round trip costs about {cost_pct * 2:.4f}% of price")

    main_arm = arm(rates["time"], rates["close"].astype(float), s_h, e_h,
                   f"{PAIR} {s_h}->{e_h} (THE FIX WINDOW)")
    rows = [main_arm]
    # HOUR control: the same days over a window with no fix in it.
    rows.append(arm(rates["time"], rates["close"].astype(float), 12, 16,
                    f"{PAIR} 12->16 (hour control)"))
    # PAIR controls: the mechanism does not mention these.
    for sym, cr in controls.items():
        rows.append(arm(cr["time"], cr["close"].astype(float), s_h, e_h,
                        f"{sym} {s_h}->{e_h} (pair control)"))

    print("  " + "-" * 78)
    print(f"  {'arm':38}{'n gotobi':>10}{'gotobi %':>10}{'other %':>10}"
          f"{'diff %':>9}{'t':>7}")
    for r in rows:
        if r is None:
            continue
        print(f"  {r['label']:38}{r['n_gotobi']:>10,}{r['mean_gotobi_pct']:>+10.4f}"
              f"{r['mean_other_pct']:>+10.4f}{r['diff_pct']:>+9.4f}{r['t']:>+7.2f}")

    # Permutation null: shuffle WHICH dates carry the label, holding the count
    # and the window fixed. Anything the real labelling earns over this is
    # about the dates and not about the window.
    rng = np.random.default_rng(20260923)
    rets, is_goto = main_arm["rets"], main_arm["is_goto"]
    k = int(is_goto.sum())
    null_t = []
    for _ in range(args.null_rounds):
        idx = rng.permutation(len(rets))
        pick = np.zeros(len(rets), dtype=bool)
        pick[idx[:k]] = True
        _d, tt = welch(rets[pick], rets[~pick])
        if tt == tt:
            null_t.append(tt)
    null_t = np.array(null_t)
    real_t = main_arm["t"]
    p_two = float((np.abs(null_t) >= abs(real_t)).mean())
    print("  " + "-" * 78)
    print(f"  permutation null over {len(null_t):,} relabellings: "
          f"|t| >= {abs(real_t):.2f} in {p_two * 100:.1f}% of them")
    print(f"  null |t| reaches {np.abs(null_t).max():.2f}, "
          f"95th percentile {np.percentile(np.abs(null_t), 95):.2f}")
    # Bonferroni over the arms actually judged.
    judged = sum(1 for r in rows if r is not None)
    print(f"  Bonferroni over {judged} arms: p must beat {0.05 / judged:.4f}")
    print(f"\n  the measured difference is {main_arm['diff_pct']:+.4f}% against a "
          f"round-trip cost of {cost_pct * 2:.4f}%")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"arms": [{k2: v for k2, v in r.items()
                             if k2 not in ("rets", "is_goto")}
                            for r in rows if r is not None],
                   "null": {"rounds": len(null_t), "p_two_sided": p_two,
                            "max_abs_t": float(np.abs(null_t).max()),
                            "p95_abs_t": float(np.percentile(np.abs(null_t), 95))},
                   "cost_round_trip_pct": cost_pct * 2,
                   "config": {"window_server_hours": [s_h, e_h],
                              "fix_server_hour": FIX_SERVER_HOUR,
                              "gotobi_days": list(GOTOBI_DAYS),
                              "from": str(t0), "to": str(t1),
                              "bonferroni_arms": judged}}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
