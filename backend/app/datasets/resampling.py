"""OHLCV aggregation, with deterministic boundaries and no future bars.

The module is `resampling` and the function is `resample`, deliberately: a
module and a callable sharing one name inside a package means
`from app.datasets import resample` gives you whichever the `__init__` imported
last, which is the kind of ambiguity that is only ever discovered from a stack
trace.

Section 13. Open is the first, high the maximum, low the minimum, close the
last, volume the sum — and the whole risk lives in which bars are "the" bars,
not in that arithmetic.

**A boundary is computed from the timestamp, never from the position in the
list.** Bucketing by "every fourth bar" gives a different answer depending on
where the series happens to start, so two runs over overlapping windows would
produce different H4 candles for the same hour. Here a bar's bucket is a pure
function of its own `bar_time`, so the same input bar always lands in the same
output candle no matter what else is in the list.

**A partial bucket is marked, not hidden.** The last bucket of a series
usually has fewer source bars than it should, because the period has not
finished. Emitting it as a complete candle would mean a feature computed on it
reads a close that is not the close — the single most common way a "no
leakage" pipeline leaks. `complete=False` travels on the Bar, and the dataset
builder drops incomplete candles.

**Aggregation never crosses a gap silently.** The count of source bars behind
each output candle is checked against how many that period should hold, and a
short bucket is reported through the series quality rather than smoothed over.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.types import Availability, Bar, Timeframe, seconds_of


class ResampleError(Exception):
    """An aggregation that cannot be performed. Refused, never approximated."""


# Which timeframes may be built from which. Only exact multiples: an H4 from
# H1 is four bars, and an H4 from M5 is 48 -- but a D1 from H4 is six on a
# 24-hour market and something else on one that closes, so the daily bucket is
# only offered from timeframes that divide a day exactly.
_ALLOWED: dict[Timeframe, tuple[Timeframe, ...]] = {
    Timeframe.M5: (Timeframe.M1,),
    Timeframe.M15: (Timeframe.M1, Timeframe.M5),
    Timeframe.M30: (Timeframe.M1, Timeframe.M5, Timeframe.M15),
    Timeframe.H1: (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.M30),
    Timeframe.H4: (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1),
    Timeframe.D1: (Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4),
}


def bucket_start(at: datetime, target: Timeframe) -> datetime:
    """The open time of the candle `at` belongs to.

    Anchored to the UTC epoch for intraday periods and to UTC midnight for D1,
    so the answer depends only on the timestamp. A broker whose day starts at
    its own 00:00 is a different convention, and converting to it is the
    adapter's job -- `server_day_start()` in the toolkit exists for exactly
    that, and guessing it here would put the same bar in two different days
    depending on who asked.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if target is Timeframe.D1:
        return at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if target is Timeframe.W1:
        day = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    seconds = seconds_of(target)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((at.astimezone(UTC) - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - elapsed % seconds)


def resample(bars: list[Bar], target: Timeframe) -> list[Bar]:
    """Aggregate an ordered series into `target` candles.

    Bars must already be sorted and of one timeframe, symbol and provider; a
    mixed list is refused rather than grouped, because silently aggregating two
    providers' bars into one candle would average two different books — the
    error `market_bars` keys `provider` into its identity to prevent.
    """
    if not bars:
        return []

    source = bars[0].timeframe
    symbol = bars[0].symbol
    provider = bars[0].provider
    for bar in bars:
        if bar.timeframe is not source:
            raise ResampleError(
                f"mixed timeframes: {source} and {bar.timeframe}. Aggregating them would "
                "produce a candle nobody can describe."
            )
        if bar.symbol != symbol or bar.provider is not provider:
            raise ResampleError(
                "mixed symbols or providers in one series. Two providers' bars are "
                "different measurements of different books and are never merged."
            )

    if target is source:
        return list(bars)
    allowed = _ALLOWED.get(target, ())
    if source not in allowed:
        raise ResampleError(
            f"{source} cannot be aggregated into {target}. Allowed sources are "
            f"{', '.join(str(t) for t in allowed) or 'none'}; anything else would need "
            "a partial bar and would not be the candle it claims to be."
        )

    expected = seconds_of(target) // seconds_of(source)
    buckets: dict[datetime, list[Bar]] = {}
    order: list[datetime] = []
    for bar in bars:
        start = bucket_start(bar.bar_time, target)
        if start not in buckets:
            buckets[start] = []
            order.append(start)
        buckets[start].append(bar)

    out: list[Bar] = []
    for start in order:
        group = buckets[start]
        volumes = [b.volume for b in group]
        # A single missing volume makes the sum a different quantity from a
        # complete one, so the aggregate is absent rather than partial. The
        # start value is Decimal("0") rather than the int 0 that `sum` defaults
        # to: a mixed-type total is a float away from losing the exactness the
        # whole column is Numeric for.
        volume: Decimal | None = (
            sum((v for v in volumes if v is not None), Decimal("0"))
            if all(v is not None for v in volumes)
            else None
        )
        spreads = [b.spread for b in group if b.spread is not None]
        out.append(
            Bar(
                symbol=symbol,
                provider=provider,
                timeframe=target,
                bar_time=start,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=volume,
                # The period's own spread is not the sum of its parts; the
                # widest reading in the window is the one a bracket has to
                # survive, and `cost_profile.py` measures cost that way.
                spread=max(spreads) if spreads else None,
                spread_availability=(
                    Availability.available if spreads else Availability.not_available
                ),
                complete=len(group) == expected and all(b.complete for b in group),
            )
        )
    return out


def aggregation_report(bars: list[Bar], target: Timeframe) -> dict[str, object]:
    """What the aggregation did, including which candles are short.

    Reported rather than repaired: a short bucket usually means the market was
    closed, which is a real fact about the series and not a defect to fill in.
    """
    result = resample(bars, target)
    incomplete = [b.bar_time.isoformat() for b in result if not b.complete]
    return {
        "source_timeframe": str(bars[0].timeframe) if bars else None,
        "target_timeframe": str(target),
        "source_bars": len(bars),
        "candles": len(result),
        "incomplete_candles": len(incomplete),
        "incomplete_times": incomplete[:20],
        "policy": "incomplete candles are marked and dropped by the builder, never padded",
    }
