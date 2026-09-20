#!/usr/bin/env python3
"""Does the positive overnight swap on a long pair survive the spot drift?

`swap_profile.json` records the one number in this account that is positive:
holding AUDUSD LONG earns 4.51 points a night, NZDUSD 1.67, USDCHF 0.08. Every
other side of every other symbol is a charge. A positive financing rate is not
a rule fitted to the data -- it is a term of the contract, known before any
trade -- so it is the one candidate in this repository that does not have to
clear a permutation null to be taken seriously. It has to clear something
harder: the currency has to not fall by more than it pays.

That is the carry trade, and the reason it is not free money is uncovered
interest parity -- the high-rate currency is expected to depreciate by roughly
the differential. The premium that survives is compensation for crash risk:
carry pays a little most of the time and loses a great deal occasionally, so
its mean is the wrong summary and its worst drawdown is the right one.

**THE SWAP RATE IS A SNAPSHOT AND THE HISTORY IS NOT.** MT5 reports what the
broker charges TODAY. AUD-USD policy differential has changed sign inside the
window this tool reads: applying today's +4.51 to a year when the Fed paid
more than the RBA does not approximate that year, it inverts it. So this tool
reports the counterfactual and the spot record SEPARATELY and refuses to
multiply them into a backtest equity curve. What it can establish honestly:

  * what the carry pays per year at TODAY's rate, in points and in percent;
  * what spot actually did over the same window, and its volatility;
  * what the ratio would be IF spot were a martingale -- the optimistic case;
  * the worst peak-to-trough spot move, which is what the carry has to survive.

A verdict of CARRY_EXCEEDS_DRIFT is therefore a statement about a
counterfactual, not a measured return, and the tool says so in its own output.

    python tools/carry_check.py
    python tools/carry_check.py --symbols AUDUSD,NZDUSD --years 10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import choose_spread, connect, fetch_rates

# Reused, never restated. The first version of this file divided the raw swap
# by `trade_tick_value` and skipped the base-to-deposit conversion, which
# reported AUDUSD at 6.3 points a night against the 4.51 in swap_profile.json
# -- a 40% overstatement of the only positive number on the account, in the
# direction that manufactures an edge. swap.py already converts this exactly
# and refuses the modes it cannot convert.
from swap import swap_points_per_night

# Nights billed per calendar year. Swap accrues per server midnight crossed and
# one weekday is billed triple to carry the weekend, so a position held
# continuously is charged on every calendar night, not on trading days only.
NIGHTS_PER_YEAR = 365.0
TRADING_DAYS = 252.0


def spot_stats(closes: np.ndarray, point: float) -> dict:
    """Drift, volatility and worst drawdown of simply holding the pair long."""
    r = np.diff(np.log(closes))
    n = len(r)
    years = n / TRADING_DAYS
    total = float(np.log(closes[-1] / closes[0]))
    peak = np.maximum.accumulate(closes)
    dd = float(np.min(closes / peak) - 1.0)
    return {
        "bars": int(len(closes)),
        "years": round(years, 2),
        "first": float(closes[0]),
        "last": float(closes[-1]),
        "drift_points_per_year": round(
            ((closes[-1] - closes[0]) / point) / years, 1
        ),
        "drift_pct_per_year": round(100.0 * (math.exp(total / years) - 1.0), 2),
        "vol_pct_annual": round(100.0 * float(r.std(ddof=1)) * math.sqrt(TRADING_DAYS), 2),
        "worst_drawdown_pct": round(100.0 * dd, 1),
    }


def carry_stats(long_pts_night: float, price: float, point: float,
                spread_points: float) -> dict:
    """What the financing pays a year, and what it costs to get into it once."""
    per_year_points = long_pts_night * NIGHTS_PER_YEAR
    return {
        "points_per_night": round(long_pts_night, 2),
        "points_per_year": round(per_year_points, 1),
        "pct_per_year": round(100.0 * (per_year_points * point) / price, 2),
        "spread_points": round(spread_points, 1),
        "nights_to_repay_spread": (
            round(spread_points / long_pts_night, 1) if long_pts_night > 0 else None
        ),
    }


def demonstrability(carry: dict, by_window: dict) -> dict:
    """How long the carry would take to separate from zero, and what the
    drift estimate's own error bar is.

    This exists because WINDOW_DEPENDENT on its own is too blunt a verdict.
    The obvious rebuttal to it is correct: if spot is a martingale then the
    drift term has expectation zero, its variation across windows is sampling
    noise rather than evidence, and a contractual carry is positive expected
    value however the history is cut. So the question is not whether the
    windows disagree -- they must -- but whether the carry is large enough to
    ever be told apart from that noise.

    Two numbers settle it.

      * The standard error of a drift estimate over T years at annual
        volatility s is s/sqrt(T). Compare it to the carry. If one standard
        error of the thing that could eat the carry is BIGGER than the carry,
        no window of that length can show the carry surviving.
      * Treating the carry as the whole expected return and spot vol as the
        whole risk gives a Sharpe ratio, and a t-statistic grows as
        Sharpe*sqrt(T). Years to reach t=1.96 is (1.96/Sharpe)^2.

    This is the same arithmetic `cost_hurdle.py` applies to a win rate, in the
    units a hold rather than a trade is measured in.
    """
    vol_pct = float(np.mean([s["vol_pct_annual"] for s in by_window.values()]))
    sharpe = carry["pct_per_year"] / vol_pct if vol_pct else 0.0
    out = {
        "carry_pct_per_year": carry["pct_per_year"],
        "spot_vol_pct_annual": round(vol_pct, 2),
        "sharpe_if_spot_is_a_martingale": round(sharpe, 3),
        "years_to_t_1_96": (
            round((1.96 / sharpe) ** 2, 1) if sharpe > 0 else None
        ),
        "drift_standard_error_by_window": {},
    }
    for w in sorted(by_window):
        se_pct = vol_pct / math.sqrt(w)
        out["drift_standard_error_by_window"][w] = {
            "se_pct_per_year": round(se_pct, 2),
            "carry_over_se": round(carry["pct_per_year"] / se_pct, 2) if se_pct else None,
        }
    return out


def judge(carry: dict, by_window: dict) -> tuple[str, str]:
    """Judge across windows, because one window cannot decide this.

    Measured 2026-09-20 on AUDUSD: the carry term is a constant (it is today's
    contract) while the spot term ran +2,097 points a year over three years,
    -455 over ten and -1,976 over fifteen. Net of carry that is +3,735,
    +1,183 and -338 -- the answer changes SIGN with the window, and nothing in
    a single-window run tells you that. So a verdict is only issued when every
    window agrees, and the disagreement is itself the finding otherwise.
    """
    if carry["points_per_night"] <= 0:
        return "NO_CARRY", "the long side is charged, not paid; there is nothing to test"

    pts_year = carry["points_per_year"]
    nets = {w: pts_year + s["drift_points_per_year"] for w, s in by_window.items()}
    worst_dd = min(s["worst_drawdown_pct"] for s in by_window.values())
    spread = max(nets.values()) - min(nets.values())
    detail_windows = ", ".join(f"{w}y {nets[w]:+,.0f}" for w in sorted(nets))

    if min(nets.values()) <= 0 < max(nets.values()):
        return ("WINDOW_DEPENDENT",
                f"net of carry the result changes sign with the window "
                f"({detail_windows} points a year). The carry term is fixed at "
                f"{pts_year:,.0f} because it is today's contract; the spot term "
                f"moves by {spread:,.0f} points a year depending only on where "
                f"the history is cut. Nothing here is bankable")

    if max(nets.values()) <= 0:
        return ("DRIFT_EATS_CARRY",
                f"spot gave up more than the {pts_year:,.0f} points a year "
                f"earned in every window tested ({detail_windows})")

    # Every window positive. Still the optimistic reading: spot a martingale.
    vol = max(s["vol_pct_annual"] for s in by_window.values())
    sharpe = carry["pct_per_year"] / vol if vol else float("nan")
    return ("CARRY_EXCEEDS_DRIFT",
            f"net positive in every window ({detail_windows} points a year); "
            f"if spot were a martingale the ratio would be {sharpe:.2f} against "
            f"a worst drawdown of {worst_dd:.0f}%. A ratio near 0.2 with a "
            f"drawdown near 30% is the carry risk premium, which is payment "
            f"for crash risk rather than an inefficiency")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="AUDUSD,NZDUSD,USDCHF,EURUSD,GBPUSD")
    ap.add_argument("--windows", default="3,5,10,15,25",
                    help="years of history to cut the spot term over. Several, "
                         "because the verdict changes sign with one window")
    ap.add_argument("--path")
    ap.add_argument("--out")
    args = ap.parse_args()

    mt5 = connect(args.path)
    ai = mt5.account_info()
    if ai is None:
        raise SystemExit("no account info; cannot know the deposit currency")
    deposit = ai.currency

    windows = sorted({int(w) for w in args.windows.split(",") if w.strip()})
    longest = max(windows)
    report: dict = {"windows_years": windows, "deposit_currency": deposit,
                    "by_symbol": {}, "data_gaps": []}

    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        info = mt5.symbol_info(sym)
        if info is None:
            report["data_gaps"].append(f"{sym}: no symbol info")
            continue
        # Fetch once at the longest window and slice; refetching per window
        # would ask the terminal for the same bars five times and, worse,
        # could land on different bar counts between calls.
        rates = fetch_rates(mt5, sym, int(longest * TRADING_DAYS) + 50,
                            timeframe="D1", min_bars=300)
        if rates is None or len(rates) < 300:
            report["data_gaps"].append(f"{sym}: fewer than 300 daily bars")
            continue
        tick = mt5.symbol_info_tick(sym)
        spread_price, spread_note = choose_spread(rates, info, tick, "median")
        point = float(info.point)
        closes = np.asarray(rates["close"], dtype=float)

        long_pts, _short_pts, swap_note = swap_points_per_night(
            info, float(closes[-1]), deposit
        )
        if long_pts is None:
            report["data_gaps"].append(f"{sym}: {swap_note}")
            continue

        by_window = {}
        for w in windows:
            need = int(w * TRADING_DAYS)
            if len(closes) < need:
                report["data_gaps"].append(
                    f"{sym}: only {len(closes)} daily bars, "
                    f"short of the {need} a {w}-year window needs"
                )
                continue
            by_window[w] = spot_stats(closes[-need:], point)
        if not by_window:
            continue

        carry = carry_stats(long_pts, float(closes[-1]), point,
                            spread_price / point)
        verdict, detail = judge(carry, by_window)
        report["by_symbol"][sym] = {
            "carry": carry, "spot_by_window": by_window,
            "demonstrability": demonstrability(carry, by_window),
            "spread_note": spread_note, "verdict": verdict, "detail": detail,
        }

    report["caveat"] = (
        "The swap figure is TODAY's contract; the spot figures are history. "
        "AUD-USD and NZD-USD policy differentials changed sign inside this "
        "window, so the carry column does NOT describe what a position would "
        "have earned over it. Read the two columns as a comparison of scale, "
        "never as a backtest."
    )

    print("\n  carry against spot drift. The carry column is ONE number because "
          "it is\n  today's contract; the spot columns are what changes.")
    print("  " + "-" * 74)
    head = f"  {'sym':8} {'pts/nt':>7} {'carry/yr':>9}"
    for w in windows:
        head += f" {str(w) + 'y net':>9}"
    print(head + f" {'worst DD':>9}")
    for sym, r in report["by_symbol"].items():
        c, bw = r["carry"], r["spot_by_window"]
        line = (f"  {sym:8} {c['points_per_night']:>7} "
                f"{c['points_per_year']:>9,}")
        for w in windows:
            if w in bw:
                line += f" {c['points_per_year'] + bw[w]['drift_points_per_year']:>9,.0f}"
            else:
                line += f" {'-':>9}"
        worst = min(s["worst_drawdown_pct"] for s in bw.values())
        print(line + f" {worst:>8.0f}%")
    print("\n  net = carry + realised spot drift, points per year, before spread.\n")

    print("  can the carry ever be told apart from the drift it has to survive?")
    print("  " + "-" * 74)
    print(f"  {'sym':8} {'carry%/yr':>10} {'vol%':>7} {'Sharpe':>8} "
          f"{'yrs to t=1.96':>14} {'carry/SE(10y)':>14}")
    for sym, r in report["by_symbol"].items():
        d = r["demonstrability"]
        if d["sharpe_if_spot_is_a_martingale"] <= 0:
            continue
        ratio = d["drift_standard_error_by_window"].get(10, {}).get("carry_over_se")
        print(f"  {sym:8} {d['carry_pct_per_year']:>10} "
              f"{d['spot_vol_pct_annual']:>7} "
              f"{d['sharpe_if_spot_is_a_martingale']:>8} "
              f"{d['years_to_t_1_96']:>14} "
              f"{(ratio if ratio is not None else float('nan')):>14}")
    print()
    for sym, r in report["by_symbol"].items():
        print(f"  {sym}: {r['verdict']}")
        print(f"    {r['detail']}")
    print(f"\n  CAVEAT: {report['caveat']}\n")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
        print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
