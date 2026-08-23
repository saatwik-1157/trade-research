"""Technical indicators computed from OHLCV data.

Every function here takes a pandas DataFrame/Series of real price data and
returns a number derived arithmetically from it. Nothing in this module
estimates, infers, or guesses. If the input history is too short to compute an
indicator, the function returns None rather than a plausible-looking value.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _f(x):
    """Coerce to a plain float, mapping NaN/inf to None."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, 6)


def sma(close: pd.Series, n: int):
    if len(close) < n:
        return None
    return _f(close.rolling(n).mean().iloc[-1])


def ema_series(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14):
    """Wilder RSI. Requires 3n bars so the smoothing has settled."""
    if len(close) < 3 * n:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing is an EWM with alpha = 1/n
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / last_loss
    return _f(100 - (100 / (1 + rs)))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    if len(close) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}
    line = ema_series(close, fast) - ema_series(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return {
        "macd": _f(line.iloc[-1]),
        "signal": _f(sig.iloc[-1]),
        "histogram": _f(line.iloc[-1] - sig.iloc[-1]),
    }


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14):
    if len(df) < 2 * n:
        return None
    return _f(true_range(df).ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def adx(df: pd.DataFrame, n: int = 14):
    """Average Directional Index - trend strength, direction agnostic."""
    if len(df) < 3 * n:
        return None
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr_n = true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr_n
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr_n
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return _f(dx.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    empty = {"upper": None, "middle": None, "lower": None, "percent_b": None, "bandwidth": None}
    if len(close) < n:
        return empty
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = upper.iloc[-1] - lower.iloc[-1]
    pct_b = (close.iloc[-1] - lower.iloc[-1]) / width if width else None
    return {
        "upper": _f(upper.iloc[-1]),
        "middle": _f(mid.iloc[-1]),
        "lower": _f(lower.iloc[-1]),
        "percent_b": _f(pct_b),
        "bandwidth": _f(width / mid.iloc[-1]) if mid.iloc[-1] else None,
    }


def obv_trend(df: pd.DataFrame, n: int = 20):
    """Normalised slope of on-balance volume over the last n bars."""
    if len(df) < n + 1:
        return None
    direction = np.sign(df["Close"].diff().fillna(0.0))
    obv = (direction * df["Volume"]).cumsum()
    window = obv.iloc[-n:].to_numpy(dtype=float)
    slope = np.polyfit(np.arange(len(window)), window, 1)[0]
    denom = df["Volume"].iloc[-n:].mean()
    return _f(slope / denom) if denom else None


def realized_vol(close: pd.Series, n: int = 20, annualize: int = 252):
    if len(close) < n + 1:
        return None
    r = np.log(close / close.shift(1)).dropna()
    if len(r) < n:
        return None
    return _f(r.iloc[-n:].std(ddof=1) * np.sqrt(annualize))


def max_drawdown(close: pd.Series):
    if len(close) < 2:
        return None
    running_max = close.cummax()
    return _f(((close / running_max) - 1).min())


def beta(close: pd.Series, bench_close: pd.Series | None):
    """Beta against a benchmark, on dates where both series actually traded."""
    if bench_close is None or len(close) < 60 or len(bench_close) < 60:
        return None
    a = np.log(close / close.shift(1)).dropna()
    b = np.log(bench_close / bench_close.shift(1)).dropna()
    a.index = pd.to_datetime(a.index).tz_localize(None).normalize()
    b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
    joined = pd.concat([a.rename("s"), b.rename("m")], axis=1, join="inner").dropna()
    if len(joined) < 60:
        return None
    var_m = joined["m"].var(ddof=1)
    if not var_m:
        return None
    return _f(joined["s"].cov(joined["m"]) / var_m)


def swing_points(df: pd.DataFrame, left: int = 5, right: int = 5):
    """Fractal swing highs/lows: bars that are the extreme of their own window.

    These are the raw material for support, resistance and Fibonacci levels, so
    every level downstream traces back to an actual bar in the price series.
    """
    highs, lows = [], []
    h = df["High"].to_numpy(dtype=float)
    lo = df["Low"].to_numpy(dtype=float)
    idx = df.index
    for i in range(left, len(df) - right):
        w = slice(i - left, i + right + 1)
        if h[i] == h[w].max() and (h[w] == h[i]).sum() == 1:
            highs.append((pd.Timestamp(idx[i]).date().isoformat(), _f(h[i])))
        if lo[i] == lo[w].min() and (lo[w] == lo[i]).sum() == 1:
            lows.append((pd.Timestamp(idx[i]).date().isoformat(), _f(lo[i])))
    return highs, lows


def support_resistance(df: pd.DataFrame, price: float, left: int = 5, right: int = 5, k: int = 3):
    """Nearest k swing lows below price (support) and swing highs above it."""
    highs, lows = swing_points(df, left, right)
    support = sorted([p for _, p in lows if p is not None and p < price], reverse=True)[:k]
    resistance = sorted([p for _, p in highs if p is not None and p > price])[:k]
    return {
        "support": support,
        "resistance": resistance,
        "method": f"fractal swing pivots (left={left}, right={right}) over the supplied OHLCV window",
        "swing_high_count": len(highs),
        "swing_low_count": len(lows),
    }


def fibonacci(df: pd.DataFrame, lookback: int = 180):
    """Retracement levels of the dominant swing in the lookback window.

    The swing is the actual highest high and lowest low in the window, and both
    dates are returned. A Fibonacci level with no named swing behind it is
    meaningless, so this never emits levels without one.
    """
    if len(df) < 20:
        return None
    w = df.iloc[-lookback:]
    hi_idx, lo_idx = w["High"].idxmax(), w["Low"].idxmin()
    hi, lo = _f(w["High"].max()), _f(w["Low"].min())
    if hi is None or lo is None or hi <= lo:
        return None
    span = hi - lo
    up = hi_idx > lo_idx  # the low came first, so the swing travelled upward
    levels = {}
    for r in (0.236, 0.382, 0.5, 0.618, 0.786):
        levels[f"{r:.3f}"] = _f(hi - span * r if up else lo + span * r)
    return {
        "swing_high": hi,
        "swing_high_date": pd.Timestamp(hi_idx).date().isoformat(),
        "swing_low": lo,
        "swing_low_date": pd.Timestamp(lo_idx).date().isoformat(),
        "direction": "up" if up else "down",
        "levels": levels,
        "lookback_bars": int(len(w)),
    }


def compute_all(df: pd.DataFrame, bench_close: pd.Series | None = None) -> dict:
    """Full technical feature set. Any field that cannot be computed is None."""
    close = df["Close"]
    price = _f(close.iloc[-1])
    s50, s200 = sma(close, 50), sma(close, 200)
    hi52 = _f(df["High"].iloc[-252:].max()) if len(df) >= 60 else None
    lo52 = _f(df["Low"].iloc[-252:].min()) if len(df) >= 60 else None
    atr14 = atr(df, 14)

    def ret(n):
        return _f(close.iloc[-1] / close.iloc[-1 - n] - 1) if len(close) > n else None

    return {
        "price": price,
        "asof": pd.Timestamp(df.index[-1]).date().isoformat(),
        "bars_used": int(len(df)),
        "sma20": sma(close, 20),
        "sma50": s50,
        "sma200": s200,
        "price_vs_sma50": _f(price / s50 - 1) if s50 else None,
        "price_vs_sma200": _f(price / s200 - 1) if s200 else None,
        "golden_cross": bool(s50 > s200) if (s50 and s200) else None,
        "rsi14": rsi(close, 14),
        "macd": macd(close),
        "atr14": atr14,
        "atr_pct": _f(atr14 / price) if (atr14 and price) else None,
        "adx14": adx(df, 14),
        "bollinger": bollinger(close),
        "obv_slope_norm": obv_trend(df),
        "realized_vol_20d": realized_vol(close, 20),
        "realized_vol_60d": realized_vol(close, 60),
        "max_drawdown_1y": max_drawdown(close.iloc[-252:]) if len(close) >= 60 else None,
        "beta_vs_benchmark": beta(close, bench_close),
        "high_52w": hi52,
        "low_52w": lo52,
        "pct_off_52w_high": _f(price / hi52 - 1) if hi52 else None,
        "pct_above_52w_low": _f(price / lo52 - 1) if lo52 else None,
        "return_1m": ret(21),
        "return_3m": ret(63),
        "return_6m": ret(126),
        "return_12m": ret(252),
        "momentum_12_1": _f(close.iloc[-22] / close.iloc[-253] - 1) if len(close) > 253 else None,
        "avg_volume_20d": _f(df["Volume"].iloc[-20:].mean()) if len(df) >= 20 else None,
        "dollar_volume_20d": _f((df["Close"] * df["Volume"]).iloc[-20:].mean()) if len(df) >= 20 else None,
        "levels": support_resistance(df, price) if price else None,
        "fibonacci": fibonacci(df),
    }
