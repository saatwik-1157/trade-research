"""Market data access layer.

The only place in this project that talks to a price data provider. Everything
downstream consumes what this module returns, so if the source is ever swapped
(Polygon, Tiingo, a broker feed) nothing else has to change.

Data is cached to disk per (ticker, day) so repeated analysis of the same name
does not re-hit the provider.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

PROVIDER = "yfinance (Yahoo Finance)"
BENCHMARK = "SPY"


def _cache_path(key: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(CACHE_DIR, f"{safe}.{day}.json")


def _read_cache(key: str, max_age_s: int = 3600):
    path = _cache_path(key)
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age_s:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
    return None


# Cache entries are named per (key, UTC day), so each day writes a fresh set
# beside the previous one and nothing ever overwrites. Read freshness is capped
# at 12 hours, so anything older than a few days is dead weight: a single day of
# ordinary use leaves ~175 files. Sweep it once per process rather than on every
# write, which would mean a directory scan per cached call.
CACHE_MAX_AGE_DAYS = 7
_PRUNED = False


def prune_cache(max_age_days: int = CACHE_MAX_AGE_DAYS) -> int:
    """Delete cache entries older than max_age_days. Returns how many were removed."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue  # a file that vanished or is locked is not worth failing over
    return removed


def _prune_once() -> None:
    global _PRUNED
    if not _PRUNED:
        _PRUNED = True
        prune_cache()


def _write_cache(key: str, payload) -> None:
    _prune_once()
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
    except OSError:
        pass


# Populated by validate_ohlcv, read by provenance() so data quality travels with
# the numbers instead of being discovered later.
VALIDATION: dict[str, dict] = {}


def validate_ohlcv(ticker: str, df: pd.DataFrame) -> pd.DataFrame:
    """Repair bars whose OHLC cannot all be true, and record how many there were.

    Yahoo's FX series takes its close from a different snapshot than the high
    and low, so 2-6% of currency bars have a close outside the day's range.
    That is not a rounding nuisance: any pattern defined on candle shadows
    computes a negative shadow on such a bar and fires unconditionally. Left
    unvalidated it produced a t-statistic of 28 in this repository's own forex
    pattern study - an artefact that reads exactly like a spectacular edge.

    Equities and crypto from the same provider show zero violations, so this is
    a source-and-asset-class problem rather than a universal one, which is
    precisely why the rate is recorded per ticker rather than assumed.

    Repair clamps the range to contain the open and close. The close is the
    better-sourced value; the extremes are what get stretched.
    """
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    eps = 1e-12
    bad = ((c > h + eps) | (c < l - eps) | (o > h + eps) | (o < l - eps) | (h < l))
    n_bad = int(bad.sum())
    n = len(df)

    # The sharper tell is how often the open exactly equals the close. Real bars
    # almost never do (0.4% on equities and futures, 0% on crypto); Yahoo's FX
    # series does on ~40% of bars, because those opens are synthesised rather
    # than observed. Any pattern defined on the candle body or its shadows is
    # then measuring the vendor's fill-in rule, not the market. Verified against
    # the same underlying: hammer gives t = -8.3 on EURUSD=X and t = -0.2 on the
    # Euro ETF and -1.0 on EUR futures over the same decade.
    oc_rate = float((o == c).mean()) if n else 0.0
    invalid_rate = n_bad / n if n else 0.0
    trustworthy = invalid_rate < 0.005 and oc_rate < 0.10

    VALIDATION[ticker] = {
        "bars": int(n),
        "invalid_bars": n_bad,
        "invalid_rate": round(invalid_rate, 5),
        "open_equals_close_rate": round(oc_rate, 5),
        "repaired": n_bad > 0,
        "ohlc_trustworthy": trustworthy,
        "close_series_usable": True,
        "note": (
            "OHLC is synthetic or inconsistent on this source. Close-based analysis "
            "is fine; anything defined on the open, the body or the shadows is "
            "measuring the vendor's construction and must not be run."
            if not trustworthy else "All bars internally consistent."
        ),
    }

    if n_bad:
        df = df.copy()
        df["High"] = pd.concat([h, o, c], axis=1).max(axis=1)
        df["Low"] = pd.concat([l, o, c], axis=1).min(axis=1)
    return df


def get_ohlcv(ticker: str, period: str = "3y", interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV as a DataFrame indexed by date. Raises if the ticker is empty."""
    import yfinance as yf

    key = f"ohlcv.{ticker}.{period}.{interval}"
    cached = _read_cache(key)
    if cached:
        df = pd.DataFrame(cached["data"])
        df.index = pd.to_datetime(df.pop("Date"))
        return validate_ohlcv(ticker, df)

    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"No price history returned for {ticker!r} from {PROVIDER}")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)

    out = df.reset_index()
    out.columns = ["Date"] + list(df.columns)
    _write_cache(key, {"data": out.to_dict(orient="records")})
    return validate_ohlcv(ticker, df)


def get_benchmark_close(period: str = "3y") -> pd.Series | None:
    try:
        return get_ohlcv(BENCHMARK, period=period)["Close"]
    except Exception:
        return None


# Fields pulled from the provider profile. Kept explicit so the snapshot can
# never silently gain an unvetted field.
_PROFILE_FIELDS = [
    "shortName", "longName", "sector", "industry", "country", "currency",
    "fullTimeEmployees", "website", "longBusinessSummary",
]
_VALUATION_FIELDS = [
    "marketCap", "enterpriseValue", "trailingPE", "forwardPE", "priceToBook",
    "priceToSalesTrailing12Months", "enterpriseToRevenue", "enterpriseToEbitda",
    "pegRatio", "dividendYield", "payoutRatio",
]
_FUNDAMENTAL_FIELDS = [
    "totalRevenue", "revenueGrowth", "earningsGrowth", "grossMargins",
    "operatingMargins", "profitMargins", "ebitdaMargins", "returnOnEquity",
    "returnOnAssets", "debtToEquity", "totalDebt", "totalCash", "freeCashflow",
    "operatingCashflow", "currentRatio", "quickRatio", "bookValue",
]
_ANALYST_FIELDS = [
    "recommendationMean", "recommendationKey", "numberOfAnalystOpinions",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice", "targetMedianPrice",
]
_SHARE_FIELDS = [
    "sharesOutstanding", "floatShares", "sharesShort", "shortRatio",
    "shortPercentOfFloat", "heldPercentInsiders", "heldPercentInstitutions",
]


def get_profile(ticker: str) -> dict:
    """Company profile, valuation, fundamentals, analyst coverage and float data.

    Returns four separate blocks rather than one flat dict so downstream code
    can reason about which provider surface each number came from.
    """
    import yfinance as yf

    key = f"profile.{ticker}"
    cached = _read_cache(key, max_age_s=6 * 3600)
    if cached:
        return cached

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:  # provider hiccup should degrade, not crash
        return {"error": f"{type(exc).__name__}: {exc}", "available": False}

    def pick(fields):
        return {f: info.get(f) for f in fields if info.get(f) is not None}

    out = {
        "available": True,
        "profile": pick(_PROFILE_FIELDS),
        "valuation": pick(_VALUATION_FIELDS),
        "fundamentals": pick(_FUNDAMENTAL_FIELDS),
        "analyst": pick(_ANALYST_FIELDS),
        "shares": pick(_SHARE_FIELDS),
        "missing_fields": sorted(
            f for f in _PROFILE_FIELDS + _VALUATION_FIELDS + _FUNDAMENTAL_FIELDS + _ANALYST_FIELDS + _SHARE_FIELDS
            if info.get(f) is None
        ),
    }
    _write_cache(key, out)
    return out


def get_earnings_dates(ticker: str, limit: int = 8) -> list[dict]:
    """Upcoming and recent earnings dates with EPS estimate vs actual."""
    import yfinance as yf

    key = f"earnings.{ticker}"
    cached = _read_cache(key, max_age_s=12 * 3600)
    if cached is not None:
        return cached

    rows = []
    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=limit)
        if df is not None and not df.empty:
            for idx, row in df.iterrows():
                rows.append({
                    "date": pd.Timestamp(idx).date().isoformat(),
                    "eps_estimate": None if pd.isna(row.get("EPS Estimate")) else float(row.get("EPS Estimate")),
                    "eps_actual": None if pd.isna(row.get("Reported EPS")) else float(row.get("Reported EPS")),
                    "surprise_pct": None if pd.isna(row.get("Surprise(%)")) else float(row.get("Surprise(%)")),
                })
    except Exception:
        pass
    _write_cache(key, rows)
    return rows


def provenance(ticker: str, df: pd.DataFrame) -> dict:
    """Where the numbers came from. Attached to every snapshot."""
    return {
        "price_data": {
            "source": PROVIDER,
            "ticker": ticker,
            "interval": "1d",
            "adjusted": True,
            "first_bar": pd.Timestamp(df.index[0]).date().isoformat(),
            "last_bar": pd.Timestamp(df.index[-1]).date().isoformat(),
            "bars": int(len(df)),
        },
        "indicators": {
            "source": "computed locally in tools/indicators.py from the OHLCV series above",
            "estimated": False,
        },
        "data_quality": VALIDATION.get(ticker, {"note": "not validated"}),
        "benchmark": {"source": PROVIDER, "ticker": BENCHMARK},
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Market data cache maintenance.")
    ap.add_argument("--prune", action="store_true", help="delete stale cache entries")
    ap.add_argument("--max-age-days", type=int, default=CACHE_MAX_AGE_DAYS,
                    help=f"age threshold in days (default {CACHE_MAX_AGE_DAYS})")
    args = ap.parse_args()
    if not args.prune:
        ap.print_help()
        raise SystemExit(0)
    n = prune_cache(args.max_age_days)
    print(f"removed {n} cache entries older than {args.max_age_days} days from {CACHE_DIR}")
