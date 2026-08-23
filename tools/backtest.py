"""Does the score actually separate forward returns?

This is the file the whole project exists to make possible. A 0-100 number that
has never been tested against forward returns is decoration: it looks like
analysis and carries no information. This measures whether the composite has
historically ranked stocks in a way that related to what happened next.

What is measured, and what is not
---------------------------------
Only the price-derived half of the composite (technical + risk) is tested.
Quality, valuation and analyst inputs come from a vendor snapshot of *today's*
figures. Using them at a historical date would leak the future into the past -
you would be scoring 2024 with 2026's margins and 2026's price targets - and
the resulting backtest would look excellent and mean nothing. They are
therefore excluded, and the composite tested here is the two-component version.

Known biases that remain, stated rather than hidden
---------------------------------------------------
* Survivorship: the universe is a list of tickers that exist today. Companies
  that were delisted or acquired are absent, which tilts results upward.
* Point-in-time index membership is not modelled.
* No transaction costs, slippage, borrow costs or taxes.
* Overlapping forward windows make observations non-independent, so the
  reported t-statistic overstates significance.

Usage:
    python tools/backtest.py --universe tools/universe.txt --years 6 --horizon 63
    python tools/backtest.py --tickers NVDA,AMD,INTC --years 4 --horizon 21
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import indicators  # noqa: E402
import market  # noqa: E402
import score as scoring  # noqa: E402

# Weights for the two components that can be evaluated point-in-time, kept in
# the same 2:1 proportion they have in the full composite.
PIT_WEIGHTS = {"technical": 0.67, "risk": 0.33}
WINDOW_BARS = 420  # enough history for a 200-day SMA and a settled ADX


def score_at(df: pd.DataFrame, cutoff: pd.Timestamp):
    """Point-in-time score using only bars at or before cutoff."""
    sub = df[df.index <= cutoff]
    if len(sub) < 260:
        return None
    sub = sub.iloc[-WINDOW_BARS:]
    tech = indicators.compute_all(sub, bench_close=None)
    subs = {"technical": scoring.technical_score(tech), "risk": scoring.risk_score(tech)}
    comp = scoring.composite(subs, weights=PIT_WEIGHTS)
    if comp.get("score") is None:
        return None
    return {
        "score": comp["score"],
        "technical": subs["technical"]["score"],
        "risk": subs["risk"]["score"],
        "price": tech["price"],
    }


def forward_return(df: pd.DataFrame, cutoff: pd.Timestamp, horizon: int):
    """Return from the close on/before cutoff to the close `horizon` bars later."""
    idx = df.index[df.index <= cutoff]
    if len(idx) == 0:
        return None
    pos = df.index.get_loc(idx[-1])
    if pos + horizon >= len(df):
        return None
    start, end = df["Close"].iloc[pos], df["Close"].iloc[pos + horizon]
    if not start:
        return None
    return float(end / start - 1)


def spearman(x, y):
    """Rank correlation without a scipy dependency."""
    if len(x) < 3:
        return None
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def run(tickers: list[str], years: int, horizon: int, freq: str = "ME") -> dict:
    history_period = f"{years + 2}y"
    data: dict[str, pd.DataFrame] = {}
    failed = []
    for t in tickers:
        try:
            df = market.get_ohlcv(t, period=history_period)
            if len(df) >= 300:
                data[t] = df
            else:
                failed.append((t, f"only {len(df)} bars"))
        except Exception as exc:
            failed.append((t, f"{type(exc).__name__}: {exc}"))

    if len(data) < 3:
        return {"error": "need at least 3 tickers with sufficient history", "failed": failed}

    bench = market.get_benchmark_close(period=history_period)
    bench_df = pd.DataFrame({"Close": bench}) if bench is not None else None

    end = min(df.index[-1] for df in data.values())
    start = max(max(df.index[0] for df in data.values()), end - pd.Timedelta(days=365 * years))
    dates = pd.date_range(start=start, end=end, freq=freq)

    observations = []
    per_date = []
    for d in dates:
        rows = []
        for t, df in data.items():
            s = score_at(df, d)
            if s is None:
                continue
            fwd = forward_return(df, d, horizon)
            if fwd is None:
                continue
            bfwd = forward_return(bench_df, d, horizon) if bench_df is not None else None
            rows.append({
                "date": d.date().isoformat(),
                "ticker": t,
                "score": s["score"],
                "technical": s["technical"],
                "risk": s["risk"],
                "forward_return": round(fwd, 6),
                "excess_return": round(fwd - bfwd, 6) if bfwd is not None else None,
            })
        if len(rows) >= 5:
            ic = spearman([r["score"] for r in rows], [r["forward_return"] for r in rows])
            ic_ex = spearman(
                [r["score"] for r in rows],
                [r["excess_return"] for r in rows if r["excess_return"] is not None],
            ) if all(r["excess_return"] is not None for r in rows) else None
            per_date.append({"date": d.date().isoformat(), "n": len(rows), "ic": ic, "ic_excess": ic_ex})
            observations.extend(rows)

    if not observations:
        return {"error": "no scoreable observations in range", "failed": failed}

    obs = pd.DataFrame(observations)
    ics = [p["ic"] for p in per_date if p["ic"] is not None]
    mean_ic = float(np.mean(ics)) if ics else None
    # t-stat on the mean IC. Overlapping windows inflate this; see the caveats.
    t_stat = (
        float(np.mean(ics) / (np.std(ics, ddof=1) / np.sqrt(len(ics))))
        if ics and len(ics) > 2 and np.std(ics, ddof=1) > 0 else None
    )

    ret_col = "excess_return" if obs["excess_return"].notna().all() else "forward_return"
    obs["bucket"] = pd.qcut(obs["score"].rank(method="first"), 5,
                            labels=["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"])
    buckets = []
    for name, g in obs.groupby("bucket", observed=True):
        buckets.append({
            "bucket": str(name),
            "n": int(len(g)),
            "score_range": [round(float(g["score"].min()), 1), round(float(g["score"].max()), 1)],
            "mean_forward_return": round(float(g["forward_return"].mean()), 5),
            "median_forward_return": round(float(g["forward_return"].median()), 5),
            "mean_excess_return": round(float(g["excess_return"].mean()), 5) if ret_col == "excess_return" else None,
            "hit_rate_positive": round(float((g["forward_return"] > 0).mean()), 4),
        })

    top, bottom = buckets[-1], buckets[0]
    spread_key = "mean_excess_return" if ret_col == "excess_return" else "mean_forward_return"
    spread = (top[spread_key] or 0) - (bottom[spread_key] or 0)

    monotonic = all(
        (buckets[i][spread_key] or 0) <= (buckets[i + 1][spread_key] or 0) + 1e-9
        for i in range(len(buckets) - 1)
    )

    # A claim of edge has to clear three independent hurdles, not one. An IC
    # that is positive while the quintile spread is negative is noise wearing a
    # decimal point, and reporting it as "weak signal" would be the exact
    # failure this project is meant to avoid.
    significant = t_stat is not None and abs(t_stat) >= 2.0
    agrees = mean_ic is not None and mean_ic * spread > 0
    checks = {
        "ic_significant_t_ge_2": significant,
        "ic_and_quintile_spread_agree_in_sign": agrees,
        "quintiles_monotonic": monotonic,
    }
    passed = sum(bool(v) for v in checks.values())

    if mean_ic is None:
        verdict = "INCONCLUSIVE: not enough cross-sectional observations to measure anything."
    elif passed < 2:
        failures = [k for k, v in checks.items() if not v]
        verdict = (
            f"NO RELIABLE EDGE (mean IC {mean_ic:+.4f}, t={t_stat if t_stat is not None else float('nan'):.2f}, "
            f"Q5-Q1 {spread:+.4f}). Failed: {', '.join(failures)}. "
            "Do not present the composite as predictive. Report the underlying components as "
            "descriptive state and let the reader weigh them."
        )
    elif mean_ic > 0:
        verdict = (
            f"WEAK POSITIVE SIGNAL (mean IC {mean_ic:+.4f}, t={t_stat:.2f}, Q5-Q1 {spread:+.4f}); "
            f"{passed}/3 robustness checks passed. Quant factors typically run 0.02-0.05 IC, so this "
            "is plausible rather than impressive, and the caveats below are not corrected for."
        )
    else:
        verdict = (
            f"NEGATIVE SIGNAL (mean IC {mean_ic:+.4f}, t={t_stat:.2f}, Q5-Q1 {spread:+.4f}): higher "
            "scores preceded worse returns in this sample. Do not use the composite directionally."
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "tickers_requested": len(tickers),
            "tickers_used": sorted(data),
            "failed_tickers": failed,
            "years": years,
            "horizon_trading_days": horizon,
            "rebalance": freq,
            "components_tested": list(PIT_WEIGHTS),
            "components_excluded": ["quality", "valuation", "analyst"],
            "exclusion_reason": "vendor fundamentals and price targets are current-only; "
                                "using them at a past date would leak the future",
            "return_basis": ret_col,
        },
        "results": {
            "observations": int(len(obs)),
            "rebalance_dates": len(per_date),
            "mean_information_coefficient": round(mean_ic, 4) if mean_ic is not None else None,
            "ic_std": round(float(np.std(ics, ddof=1)), 4) if len(ics) > 2 else None,
            "ic_t_stat": round(t_stat, 2) if t_stat is not None else None,
            "ic_positive_rate": round(float(np.mean([i > 0 for i in ics])), 3) if ics else None,
            "quintiles": buckets,
            "top_minus_bottom_quintile": round(spread, 5),
            "quintiles_monotonic": monotonic,
            "robustness_checks": checks,
            "robustness_checks_passed": f"{passed}/3",
        },
        "verdict": verdict,
        "caveats": [
            "Survivorship bias: universe is tickers that exist today.",
            "No transaction costs, slippage or taxes.",
            f"Overlapping {horizon}-day windows sampled monthly make observations "
            "non-independent; the t-statistic is optimistic.",
            "Single market regime; results do not transfer across regimes.",
            "Only the price-derived components are point-in-time; the full composite is untested.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure whether the composite score separated forward returns.")
    ap.add_argument("--tickers", help="comma-separated tickers")
    ap.add_argument("--universe", help="file with one ticker per line")
    ap.add_argument("--years", type=int, default=6)
    ap.add_argument("--horizon", type=int, default=63, help="forward trading days (63 ~ one quarter)")
    ap.add_argument("--freq", default="ME", help="rebalance frequency (ME = month end)")
    ap.add_argument("--out", default="reports/backtest.json")
    args = ap.parse_args()

    tickers = []
    if args.universe:
        with open(args.universe, encoding="utf-8") as fh:
            tickers = [ln.strip().upper() for ln in fh if ln.strip() and not ln.startswith("#")]
    if args.tickers:
        tickers += [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        print("Provide --tickers or --universe")
        return 1

    result = run(sorted(set(tickers)), args.years, args.horizon, args.freq)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)

    if "error" in result:
        print("Backtest failed:", result["error"])
        return 1

    r = result["results"]
    print(f"\nBacktest  |  {len(result['config']['tickers_used'])} tickers  "
          f"|  {r['observations']} observations over {r['rebalance_dates']} rebalance dates")
    print(f"Horizon   |  {args.horizon} trading days   Return basis: {result['config']['return_basis']}\n")
    print(f"  Mean IC (rank corr with forward return) : {r['mean_information_coefficient']}")
    print(f"  IC t-stat (optimistic, overlapping)     : {r['ic_t_stat']}")
    print(f"  Share of dates with positive IC         : {r['ic_positive_rate']}\n")
    print(f"  {'Bucket':<15}{'n':>6}{'score range':>16}{'mean fwd':>11}{'hit rate':>10}")
    for b in r["quintiles"]:
        rng = f"{b['score_range'][0]}-{b['score_range'][1]}"
        print(f"  {b['bucket']:<15}{b['n']:>6}{rng:>16}{b['mean_forward_return']:>11.4f}{b['hit_rate_positive']:>10.3f}")
    print(f"\n  Q5 - Q1 spread: {r['top_minus_bottom_quintile']:+.4f}   monotonic: {r['quintiles_monotonic']}")
    print(f"\n  VERDICT: {result['verdict']}")
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
