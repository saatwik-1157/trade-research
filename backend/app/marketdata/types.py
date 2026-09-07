"""The normalized market-data model.

One internal shape, whatever the provider. The rule that shapes every field:
**a value that was not supplied is None and stays None.** Not zero, not the
previous bar's close, not an interpolation. A spread of `None` and a spread of
`0` are different claims, and the second one is a claim this broker's data has
already been shown to make falsely -- `cost_profile.py` records that bars
reporting a 0 spread are unrecorded rather than free, and averaging them in
halves the apparent cost of trading.

That is why `Bar` and `Quote` carry an explicit availability rather than
relying on a caller to notice a null. A field is:

    AVAILABLE      the provider supplied it
    NOT_AVAILABLE  the provider does not supply it at all
    DERIVED        we computed it from other fields, and say so

`spread` on a Bar is the clearest case. MT5 records it per bar; yfinance has
no concept of it; and where we compute `ask - bid` from a quote that is
DERIVED, not observed. Downstream cost models must be able to tell those apart,
because a cost model fed a fabricated spread produces a backtest that is wrong
in the expensive direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Provider(StrEnum):
    """Where data came from. Matches `symbol_mappings.provider`."""

    mt5 = "mt5"
    yfinance = "yfinance"
    ccxt = "ccxt"
    tradingview = "tradingview"
    simulator = "simulator"


class Availability(StrEnum):
    available = "available"
    not_available = "not_available"
    derived = "derived"


class Timeframe(StrEnum):
    """Internal timeframe names. Provider-specific names map onto these."""

    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"


SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
    Timeframe.W1: 604800,
}


class TimeframeError(Exception):
    """An unsupported timeframe. Never silently mapped to a nearby one."""


def parse_timeframe(value: str) -> Timeframe:
    try:
        return Timeframe(value.strip().upper())
    except (AttributeError, ValueError) as exc:
        raise TimeframeError(
            f"unknown timeframe {value!r}; expected one of {', '.join(str(t) for t in Timeframe)}"
        ) from exc


def seconds_of(timeframe: Timeframe) -> int:
    return SECONDS[timeframe]


@dataclass(frozen=True)
class Quote:
    """A top-of-book snapshot.

    `at` is the provider's own timestamp, in UTC. `received_at` is when we saw
    it. Keeping both is what makes staleness measurable rather than assumed:
    a feed that stopped moving and a process that stopped reading look
    identical from one timestamp.
    """

    symbol: str
    provider: Provider
    at: datetime
    received_at: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    volume: Decimal | None = None
    spread_availability: Availability = Availability.not_available

    @property
    def spread(self) -> Decimal | None:
        """ask - bid, or None. Derived, and the availability field says so."""
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    def age_seconds(self, now: datetime) -> float:
        return (now - self.at).total_seconds()

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "provider": str(self.provider),
            "at": self.at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "last": str(self.last) if self.last is not None else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "spread_availability": str(
                Availability.derived if self.spread is not None else self.spread_availability
            ),
            "volume": str(self.volume) if self.volume is not None else None,
        }


@dataclass(frozen=True)
class Bar:
    """One OHLCV candle, normalized.

    `bar_time` is the OPEN time of the bar, in UTC, and that convention is
    load-bearing: MT5 stamps a bar with its open and yfinance indexes by the
    period start, but a provider that stamped closes would shift every signal
    by one bar. Adapters convert; nothing downstream guesses.
    """

    symbol: str
    provider: Provider
    timeframe: Timeframe
    bar_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    spread: Decimal | None = None
    spread_availability: Availability = Availability.not_available
    # True only when the provider says the bar has closed. A forming bar must
    # never reach a strategy: acting on it is lookahead in the one direction
    # that always flatters a backtest.
    complete: bool = True

    @property
    def key(self) -> tuple[str, str, str, datetime]:
        """The identity of a bar. Ingestion is idempotent on exactly this."""
        return (str(self.provider), self.symbol, str(self.timeframe), self.bar_time)

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "provider": str(self.provider),
            "timeframe": str(self.timeframe),
            "bar_time": self.bar_time.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume) if self.volume is not None else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "spread_availability": str(self.spread_availability),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class SeriesQuality:
    """What was found wrong with a series, reported rather than repaired away.

    Carries the two measurements `tools/market.validate_ohlcv` already makes,
    because they are the difference between a usable series and one that
    manufactures findings. In this repository's own forex pattern study an
    unvalidated Yahoo FX series produced a t-statistic of 28 from bars whose
    close sat outside the day's range -- an artefact that reads exactly like a
    spectacular edge.
    """

    bars: int
    invalid_ohlc: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    future_stamped: int = 0
    open_equals_close_rate: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def invalid_rate(self) -> float:
        return self.invalid_ohlc / self.bars if self.bars else 0.0

    @property
    def ohlc_trustworthy(self) -> bool:
        """False means: use the close, never the body or the shadows.

        The thresholds are the ones `tools/market.validate_ohlcv` measured --
        equities and crypto from the same provider show zero violations while
        Yahoo's FX series synthesises ~40% of its opens, so this is a
        source-and-asset-class property rather than a universal one.
        """
        return self.invalid_rate < 0.005 and self.open_equals_close_rate < 0.10

    def as_dict(self) -> dict[str, object]:
        return {
            "bars": self.bars,
            "invalid_ohlc": self.invalid_ohlc,
            "invalid_rate": round(self.invalid_rate, 5),
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "gaps": self.gaps,
            "future_stamped": self.future_stamped,
            "open_equals_close_rate": round(self.open_equals_close_rate, 5),
            "ohlc_trustworthy": self.ohlc_trustworthy,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Series:
    """Bars plus what is wrong with them. The two travel together on purpose."""

    symbol: str
    provider: Provider
    timeframe: Timeframe
    bars: list[Bar]
    quality: SeriesQuality

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "provider": str(self.provider),
            "timeframe": str(self.timeframe),
            "bars": [b.as_dict() for b in self.bars],
            "quality": self.quality.as_dict(),
        }
