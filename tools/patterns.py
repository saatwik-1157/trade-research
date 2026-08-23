"""Event study over candlestick patterns, calendar effects and price cycles.

The question this answers is not "which patterns appear" - any of them can be
detected trivially - but "does anything measurably follow them". Those are
different questions, and only the second one matters.

Why multiple-testing correction is the centre of this file
----------------------------------------------------------
Testing 20 patterns across 3 horizons is 60 hypotheses. At the conventional 5%
threshold, three of them are expected to look significant when nothing is
happening at all. Pattern-mining literature is largely a record of that effect,
and reporting the winners without the correction is how noise becomes a
"strategy". Every p-value here therefore goes through Benjamini-Hochberg FDR
control, and the raw and corrected verdicts are printed side by side so the
difference is visible rather than assumed.

Overlapping forward windows are a second problem: a 10-day forward return
sampled daily reuses each day's move ten times, so observations are not
independent and the naive t-statistic is inflated. Horizon 1 is clean; longer
horizons carry the caveat and should be read as indicative only.

Usage:
    python tools/patterns.py --universe tools/universe.txt --years 8
    python tools/patterns.py --tickers NVDA,AMD,TSLA --years 5
    python tools/patterns.py --tickers SPY --years 15 --cycles
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import indicators  # noqa: E402
import market  # noqa: E402

HORIZONS = (1, 5, 10)

# Patterns defined only on the closing series survive a source with synthetic
# opens; everything else reads the candle body or its shadows and cannot.
CLOSE_ONLY = {
    "three_up_days", "three_down_days", "rsi_cross_below_30", "rsi_cross_above_70",
    "cross_above_sma200", "cross_below_sma200", "golden_cross", "death_cross",
    "new_20d_high", "new_20d_low", "bollinger_break_up", "bollinger_break_down",
}
FDR_Q = 0.10  # false discovery rate we are willing to tolerate


# ---------------------------------------------------------------- patterns
# Each returns a boolean Series aligned to df.index: True where the pattern
# completes on that bar. Signals are evaluated on the bar's close, and forward
# returns start from that close, so nothing here can peek at the future.

def _body(df):
    return (df["Close"] - df["Open"]).abs()


def _range(df):
    return (df["High"] - df["Low"]).replace(0, np.nan)


def bullish_engulfing(df):
    prev_down = df["Close"].shift(1) < df["Open"].shift(1)
    now_up = df["Close"] > df["Open"]
    engulf = (df["Open"] <= df["Close"].shift(1)) & (df["Close"] >= df["Open"].shift(1))
    return prev_down & now_up & engulf


def bearish_engulfing(df):
    prev_up = df["Close"].shift(1) > df["Open"].shift(1)
    now_down = df["Close"] < df["Open"]
    engulf = (df["Open"] >= df["Close"].shift(1)) & (df["Close"] <= df["Open"].shift(1))
    return prev_up & now_down & engulf


def hammer(df):
    lower = df[["Open", "Close"]].min(axis=1) - df["Low"]
    upper = df["High"] - df[["Open", "Close"]].max(axis=1)
    return (lower > 2 * _body(df)) & (upper < _body(df)) & (_range(df) > 0)


def shooting_star(df):
    lower = df[["Open", "Close"]].min(axis=1) - df["Low"]
    upper = df["High"] - df[["Open", "Close"]].max(axis=1)
    return (upper > 2 * _body(df)) & (lower < _body(df)) & (_range(df) > 0)


def doji(df):
    return _body(df) < 0.1 * _range(df)


def inside_bar(df):
    return (df["High"] < df["High"].shift(1)) & (df["Low"] > df["Low"].shift(1))


def outside_bar(df):
    return (df["High"] > df["High"].shift(1)) & (df["Low"] < df["Low"].shift(1))


def nr7(df):
    r = df["High"] - df["Low"]
    return r == r.rolling(7).min()


def gap_up(df):
    return df["Open"] > df["High"].shift(1)


def gap_down(df):
    return df["Open"] < df["Low"].shift(1)


def three_up(df):
    up = df["Close"] > df["Close"].shift(1)
    return up & up.shift(1) & up.shift(2)


def three_down(df):
    dn = df["Close"] < df["Close"].shift(1)
    return dn & dn.shift(1) & dn.shift(2)


def _rsi_series(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def rsi_oversold(df):
    r = _rsi_series(df["Close"])
    return (r < 30) & (r.shift(1) >= 30)


def rsi_overbought(df):
    r = _rsi_series(df["Close"])
    return (r > 70) & (r.shift(1) <= 70)


def cross_above_sma200(df):
    s = df["Close"].rolling(200).mean()
    return (df["Close"] > s) & (df["Close"].shift(1) <= s.shift(1))


def cross_below_sma200(df):
    s = df["Close"].rolling(200).mean()
    return (df["Close"] < s) & (df["Close"].shift(1) >= s.shift(1))


def golden_cross(df):
    f, s = df["Close"].rolling(50).mean(), df["Close"].rolling(200).mean()
    return (f > s) & (f.shift(1) <= s.shift(1))


def death_cross(df):
    f, s = df["Close"].rolling(50).mean(), df["Close"].rolling(200).mean()
    return (f < s) & (f.shift(1) >= s.shift(1))


def new_20d_high(df):
    return df["Close"] >= df["Close"].rolling(20).max()


def new_20d_low(df):
    return df["Close"] <= df["Close"].rolling(20).min()


def bollinger_break_up(df):
    m = df["Close"].rolling(20).mean()
    sd = df["Close"].rolling(20).std(ddof=0)
    return df["Close"] > m + 2 * sd


def bollinger_break_down(df):
    m = df["Close"].rolling(20).mean()
    sd = df["Close"].rolling(20).std(ddof=0)
    return df["Close"] < m - 2 * sd


def volume_spike_up(df):
    v = df["Volume"] > 2 * df["Volume"].rolling(20).mean()
    return v & (df["Close"] > df["Open"])


PATTERNS = {
    "bullish_engulfing": bullish_engulfing,
    "bearish_engulfing": bearish_engulfing,
    "hammer": hammer,
    "shooting_star": shooting_star,
    "doji": doji,
    "inside_bar": inside_bar,
    "outside_bar": outside_bar,
    "nr7": nr7,
    "gap_up": gap_up,
    "gap_down": gap_down,
    "three_up_days": three_up,
    "three_down_days": three_down,
    "rsi_cross_below_30": rsi_oversold,
    "rsi_cross_above_70": rsi_overbought,
    "cross_above_sma200": cross_above_sma200,
    "cross_below_sma200": cross_below_sma200,
    "golden_cross": golden_cross,
    "death_cross": death_cross,
    "new_20d_high": new_20d_high,
    "new_20d_low": new_20d_low,
    "bollinger_break_up": bollinger_break_up,
    "bollinger_break_down": bollinger_break_down,
    "volume_spike_up": volume_spike_up,
}


# ---------------------------------------------------------------- statistics

def benjamini_hochberg(pvalues: list[float], q: float = FDR_Q) -> list[bool]:
    """Which hypotheses survive FDR control at level q."""
    n = len(pvalues)
    if not n:
        return []
    order = sorted(range(n), key=lambda i: pvalues[i])
    survive = [False] * n
    threshold_rank = -1
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= q * rank / n:
            threshold_rank = rank
    for rank, idx in enumerate(order, start=1):
        if rank <= threshold_rank:
            survive[idx] = True
    return survive


def _norm_sf(z: float) -> float:
    """Two-sided p-value from a z score, via the error function."""
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))


def event_study(samples: list[tuple[str, float]], baseline_mean: float) -> dict:
    """Mean excess forward return after an event, with date-clustered inference.

    The naive t-statistic treats every (ticker, date) observation as
    independent. It is not: large caps are highly correlated, so 35 names
    signalling on the same day is closer to one observation than to 35, and
    market-wide events such as a death cross fire on nearly every name at once.
    Left uncorrected this inflates t by roughly the square root of the number of
    names, which manufactures significance out of a single shared market move.

    Both are reported - the naive figure because it is what a naive tool would
    print, and the clustered one because it is the number to believe.
    """
    n = len(samples)
    if n < 20:
        return {"n": n, "insufficient": True}

    arr = np.asarray([v for _, v in samples], dtype=float)
    excess = arr - baseline_mean
    mean = float(excess.mean())
    sd = float(excess.std(ddof=1))
    if sd == 0:
        return {"n": n, "insufficient": True}

    naive_t = mean / (sd / math.sqrt(n))

    # Collapse to one observation per calendar date, then test across dates.
    by_date: dict[str, list[float]] = {}
    for d, v in samples:
        by_date.setdefault(d, []).append(v - baseline_mean)
    date_means = np.asarray([np.mean(v) for v in by_date.values()], dtype=float)
    n_dates = len(date_means)

    if n_dates < 20 or date_means.std(ddof=1) == 0:
        return {
            "n": n, "n_dates": n_dates,
            "mean_excess_return": round(mean, 6),
            "naive_t_stat": round(naive_t, 3),
            "insufficient": True,
            "reason": "too few distinct dates for clustered inference",
        }

    clustered_t = float(date_means.mean() / (date_means.std(ddof=1) / math.sqrt(n_dates)))

    # Two means, and they can disagree in sign. The pooled mean weights every
    # observation equally, so dates where many names signal at once dominate it;
    # the per-date mean weights every trading day equally. When they diverge,
    # the apparent edge lives in a handful of crowded dates - a market-wide move
    # counted many times - rather than in the pattern itself. That divergence is
    # the finding, so both are reported rather than one being chosen.
    per_date_mean = float(date_means.mean())
    return {
        "n": n,
        "n_dates": n_dates,
        "obs_per_date": round(n / n_dates, 2),
        "mean_excess_return": round(mean, 6),
        "mean_excess_per_date": round(per_date_mean, 6),
        "signs_disagree": bool(mean * per_date_mean < 0),
        "naive_t_stat": round(naive_t, 3),
        "t_stat": round(clustered_t, 3),
        "p_value": round(_norm_sf(clustered_t), 5),
        "hit_rate": round(float((arr > 0).mean()), 4),
        "insufficient": False,
    }


def run(tickers: list[str], years: int) -> dict:
    period = f"{years}y"
    loaded, failed = {}, []
    for t in tickers:
        try:
            df = market.get_ohlcv(t, period=period)
            if len(df) >= 300:
                loaded[t] = df
            else:
                failed.append((t, f"only {len(df)} bars"))
        except Exception as exc:
            failed.append((t, f"{type(exc).__name__}: {exc}"))

    if not loaded:
        return {"error": "no usable price history", "failed": failed}

    # Pooled across tickers: one name gives far too few pattern occurrences to
    # say anything, and pooling is the only way to reach a usable sample.
    pooled: dict[tuple[str, int], list[tuple[str, float]]] = {}
    baseline: dict[int, list[float]] = {h: [] for h in HORIZONS}

    skipped_ohlc = []
    for t, df in loaded.items():
        quality = market.VALIDATION.get(t, {})
        ohlc_ok = quality.get("ohlc_trustworthy", True)
        if not ohlc_ok:
            skipped_ohlc.append({
                "ticker": t,
                "open_equals_close_rate": quality.get("open_equals_close_rate"),
                "invalid_rate": quality.get("invalid_rate"),
            })
        close = df["Close"]
        fwd = {h: (close.shift(-h) / close - 1) for h in HORIZONS}
        for h in HORIZONS:
            baseline[h].extend(fwd[h].dropna().tolist())
        for name, fn in PATTERNS.items():
            if not ohlc_ok and name not in CLOSE_ONLY:
                continue
            try:
                sig = fn(df).fillna(False)
            except Exception:
                continue
            for h in HORIZONS:
                hit = fwd[h][sig].dropna()
                pooled.setdefault((name, h), []).extend(
                    (d.date().isoformat(), float(v)) for d, v in hit.items()
                )

    base_mean = {h: float(np.mean(baseline[h])) for h in HORIZONS}

    results, pvals, keys = [], [], []
    for (name, h), samples in sorted(pooled.items()):
        stat = event_study(samples, base_mean[h])
        row = {"pattern": name, "horizon_days": h, **stat}
        results.append(row)
        if not stat.get("insufficient"):
            pvals.append(stat["p_value"])
            keys.append(len(results) - 1)

    survive = benjamini_hochberg(pvals, FDR_Q)
    for flag, idx in zip(survive, keys):
        results[idx]["survives_fdr"] = bool(flag)
        results[idx]["naive_significant"] = results[idx]["p_value"] < 0.05

    tested = len(pvals)
    naive_hits = sum(1 for i in keys if results[i]["naive_significant"])
    fdr_hits = sum(1 for i in keys if results[i]["survives_fdr"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "tickers": sorted(loaded),
            "failed": failed,
            "years": years,
            "horizons": list(HORIZONS),
            "patterns_tested": len(PATTERNS),
            "hypotheses_tested": tested,
            "fdr_q": FDR_Q,
            "ohlc_untrustworthy_tickers": skipped_ohlc,
            "ohlc_patterns_skipped": bool(skipped_ohlc),
        },
        "baseline_mean_forward_return": {str(h): round(base_mean[h], 6) for h in HORIZONS},
        "results": results,
        "summary": {
            "hypotheses": tested,
            "expected_false_positives_at_5pct": round(tested * 0.05, 1),
            "naive_significant": naive_hits,
            "survive_fdr": fdr_hits,
        },
        "caveats": [
            "Forward returns for horizons above 1 day overlap, so those t-statistics "
            "are inflated; horizon 1 is the clean one.",
            "Survivorship bias: the universe is names that still trade today.",
            "No transaction costs. Most single-day edges are smaller than the spread.",
            "Pooling across tickers assumes patterns behave alike everywhere, which "
            "is an assumption, not a finding.",
        ],
    }


def cycles(ticker: str, years: int) -> dict:
    """Autocorrelation and spectral peaks in daily returns.

    Included because 'cycles' is what people ask for, and the honest answer
    needs showing rather than asserting: a random walk produces spectral peaks
    too, so a peak alone is not evidence of a cycle.
    """
    df = market.get_ohlcv(ticker, period=f"{years}y")
    r = np.log(df["Close"] / df["Close"].shift(1)).dropna().to_numpy()
    n = len(r)

    # Autocorrelation with a 95% white-noise band of about +/- 1.96/sqrt(n)
    band = 1.96 / math.sqrt(n)
    acf = []
    for lag in range(1, 61):
        a, b = r[:-lag], r[lag:]
        if a.std() == 0 or b.std() == 0:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        acf.append({"lag": lag, "autocorr": round(c, 4), "outside_noise_band": abs(c) > band})

    # Periodogram of the demeaned return series
    demeaned = r - r.mean()
    spectrum = np.abs(np.fft.rfft(demeaned)) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0)
    peaks = []
    if len(spectrum) > 3:
        order = np.argsort(spectrum[1:])[::-1][:5] + 1
        total = float(spectrum[1:].sum())
        for i in order:
            f = float(freqs[i])
            if f > 0:
                peaks.append({
                    "period_days": round(1.0 / f, 1),
                    "share_of_variance": round(float(spectrum[i]) / total, 5),
                })

    significant = [a for a in acf if a["outside_noise_band"]]
    return {
        "ticker": ticker,
        "observations": n,
        "white_noise_band": round(band, 4),
        "autocorrelation": acf,
        "lags_outside_band": [a["lag"] for a in significant],
        "spectral_peaks": peaks,
        "interpretation": (
            f"{len(significant)} of {len(acf)} lags fall outside the white-noise band; "
            f"about {round(0.05 * len(acf), 1)} are expected by chance alone. "
            "Spectral peaks are reported with their share of total variance because a "
            "random walk also produces peaks - a peak explaining a fraction of a "
            "percent of variance is not a tradeable cycle."
        ),
    }


def calendar(tickers: list[str], years: int) -> dict:
    """Day-of-week and month-of-year effects, FDR-corrected like everything else."""
    frames = []
    for t in tickers:
        try:
            df = market.get_ohlcv(t, period=f"{years}y")
        except Exception:
            continue
        r = (df["Close"] / df["Close"].shift(1) - 1).dropna()
        frames.append(pd.DataFrame({"ret": r, "dow": r.index.dayofweek, "month": r.index.month}))
    if not frames:
        return {"error": "no data"}

    allr = pd.concat(frames)
    overall = float(allr["ret"].mean())
    rows, pvals = [], []
    for label, col, names in (
        ("day_of_week", "dow", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("month", "month", ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]),
    ):
        for i, name in enumerate(names):
            key = i if col == "dow" else i + 1
            grp = allr[allr[col] == key]["ret"]
            if len(grp) < 30:
                continue
            excess = grp - overall
            naive_t = float(excess.mean() / (excess.std(ddof=1) / math.sqrt(len(grp))))
            # One observation per date: all tickers share each trading day.
            per_date = excess.groupby(excess.index).mean()
            if len(per_date) < 20 or per_date.std(ddof=1) == 0:
                continue
            t_stat = float(per_date.mean() / (per_date.std(ddof=1) / math.sqrt(len(per_date))))
            p = _norm_sf(t_stat)
            rows.append({
                "effect": label, "bucket": name, "n": int(len(grp)),
                "n_dates": int(len(per_date)),
                "mean_excess_return": round(float(excess.mean()), 6),
                "naive_t_stat": round(naive_t, 3),
                "t_stat": round(t_stat, 3), "p_value": round(p, 5),
            })
            pvals.append(p)

    for flag, row in zip(benjamini_hochberg(pvals, FDR_Q), rows):
        row["survives_fdr"] = bool(flag)
        row["naive_significant"] = row["p_value"] < 0.05

    return {
        "overall_mean_daily_return": round(overall, 6),
        "results": rows,
        "survive_fdr": sum(1 for r in rows if r["survives_fdr"]),
        "naive_significant": sum(1 for r in rows if r["naive_significant"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Event study over patterns, calendar effects and cycles.")
    ap.add_argument("--tickers")
    ap.add_argument("--universe")
    ap.add_argument("--years", type=int, default=8)
    ap.add_argument("--cycles", action="store_true", help="also run cycle analysis on the first ticker")
    ap.add_argument("--out", default="reports/patterns.json")
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
    tickers = sorted(set(tickers))

    result = run(tickers, args.years)
    if "error" in result:
        print("Failed:", result["error"])
        return 1

    result["calendar"] = calendar(tickers, args.years)
    if args.cycles:
        result["cycles"] = cycles(tickers[0], args.years)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)

    cfg, summ = result["config"], result["summary"]
    print(f"\nPattern event study  |  {len(cfg['tickers'])} tickers  |  {cfg['years']}y  "
          f"|  {cfg['patterns_tested']} patterns x {len(cfg['horizons'])} horizons")
    print(f"Hypotheses tested: {summ['hypotheses']}   "
          f"expected false positives at p<0.05: ~{summ['expected_false_positives_at_5pct']}")

    # Coverage that was dropped has to be announced. A quietly shortened test
    # list reads as "everything was checked" when it was not.
    dropped = cfg.get("ohlc_untrustworthy_tickers") or []
    if dropped:
        worst = max(x["open_equals_close_rate"] or 0 for x in dropped)
        print(f"\n  CANDLE PATTERNS SKIPPED on {len(dropped)} ticker(s): "
              f"{', '.join(x['ticker'] for x in dropped)}")
        print(f"  Synthetic OHLC - open equals close on up to {worst:.0%} of bars, so any "
              f"pattern\n  defined on the body or shadows would measure the vendor's "
              f"fill-in rule. Close-based\n  patterns are still tested and reported below.")
    print()

    print(f"  {'pattern':<21}{'h':>3}{'n':>7}{'/date':>6}{'pooled':>10}{'per-date':>10}"
          f"{'naive t':>9}{'clust t':>9}{'p':>8}  verdict")
    ranked = sorted(
        (r for r in result["results"] if not r.get("insufficient")),
        key=lambda r: abs(r["t_stat"]), reverse=True,
    )
    for r in ranked[:18]:
        verdict = "SURVIVES FDR" if r["survives_fdr"] else ("naive only" if r["naive_significant"] else "")
        flag = " SIGN FLIP" if r.get("signs_disagree") else ""
        print(f"  {r['pattern']:<21}{r['horizon_days']:>3}{r['n']:>7}{r['obs_per_date']:>6.1f}"
              f"{r['mean_excess_return']:>10.5f}{r['mean_excess_per_date']:>10.5f}"
              f"{r['naive_t_stat']:>9.2f}{r['t_stat']:>9.2f}{r['p_value']:>8.4f}"
              f"  {verdict}{flag}")

    print(f"\n  naive significant (p<0.05) : {summ['naive_significant']}")
    print(f"  survive FDR at q={FDR_Q}      : {summ['survive_fdr']}")

    cal = result["calendar"]
    print(f"\n  Calendar effects: {cal['naive_significant']} naive significant, "
          f"{cal['survive_fdr']} survive FDR")

    if result.get("cycles"):
        c = result["cycles"]
        print(f"\n  Cycles ({c['ticker']}): lags outside noise band {c['lags_outside_band'] or 'none'}")
        for p in c["spectral_peaks"][:3]:
            print(f"    period {p['period_days']:>7} days -> {p['share_of_variance']:.4%} of variance")

    print(f"\nWrote {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
