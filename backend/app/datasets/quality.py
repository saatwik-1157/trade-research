"""The data-quality score, built on the measurements that already exist.

Section 11. `app.marketdata.validation.inspect_series` already counts invalid
OHLC, duplicates, out-of-order bars, gaps, future-stamped bars and the
open-equals-close rate, and `SeriesQuality.ohlc_trustworthy` already encodes
the thresholds `tools/market.validate_ohlcv` measured. None of that is
recomputed here. This module turns it into one number and, more importantly,
into a verdict about which *kinds* of feature a series can support.

**The score does not block on its own.** Section 11 is explicit that an
imperfect score should not stop everything, and the project agrees for a
measured reason: real market anomalies look like bad data and deleting them
loses the events worth learning from. So `usable` is decided by hard failures —
a series that is not a series — while the score is a number to read.

**`ohlc_trustworthy` is the one that changes what may be built.** The project
has a worked example: an unvalidated Yahoo FX series produced a t-statistic of
28 from bars whose close sat outside the day's range. When it is false, the
close is usable and anything defined on the open, the body or the shadows is
measuring the vendor's construction. The builder drops those features rather
than the series.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.marketdata.types import Bar, SeriesQuality, Timeframe
from app.marketdata.validation import inspect_series

# Features whose value comes from the candle's construction rather than its
# close. Dropped when a source's OHLC is not trustworthy.
SHAPE_FEATURES: frozenset[str] = frozenset(
    {"body_pct", "range_pct", "upper_wick_pct", "lower_wick_pct"}
)


@dataclass(frozen=True)
class QualityReport:
    quality: SeriesQuality
    completeness: float
    duplicate_rate: float
    missing_volume_rate: float
    missing_spread_rate: float
    score: float
    usable: bool
    blocking: tuple[str, ...]
    warnings: tuple[str, ...]
    unusable_features: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": self.quality.as_dict(),
            "completeness": round(self.completeness, 5),
            "duplicate_rate": round(self.duplicate_rate, 5),
            "missing_volume_rate": round(self.missing_volume_rate, 5),
            "missing_spread_rate": round(self.missing_spread_rate, 5),
            "score": round(self.score, 4),
            "usable": self.usable,
            "blocking": list(self.blocking),
            "warnings": list(self.warnings),
            "unusable_features": list(self.unusable_features),
            "policy": (
                "the score is reported and does not block; only a series that is not a "
                "series blocks. Real market anomalies are kept -- they are the events "
                "worth learning from, and deleting them is how a dataset stops "
                "describing the market it came from."
            ),
        }


def assess(bars: list[Bar], timeframe: Timeframe, *, now: datetime | None = None) -> QualityReport:
    """Measure a series and say what it can support. Changes nothing."""
    quality = inspect_series(bars, timeframe, now=now)
    n = len(bars)

    if n == 0:
        return QualityReport(
            quality=quality,
            completeness=0.0,
            duplicate_rate=0.0,
            missing_volume_rate=1.0,
            missing_spread_rate=1.0,
            score=0.0,
            usable=False,
            blocking=("the series is empty",),
            warnings=(),
            unusable_features=(),
        )

    # Completeness against the slots a continuous series would have held. A
    # market that closes at the weekend shows as a gap, which is why gaps are
    # scored rather than treated as corruption.
    expected = n + quality.gaps
    completeness = n / expected if expected else 0.0
    duplicate_rate = quality.duplicates / n
    missing_volume = sum(1 for b in bars if b.volume is None) / n
    missing_spread = sum(1 for b in bars if b.spread is None) / n

    blocking: list[str] = []
    warnings: list[str] = []

    if quality.invalid_ohlc:
        blocking.append(
            f"{quality.invalid_ohlc} bar(s) are not candles at all (high < low, or the "
            "open or close outside the range). These are corrupt rows, not market events."
        )
    if quality.out_of_order:
        blocking.append(
            f"{quality.out_of_order} bar(s) arrived out of order. Every causal guarantee "
            "in this pipeline assumes an ordered series, so this is refused rather than "
            "sorted -- the ordering problem is upstream."
        )
    if quality.future_stamped:
        blocking.append(
            f"{quality.future_stamped} bar(s) are stamped in the future. That is a clock "
            "disagreement between this process and the source, and features built from "
            "it would be dated wrongly."
        )
    if quality.duplicates:
        warnings.append(
            f"{quality.duplicates} duplicate bar time(s). Ingestion is idempotent on "
            "(provider, symbol, timeframe, bar_time), so these are de-duplicated rather "
            "than dropped as bad data."
        )
    if quality.gaps:
        warnings.append(
            f"{quality.gaps} missing slot(s). Not filled: a fabricated candle is "
            "indistinguishable from a real one downstream."
        )
    if missing_volume > 0:
        warnings.append(
            f"{missing_volume:.1%} of bars have no volume. Volume features are null "
            "there rather than zero -- an unrecorded volume is not a quiet market."
        )
    if missing_spread > 0:
        warnings.append(
            f"{missing_spread:.1%} of bars have no recorded spread. A recorded 0 would "
            "be worse: `cost_profile.py` measured that averaging those zeros in halves "
            "the apparent cost of trading."
        )

    unusable: tuple[str, ...] = ()
    if not quality.ohlc_trustworthy:
        unusable = tuple(sorted(SHAPE_FEATURES))
        warnings.append(
            "OHLC is synthetic or inconsistent on this source, so candle-shape features "
            "measure the vendor's construction rather than the market. They are dropped; "
            "close-based features are unaffected."
        )

    # One number, weighted towards the things that decide whether a row can be
    # trusted at all. Deliberately simple: a score nobody can recompute by hand
    # is a score nobody will argue with.
    score = (
        0.40 * completeness
        + 0.25 * (1.0 - quality.invalid_rate)
        + 0.15 * (1.0 - duplicate_rate)
        + 0.10 * (1.0 if quality.ohlc_trustworthy else 0.0)
        + 0.10 * (1.0 - missing_spread)
    )

    return QualityReport(
        quality=quality,
        completeness=completeness,
        duplicate_rate=duplicate_rate,
        missing_volume_rate=missing_volume,
        missing_spread_rate=missing_spread,
        score=max(0.0, min(1.0, score)),
        usable=not blocking,
        blocking=tuple(blocking),
        warnings=tuple(warnings),
        unusable_features=unusable,
    )
