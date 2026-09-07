"""A labelled development simulator.

Every bar and quote it produces carries `provider="simulator"`, and that label
travels all the way to the API response and the WebSocket frame. Nothing
downstream can mistake a simulated price for an observed one without ignoring
a field that is right there.

Two constraints make it safe rather than merely honest:

  * **It refuses to construct in production.** A simulator reachable on a
    production host is one configuration mistake away from a chart that looks
    like a market.
  * **It is deterministic.** Same seed, same series. A simulator that
    sometimes produced different data would make a test's outcome depend on a
    coin flip, and a failure that reproduces is worth more than one that
    surprises.

It is a random walk and claims nothing else. It has no drift, no volatility
clustering and no session structure, so it must never be used to evaluate a
strategy -- fitting to it would measure the generator. It exists so a UI panel
and a WebSocket subscription can be exercised without a broker.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.base import HistoricalProvider, ProviderStatus, QuoteProvider
from app.marketdata.types import Availability, Bar, Provider, Quote, Timeframe, seconds_of

ALL_TIMEFRAMES: tuple[Timeframe, ...] = tuple(Timeframe)

WARNING = (
    "SIMULATED DATA. A random walk with no drift, no volatility clustering and "
    "no session structure. Never evaluate a strategy against it."
)


class SimulatorMarketData(QuoteProvider, HistoricalProvider):
    name = Provider.simulator
    supports_quotes = True
    supports_history = True
    timeframes = ALL_TIMEFRAMES

    def __init__(self, *, environment: str = "development", seed: int = 7) -> None:
        if environment == "production":
            raise RuntimeError(
                "the market-data simulator refuses to run in production; a simulated "
                "chart on a production host is one mistake away from being believed"
            )
        self.environment = environment
        self.seed = seed

    async def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, WARNING, True, True, ALL_TIMEFRAMES)

    def _rng(self, provider_symbol: str, timeframe: Timeframe) -> random.Random:
        # Seeded from the request, so the same symbol and timeframe always
        # produce the same series.
        return random.Random(f"{self.seed}:{provider_symbol}:{timeframe}")

    def _walk(
        self, provider_symbol: str, timeframe: Timeframe, count: int, end: datetime
    ) -> list[Bar]:
        rng = self._rng(provider_symbol, timeframe)
        step = seconds_of(timeframe)
        price = Decimal("1.10000")
        bars: list[Bar] = []
        start = end - timedelta(seconds=step * count)
        for i in range(count):
            move = Decimal(str(round(rng.uniform(-0.0015, 0.0015), 5)))
            open_ = price
            close = max(Decimal("0.00001"), open_ + move)
            high = max(open_, close) + Decimal(str(round(rng.uniform(0, 0.0008), 5)))
            low = min(open_, close) - Decimal(str(round(rng.uniform(0, 0.0008), 5)))
            low = max(low, Decimal("0.00001"))
            bars.append(
                Bar(
                    symbol=provider_symbol,
                    provider=self.name,
                    timeframe=timeframe,
                    bar_time=start + timedelta(seconds=step * i),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal(rng.randint(50, 500)),
                    # The simulator does not model a spread. Reporting one
                    # would be a fabricated cost, and a fabricated cost is the
                    # one number a backtest must never be handed.
                    spread=None,
                    spread_availability=Availability.not_available,
                    complete=True,
                )
            )
            price = close
        return bars

    async def get_bars(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
        end: datetime | None = None,
    ) -> list[Bar]:
        self.require_timeframe(timeframe)
        anchor = end or datetime.now(UTC)
        return self._walk(provider_symbol, timeframe, limit, anchor)

    async def get_quote(self, provider_symbol: str) -> Quote:
        bar = self._walk(provider_symbol, Timeframe.M1, 1, datetime.now(UTC))[0]
        half = Decimal("0.00005")
        now = datetime.now(UTC)
        return Quote(
            symbol=provider_symbol,
            provider=self.name,
            at=now,
            received_at=now,
            bid=bar.close - half,
            ask=bar.close + half,
            last=bar.close,
            spread_availability=Availability.derived,
        )
