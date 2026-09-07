"""Validation for incoming market data.

The governing rule: **flag, never silently modify.** A corrupt bar that is
quietly repaired is a bar whose repair nobody can find later, and this project
has a measured example of what that costs -- `tools/market.validate_ohlcv`
found that Yahoo's FX series puts the close outside the day's range on 2-6% of
bars, and any pattern defined on candle shadows then fires unconditionally. The
repository's own forex pattern study produced a t-statistic of 28 from exactly
that artefact.

So this module separates two things the toolkit does together:

  * `inspect_series` **measures** and reports, changing nothing.
  * `clamp_ohlc` repairs one bar, and is only ever called by a caller that has
    read the report and decided to.

Staleness is not one number. A D1 bar 20 minutes old is fresh; an M1 bar 20
minutes old is not; and a stale FX feed at the weekend is a closed market
rather than a fault. `staleness_limit` derives a threshold from the timeframe
instead of hardcoding one, and the caller says which session applies.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal

from app.marketdata.types import Bar, Quote, SeriesQuality, Timeframe, seconds_of

# How far past a bar's own length a series may run before its newest bar is
# called stale. Two intervals allows one late print without crying wolf, which
# is the same allowance `app.workers.base` gives a heartbeat.
STALE_INTERVALS = 2.0
MIN_STALE_SECONDS = 30.0

# A stamp this far ahead of now is a clock problem, not a fast feed.
FUTURE_TOLERANCE_SECONDS = 60.0


class MarketDataError(Exception):
    """Data that cannot be accepted. Never downgraded to a default value."""


def ohlc_is_consistent(bar: Bar) -> bool:
    """low <= open, close <= high, and high >= low. All four, or it is not a bar."""
    return (
        bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high and bar.high >= bar.low
    )


def clamp_ohlc(bar: Bar) -> Bar:
    """Stretch the extremes to contain the open and close.

    The close is the better-sourced value on the feeds where this happens; the
    extremes are what get stretched. Callers use this only after reading a
    quality report -- it is a repair, and a repair that happens automatically
    is a repair nobody knows about.
    """
    high = max(bar.high, bar.open, bar.close)
    low = min(bar.low, bar.open, bar.close)
    if high == bar.high and low == bar.low:
        return bar
    return Bar(
        symbol=bar.symbol,
        provider=bar.provider,
        timeframe=bar.timeframe,
        bar_time=bar.bar_time,
        open=bar.open,
        high=high,
        low=low,
        close=bar.close,
        volume=bar.volume,
        spread=bar.spread,
        spread_availability=bar.spread_availability,
        complete=bar.complete,
    )


def check_bar(bar: Bar, *, now: datetime | None = None) -> list[str]:
    """Everything wrong with one bar, as a list of reasons. Empty means clean."""
    problems: list[str] = []
    if not ohlc_is_consistent(bar):
        problems.append(f"OHLC inconsistent: o={bar.open} h={bar.high} l={bar.low} c={bar.close}")
    for name in ("open", "high", "low", "close"):
        value: Decimal = getattr(bar, name)
        if value <= 0:
            # A price of zero or below is not a cheap instrument, it is a
            # missing field that arrived as a number.
            problems.append(f"{name} is not a positive price: {value}")
    if bar.volume is not None and bar.volume < 0:
        problems.append(f"volume is negative: {bar.volume}")
    if bar.spread is not None and bar.spread < 0:
        problems.append(f"spread is negative: {bar.spread}")
    if now is not None:
        ahead = (bar.bar_time - now).total_seconds()
        if ahead > FUTURE_TOLERANCE_SECONDS:
            problems.append(f"bar_time is {int(ahead)}s in the future")
    return problems


def check_quote(quote: Quote, *, now: datetime | None = None) -> list[str]:
    problems: list[str] = []
    if quote.bid is not None and quote.bid <= 0:
        problems.append(f"bid is not a positive price: {quote.bid}")
    if quote.ask is not None and quote.ask <= 0:
        problems.append(f"ask is not a positive price: {quote.ask}")
    if quote.bid is not None and quote.ask is not None:
        if quote.ask < quote.bid:
            # A crossed book is a real thing for a microsecond on a real venue
            # and a bug everywhere else. Either way it is not something to size
            # an order from.
            problems.append(f"crossed quote: ask {quote.ask} below bid {quote.bid}")
        elif quote.ask == quote.bid:
            # A zero spread is not a free trade, it is a book that is not
            # two-sided -- a frozen feed, a closed session or a synthetic
            # quote. Costing a trade from it understates the spread to zero,
            # which is the direction that flatters every result.
            problems.append(
                f"zero spread: bid and ask are both {quote.bid}; not a two-sided market"
            )
    if now is not None and quote.age_seconds(now) < -FUTURE_TOLERANCE_SECONDS:
        problems.append("quote is stamped in the future")
    return problems


def staleness_limit(timeframe: Timeframe) -> float:
    """How old the newest bar may be before the series is called stale.

    Derived from the timeframe rather than fixed: a D1 bar twenty minutes old
    is fresh and an M1 bar twenty minutes old is not, so one universal
    threshold would be wrong for every instrument but one.
    """
    return max(seconds_of(timeframe) * STALE_INTERVALS, MIN_STALE_SECONDS)


def is_stale(
    newest: datetime, timeframe: Timeframe, now: datetime, *, market_open: bool | None = None
) -> bool:
    """Stale means "we should have seen a newer bar by now".

    `market_open=False` returns False whatever the age: a quiet FX feed at the
    weekend is a closed market, and reporting it as a fault would train an
    operator to ignore the alert that matters. `None` means the caller does not
    know, and an unknown session is treated as open -- an unnoticed dead feed
    is worse than a false alarm.
    """
    if market_open is False:
        return False
    return (now - newest).total_seconds() > staleness_limit(timeframe)


def find_duplicates(bars: Sequence[Bar]) -> list[datetime]:
    """Bar times that appear more than once.

    Ingestion must tolerate a duplicate -- a provider retry or a reconnect
    replays them -- so this reports rather than raises. The database's unique
    key on (provider, symbol, timeframe, bar_time) is what actually prevents a
    second row.
    """
    seen: set[datetime] = set()
    repeated: list[datetime] = []
    for bar in bars:
        if bar.bar_time in seen:
            repeated.append(bar.bar_time)
        else:
            seen.add(bar.bar_time)
    return repeated


def find_out_of_order(bars: Sequence[Bar]) -> list[datetime]:
    """Bars that arrived before a bar already seen.

    Reported, not reordered in place. For *historical* series the caller sorts,
    because a provider paging backwards legitimately returns descending blocks.
    For a *live* stream an out-of-order bar is a fault and the caller decides;
    silently reordering a live feed hides a provider problem that will matter
    later.
    """
    late: list[datetime] = []
    previous: datetime | None = None
    for bar in bars:
        if previous is not None and bar.bar_time < previous:
            late.append(bar.bar_time)
        previous = bar.bar_time
    return late


def find_gaps(bars: Sequence[Bar], timeframe: Timeframe) -> int:
    """Count missing slots between consecutive bars.

    Counted, never filled. A synthesised bar is indistinguishable from a real
    one by eye, which is the whole reason this project exists. Note that a
    market closing produces gaps that are not faults -- an FX weekend is 48
    hours of them -- so the count is a figure to interpret, not an error.
    """
    if len(bars) < 2:
        return 0
    step = seconds_of(timeframe)
    missing = 0
    for previous, current in zip(bars, bars[1:], strict=False):
        delta = (current.bar_time - previous.bar_time).total_seconds()
        if delta > step:
            missing += int(delta // step) - 1
    return missing


def open_equals_close_rate(bars: Sequence[Bar]) -> float:
    """The share of bars whose open exactly equals the close.

    The sharper tell of a synthesised series. Real bars almost never do it
    (0.4% on equities and futures, 0% on crypto); Yahoo's FX series does on
    ~40% of bars, because those opens are filled in rather than observed.
    """
    if not bars:
        return 0.0
    return sum(1 for b in bars if b.open == b.close) / len(bars)


def inspect_series(
    bars: Sequence[Bar], timeframe: Timeframe, *, now: datetime | None = None
) -> SeriesQuality:
    """Measure a series. Changes nothing and returns what it found."""
    invalid = sum(1 for b in bars if not ohlc_is_consistent(b))
    future = 0
    if now is not None:
        future = sum(
            1 for b in bars if (b.bar_time - now).total_seconds() > FUTURE_TOLERANCE_SECONDS
        )
    quality = SeriesQuality(
        bars=len(bars),
        invalid_ohlc=invalid,
        duplicates=len(find_duplicates(bars)),
        out_of_order=len(find_out_of_order(bars)),
        gaps=find_gaps(bars, timeframe),
        future_stamped=future,
        open_equals_close_rate=open_equals_close_rate(bars),
    )
    notes: list[str] = []
    if not quality.ohlc_trustworthy and bars:
        notes.append(
            "OHLC is synthetic or inconsistent on this source. Close-based analysis is "
            "fine; anything defined on the open, the body or the shadows is measuring "
            "the vendor's construction and must not be run."
        )
    if quality.duplicates:
        notes.append(f"{quality.duplicates} duplicate bar times; ingestion is idempotent on key")
    if quality.out_of_order:
        notes.append(f"{quality.out_of_order} bars arrived out of order")
    if quality.future_stamped:
        notes.append(f"{quality.future_stamped} bars stamped in the future; check the clock")
    return SeriesQuality(
        bars=quality.bars,
        invalid_ohlc=quality.invalid_ohlc,
        duplicates=quality.duplicates,
        out_of_order=quality.out_of_order,
        gaps=quality.gaps,
        future_stamped=quality.future_stamped,
        open_equals_close_rate=quality.open_equals_close_rate,
        notes=notes,
    )


def deduplicate(bars: Iterable[Bar]) -> list[Bar]:
    """Keep the last bar seen for each bar time, sorted ascending.

    Last wins because a provider re-sending a bar is usually correcting it. The
    ordering is applied here rather than assumed anywhere downstream.
    """
    by_time: dict[datetime, Bar] = {}
    for bar in bars:
        by_time[bar.bar_time] = bar
    return [by_time[t] for t in sorted(by_time)]
