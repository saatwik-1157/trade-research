"""The feature engine: bars in, one causal row per bar out.

**There is no second indicator engine here.** `app.strategies.indicators`
already holds SMA, EMA, RSI and ATR, and those call `tools/rule_backtest.py`
and `tools/rule_search.py` rather than reimplementing them — so a feature, a
built strategy and a searched candidate all compute the same RSI. Everything
this module adds on top is arithmetic on a bar (a body, a wick, a return) or on
one of those four series, which needs no engine at all.

**Every feature is dimensionless, and that is the most important rule in the
file.** The project has measured what happens otherwise: pooling "points"
across symbols whose median H1 ATR ranges from 160 (silver) to 9,386
(palladium) produced a +4,236 headline that was an arithmetic error, not a
finding (`CLAUDE.md`, and `rule_search.py` now raises a data gap above 5x). A
model trained on `sma_20` as a raw price level learns the price of gold. So
there is no raw price level in the catalogue at all: a moving average appears
only as `ema_distance_20`, a distance as a fraction of price, and volatility
only as `atr_pct_14`. The one exception is the time features, which are
already unitless.

**Causal by construction, not by convention.** Every series here is computed
from arrays where the value at index i reads indices <= i and nothing else, and
`tests/test_datasets.py` proves it the way section 56 asks: compute through
10:00, append 10:05 and 10:10, recompute, and require the 10:00 row to be
byte-identical.

**A feature that has not warmed up is `None`.** Not zero, and not the mean. A
zero RSI is a reading and an absent one is not, and a model trained on
substituted zeros learns the substitution. The dataset builder drops those rows
rather than filling them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.marketdata.types import Bar

# Bumped when ANY feature's arithmetic changes. A dataset records the version
# it was built with, so a model can refuse features it was not trained on
# rather than silently consuming different numbers under the same names.
FEATURE_SET_VERSION = "1.0"


class FeatureError(Exception):
    """A feature cannot be computed. Never downgraded to a default value."""


@dataclass(frozen=True)
class FeatureSpec:
    """One feature: what it is, what it reads, and when it is valid.

    Section 19's registry. The point of writing the formula down is that a
    dataset built six months ago can be explained without reading the code that
    built it — and the `lookback` is what tells a caller how many leading rows
    of any series are unusable.
    """

    name: str
    version: str
    description: str
    formula: str
    inputs: tuple[str, ...]
    lookback: int
    unit: str
    # Every feature in this catalogue reads bars at or before its own
    # timestamp. The field exists so that a future feature which does not can
    # never be added silently -- it would have to say so here.
    timestamp_policy: str = "at_or_before_T"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "formula": self.formula,
            "inputs": list(self.inputs),
            "lookback": self.lookback,
            "unit": self.unit,
            "timestamp_policy": self.timestamp_policy,
        }


def _spec(
    name: str,
    description: str,
    formula: str,
    inputs: tuple[str, ...],
    lookback: int,
    unit: str,
) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        version=FEATURE_SET_VERSION,
        description=description,
        formula=formula,
        inputs=inputs,
        lookback=lookback,
        unit=unit,
    )


# --------------------------------------------------------------- catalogue

CATALOGUE: dict[str, FeatureSpec] = {
    # ---- price shape. All divided by the close, so EURUSD and XAUUSD are
    # the same quantity rather than two numbers 3,000x apart.
    "return_1": _spec(
        "return_1",
        "One-bar simple return.",
        "close[t] / close[t-1] - 1",
        ("close",),
        1,
        "ratio",
    ),
    "return_5": _spec(
        "return_5",
        "Five-bar simple return.",
        "close[t] / close[t-5] - 1",
        ("close",),
        5,
        "ratio",
    ),
    "log_return_1": _spec(
        "log_return_1",
        "One-bar log return. Additive across bars, which the simple return is not.",
        "ln(close[t] / close[t-1])",
        ("close",),
        1,
        "ratio",
    ),
    "body_pct": _spec(
        "body_pct",
        "Signed candle body as a fraction of the close.",
        "(close[t] - open[t]) / close[t]",
        ("open", "close"),
        0,
        "ratio",
    ),
    "range_pct": _spec(
        "range_pct",
        "Candle range as a fraction of the close.",
        "(high[t] - low[t]) / close[t]",
        ("high", "low", "close"),
        0,
        "ratio",
    ),
    "upper_wick_pct": _spec(
        "upper_wick_pct",
        "Upper shadow as a fraction of the close.",
        "(high[t] - max(open[t], close[t])) / close[t]",
        ("open", "high", "close"),
        0,
        "ratio",
    ),
    "lower_wick_pct": _spec(
        "lower_wick_pct",
        "Lower shadow as a fraction of the close.",
        "(min(open[t], close[t]) - low[t]) / close[t]",
        ("open", "low", "close"),
        0,
        "ratio",
    ),
    # ---- trend. Never the moving average itself: a distance FROM it.
    "sma_distance_20": _spec(
        "sma_distance_20",
        "Distance from the 20-bar simple moving average, as a fraction of it.",
        "close[t] / SMA(close, 20)[t] - 1",
        ("close",),
        20,
        "ratio",
    ),
    "ema_distance_20": _spec(
        "ema_distance_20",
        "Distance from the 20-bar exponential moving average, as a fraction of it.",
        "close[t] / EMA(close, 20)[t] - 1",
        ("close",),
        20,
        "ratio",
    ),
    "ema_spread_10_50": _spec(
        "ema_spread_10_50",
        "Fast minus slow EMA, as a fraction of the slow one. The sign is a trend state.",
        "(EMA(close, 10)[t] - EMA(close, 50)[t]) / EMA(close, 50)[t]",
        ("close",),
        50,
        "ratio",
    ),
    # ---- momentum
    "rsi_14": _spec(
        "rsi_14",
        "Wilder RSI over 14 bars, 0-100. The same function the live rules use.",
        "wilder_rsi(close, 14)[t]",
        ("close",),
        14,
        "oscillator",
    ),
    "roc_10": _spec(
        "roc_10",
        "Ten-bar rate of change.",
        "close[t] / close[t-10] - 1",
        ("close",),
        10,
        "ratio",
    ),
    # ---- volatility
    "atr_pct_14": _spec(
        "atr_pct_14",
        "Average true range over 14 bars, as a fraction of the close. The "
        "raw ATR is in the symbol's own points and is deliberately not offered.",
        "ATR(14)[t] / close[t]",
        ("high", "low", "close"),
        14,
        "ratio",
    ),
    "volatility_20": _spec(
        "volatility_20",
        "Sample standard deviation of the last 20 one-bar log returns.",
        "stdev(log_return_1[t-19 .. t])",
        ("close",),
        20,
        "ratio",
    ),
    "atr_ratio_14_50": _spec(
        "atr_ratio_14_50",
        "Short ATR over long ATR. Above 1 means volatility is expanding.",
        "ATR(14)[t] / ATR(50)[t]",
        ("high", "low", "close"),
        50,
        "ratio",
    ),
    # ---- volume. NULL where the feed does not supply it -- never 0, because
    # `cost_profile.py` already measured what treating an unrecorded value as
    # a real zero does to a result.
    "volume_change_1": _spec(
        "volume_change_1",
        "One-bar change in volume. Null when either bar has no volume.",
        "volume[t] / volume[t-1] - 1",
        ("volume",),
        1,
        "ratio",
    ),
    "relative_volume_20": _spec(
        "relative_volume_20",
        "Volume over its own 20-bar mean. Null when any bar in the window has none.",
        "volume[t] / mean(volume[t-19 .. t])",
        ("volume",),
        20,
        "ratio",
    ),
    # ---- market structure
    "high_20_distance": _spec(
        "high_20_distance",
        "Distance below the highest high of the last 20 bars, as a fraction. "
        "Zero means this bar set the high.",
        "close[t] / max(high[t-19 .. t]) - 1",
        ("high", "close"),
        20,
        "ratio",
    ),
    "low_20_distance": _spec(
        "low_20_distance",
        "Distance above the lowest low of the last 20 bars, as a fraction.",
        "close[t] / min(low[t-19 .. t]) - 1",
        ("low", "close"),
        20,
        "ratio",
    ),
    # ---- time. Already unitless, and the last one is measured rather than
    # guessed: `cost_profile.py` puts server hour 00 at 4x the normal spread,
    # and 14.3% of M1 bars in its first minute exceed a 1.5xATR bracket.
    "hour_utc": _spec(
        "hour_utc",
        "Hour of the bar's UTC open time, 0-23. A category, not a magnitude.",
        "bar_time[t].hour",
        ("bar_time",),
        0,
        "category",
    ),
    "day_of_week": _spec(
        "day_of_week",
        "Weekday of the bar's UTC open time, Monday=0.",
        "bar_time[t].weekday()",
        ("bar_time",),
        0,
        "category",
    ),
    "is_rollover_hour": _spec(
        "is_rollover_hour",
        "1 during the broker's rollover hour (21:00 UTC = server 00:00 at UTC+3), else 0. "
        "The one hour whose quote bands measurably do not fit inside a bracket.",
        "1 if bar_time[t].hour == 21 else 0",
        ("bar_time",),
        0,
        "category",
    ),
}

DEFAULT_FEATURES: tuple[str, ...] = tuple(CATALOGUE)

# The broker is UTC+3, so its 00:00 rollover is 21:00 UTC. Written once, here,
# rather than as a bare 21 inside the loop.
ROLLOVER_HOUR_UTC = 21


def spec_for(name: str) -> FeatureSpec:
    try:
        return CATALOGUE[name]
    except KeyError as exc:
        raise FeatureError(
            f"unknown feature {name!r}; the catalogue holds {', '.join(sorted(CATALOGUE))}"
        ) from exc


def catalogue() -> list[dict[str, object]]:
    return [spec.as_dict() for spec in CATALOGUE.values()]


def warmup_for(names: tuple[str, ...]) -> int:
    """Bars needed before every requested feature means something."""
    return max((spec_for(n).lookback for n in names), default=0)


# ------------------------------------------------------------- computation


def _floats(values: list[Decimal | None]) -> list[float | None]:
    return [None if v is None else float(v) for v in values]


def _finite(value: float | None) -> float | None:
    """None for anything that is not a real number.

    NaN and infinity reach a dataset the same way a missing value does, and a
    model cannot tell them apart from a real reading unless they are made
    absent here.
    """
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _finite(numerator / denominator)


def _distance(value: float | None, base: float | None) -> float | None:
    """`value / base - 1`, and None if either side is missing.

    Written as its own helper because the arithmetic is only safe once both
    sides are known to exist, and doing the subtraction first -- with a `or 0`
    standing in for the missing side -- produces a number that then has to be
    thrown away. Checking first is the version that cannot be misread.
    """
    if value is None or base is None or base == 0:
        return None
    return _finite(value / base - 1.0)


def _indicator(
    key: str,
    period: int,
    opens: list[Decimal],
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
) -> list[float | None]:
    """One series from the existing catalogue. Imported here, never copied."""
    from app.strategies.indicators import compute

    return compute(key, {"period": period}, opens, highs, lows, closes)


def compute_features(
    bars: list[Bar],
    names: tuple[str, ...] = DEFAULT_FEATURES,
) -> list[dict[str, float | None]]:
    """One row per bar, in the same order, with `None` before warm-up.

    The returned list is the same length as `bars` deliberately: alignment with
    the label series is by index, and a function that silently dropped its
    leading rows would make that alignment a matter of arithmetic somebody has
    to get right rather than a property of the return value.
    """
    for name in names:
        spec_for(name)  # refuse an unknown name before computing anything

    n = len(bars)
    if n == 0:
        return []

    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    close_f = [float(c) for c in closes]
    open_f = [float(o) for o in opens]
    high_f = [float(h) for h in highs]
    low_f = [float(low) for low in lows]
    volume_f = _floats([b.volume for b in bars])

    wanted = set(names)

    # Only compute a series something asked for. Each of these is O(n) and the
    # engine call is not free.
    def needed(*keys: str) -> bool:
        return any(k in wanted for k in keys)

    sma20 = _indicator("SMA", 20, opens, highs, lows, closes) if "sma_distance_20" in wanted else []
    ema20 = _indicator("EMA", 20, opens, highs, lows, closes) if "ema_distance_20" in wanted else []
    ema10 = (
        _indicator("EMA", 10, opens, highs, lows, closes) if "ema_spread_10_50" in wanted else []
    )
    ema50 = (
        _indicator("EMA", 50, opens, highs, lows, closes) if "ema_spread_10_50" in wanted else []
    )
    rsi14 = _indicator("RSI", 14, opens, highs, lows, closes) if "rsi_14" in wanted else []
    atr14 = (
        _indicator("ATR", 14, opens, highs, lows, closes)
        if needed("atr_pct_14", "atr_ratio_14_50")
        else []
    )
    atr50 = _indicator("ATR", 50, opens, highs, lows, closes) if "atr_ratio_14_50" in wanted else []

    log_returns: list[float | None] = [None] * n
    if needed("log_return_1", "volatility_20"):
        for i in range(1, n):
            prior, now = close_f[i - 1], close_f[i]
            log_returns[i] = _finite(math.log(now / prior)) if prior > 0 and now > 0 else None

    rows: list[dict[str, float | None]] = []
    for i in range(n):
        row: dict[str, float | None] = {}
        close = close_f[i]

        if "return_1" in wanted:
            row["return_1"] = _distance(close, close_f[i - 1]) if i >= 1 else None
        if "return_5" in wanted:
            row["return_5"] = _distance(close, close_f[i - 5]) if i >= 5 else None
        if "log_return_1" in wanted:
            row["log_return_1"] = log_returns[i]
        if "body_pct" in wanted:
            row["body_pct"] = _ratio(close - open_f[i], close)
        if "range_pct" in wanted:
            row["range_pct"] = _ratio(high_f[i] - low_f[i], close)
        if "upper_wick_pct" in wanted:
            row["upper_wick_pct"] = _ratio(high_f[i] - max(open_f[i], close), close)
        if "lower_wick_pct" in wanted:
            row["lower_wick_pct"] = _ratio(min(open_f[i], close) - low_f[i], close)

        if "sma_distance_20" in wanted:
            row["sma_distance_20"] = _distance(close, sma20[i])
        if "ema_distance_20" in wanted:
            row["ema_distance_20"] = _distance(close, ema20[i])
        if "ema_spread_10_50" in wanted:
            row["ema_spread_10_50"] = _distance(ema10[i], ema50[i])

        if "rsi_14" in wanted:
            row["rsi_14"] = _finite(rsi14[i])
        if "roc_10" in wanted:
            row["roc_10"] = _distance(close, close_f[i - 10]) if i >= 10 else None

        if "atr_pct_14" in wanted:
            row["atr_pct_14"] = _ratio(atr14[i], close)
        if "volatility_20" in wanted:
            row["volatility_20"] = _stdev(log_returns, i, 20)
        if "atr_ratio_14_50" in wanted:
            row["atr_ratio_14_50"] = _ratio(atr14[i], atr50[i])

        if "volume_change_1" in wanted:
            row["volume_change_1"] = _distance(volume_f[i], volume_f[i - 1]) if i >= 1 else None
        if "relative_volume_20" in wanted:
            row["relative_volume_20"] = _relative_volume(volume_f, i, 20)

        if "high_20_distance" in wanted:
            row["high_20_distance"] = (
                _distance(close, max(high_f[i - 19 : i + 1])) if i >= 19 else None
            )
        if "low_20_distance" in wanted:
            row["low_20_distance"] = (
                _distance(close, min(low_f[i - 19 : i + 1])) if i >= 19 else None
            )

        at = bars[i].bar_time
        if "hour_utc" in wanted:
            row["hour_utc"] = float(at.hour)
        if "day_of_week" in wanted:
            row["day_of_week"] = float(at.weekday())
        if "is_rollover_hour" in wanted:
            row["is_rollover_hour"] = 1.0 if at.hour == ROLLOVER_HOUR_UTC else 0.0

        rows.append(row)

    return rows


def _stdev(values: list[float | None], index: int, window: int) -> float | None:
    """Sample standard deviation of a trailing window, or None if it is short.

    A window containing a single `None` returns `None` rather than a figure
    computed from the rest of it: a volatility over 19 of 20 bars is a
    different quantity from one over 20, and nothing downstream would know.
    """
    if index + 1 < window:
        return None
    window_values = values[index - window + 1 : index + 1]
    if any(v is None for v in window_values):
        return None
    sample = [v for v in window_values if v is not None]
    mean = sum(sample) / len(sample)
    variance = sum((v - mean) ** 2 for v in sample) / (len(sample) - 1)
    return _finite(math.sqrt(variance))


def _relative_volume(volumes: list[float | None], index: int, window: int) -> float | None:
    if index + 1 < window:
        return None
    window_values = volumes[index - window + 1 : index + 1]
    if any(v is None for v in window_values):
        return None
    sample = [v for v in window_values if v is not None]
    mean = sum(sample) / len(sample)
    return _ratio(sample[-1], mean)


def feature_row_is_complete(row: dict[str, float | None], names: tuple[str, ...]) -> bool:
    """True when every requested feature has a value.

    The dataset builder uses this to decide which rows are usable. It does not
    impute: a row that is short a feature is dropped and counted, because the
    alternative is a model learning whatever the filler was.
    """
    return all(row.get(name) is not None for name in names)


def feature_set_manifest(names: tuple[str, ...]) -> dict[str, Any]:
    """What produced these features, in a form that can be stored beside them."""
    return {
        "feature_set_version": FEATURE_SET_VERSION,
        "features": [spec_for(n).as_dict() for n in names],
        "warmup_bars": warmup_for(names),
        "indicator_source": "app.strategies.indicators (tools/rule_backtest.py, rule_search.py)",
        "units": "every feature is dimensionless; no raw price level is offered",
    }
