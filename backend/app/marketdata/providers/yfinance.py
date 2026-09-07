"""yfinance history, wrapping `tools/market.get_ohlcv` unchanged.

The toolkit function is **called, not copied**, and the reason is its
`validate_ohlcv` pass. That function carries a measured finding this adapter
must not lose: Yahoo's FX series takes its close from a different snapshot
than the high and low, so 2-6% of currency bars have a close outside the day's
range, and ~40% have an open exactly equal to the close because those opens
are synthesised rather than observed. Any pattern defined on candle shadows
then fires unconditionally -- it produced a t-statistic of 28 in this
repository's own forex pattern study.

So this adapter surfaces `market.VALIDATION[ticker]` as part of the series
quality rather than returning clean-looking bars. Equities and crypto from the
same provider show zero violations, which is exactly why the rate is reported
per ticker instead of assumed.

History only. There is no `get_quote` here on purpose: a delayed daily close
dressed up as a live price is the kind of substitution the whole normalization
layer exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal

from app.marketdata.base import HistoricalProvider, ProviderStatus, ProviderUnavailable
from app.marketdata.types import Availability, Bar, Provider, Timeframe

log = logging.getLogger("app.marketdata.yfinance")

# What `get_ohlcv`'s `interval` argument accepts, mapped from internal names.
INTERVALS: dict[Timeframe, str] = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.M30: "30m",
    Timeframe.H1: "1h",
    Timeframe.D1: "1d",
    Timeframe.W1: "1wk",
}
# H4 is deliberately absent: Yahoo does not serve it, and resampling H1 into
# H4 here would produce bars no venue ever printed.
SUPPORTED: tuple[Timeframe, ...] = tuple(INTERVALS)


def _toolkit():  # noqa: ANN202 - the toolkit module, imported lazily
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    tools = os.path.join(os.path.dirname(root), "tools")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.insert(0, tools)
    import market

    return market


class YFinanceMarketData(HistoricalProvider):
    name = Provider.yfinance
    supports_history = True
    timeframes = SUPPORTED

    async def status(self) -> ProviderStatus:
        try:
            _toolkit()
        except ImportError as exc:
            return ProviderStatus(
                self.name,
                False,
                f"toolkit market module unavailable: {exc}",
                False,
                True,
                SUPPORTED,
            )
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:
            return ProviderStatus(
                self.name, False, f"yfinance is not installed: {exc}", False, True, SUPPORTED
            )
        # Reachability is not probed here: a network call on a status endpoint
        # turns a health check into a rate-limited dependency. "Importable"
        # is what this claims, and it says so.
        return ProviderStatus(
            self.name, True, "yfinance importable; network not probed", False, True, SUPPORTED
        )

    def _period_for(self, timeframe: Timeframe, limit: int) -> str:
        """Yahoo takes a period, not a bar count, and caps intraday history.

        Asking for more than the cap returns a short series with no error, so
        the period is chosen generously and the caller's `limit` is applied by
        slicing afterwards.
        """
        if timeframe in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.M30):
            return "7d" if timeframe is Timeframe.M1 else "60d"
        if timeframe is Timeframe.H1:
            return "730d"
        return "10y" if limit > 1200 else "5y"

    def _bars_sync(self, ticker: str, timeframe: Timeframe, limit: int) -> tuple[list[Bar], dict]:
        market = _toolkit()
        frame = market.get_ohlcv(
            ticker, period=self._period_for(timeframe, limit), interval=INTERVALS[timeframe]
        )
        bars: list[Bar] = []
        for stamp, row in frame.iterrows():
            at = stamp.to_pydatetime()
            bars.append(
                Bar(
                    symbol=ticker,
                    provider=self.name,
                    timeframe=timeframe,
                    bar_time=at if at.tzinfo else at.replace(tzinfo=UTC),
                    open=Decimal(str(row["Open"])),
                    high=Decimal(str(row["High"])),
                    low=Decimal(str(row["Low"])),
                    close=Decimal(str(row["Close"])),
                    volume=Decimal(str(row["Volume"])) if row.get("Volume") is not None else None,
                    # Yahoo has no spread. NOT_AVAILABLE, never zero: a zero
                    # spread is a claim that trading is free.
                    spread=None,
                    spread_availability=Availability.not_available,
                    complete=True,
                )
            )
        # The toolkit's own per-ticker validation record, carried forward
        # rather than recomputed -- it is the reason to call that function.
        report = dict(getattr(market, "VALIDATION", {}).get(ticker, {}))
        return bars[-limit:], report

    async def get_bars(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
        end: datetime | None = None,
    ) -> list[Bar]:
        self.require_timeframe(timeframe)
        if end is not None:
            raise ProviderUnavailable(
                "the yfinance adapter fetches a trailing period and cannot take an end "
                "bound; ask for more bars and slice"
            )
        bars, report = await asyncio.to_thread(self._bars_sync, provider_symbol, timeframe, limit)
        self.last_validation = report
        return bars

    #: The toolkit's validation record for the most recent fetch. Read by the
    #: service so `ohlc_trustworthy` reaches the caller.
    last_validation: dict = {}
