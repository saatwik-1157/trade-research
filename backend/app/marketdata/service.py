"""The market-data service: registry, normalization, validation, storage, events.

This is the one place the rest of the platform asks for prices. It owns four
decisions, and each is here rather than in a provider because a provider that
made them would make them differently:

1. **Which provider serves this symbol.** Resolved through the L11 mapping
   table, so `EURUSD` reaches `EURUSD.r` at MT5 and `EURUSD=X` at Yahoo. A
   symbol with no mapping for the requested provider is refused. It is never
   passed through as-is: "EURUSD" at TradingView and "EURUSD" at the broker
   are two strings that happen to look alike.

2. **Whether the data is usable.** Every series is inspected before it is
   returned, and the report travels *with* the bars rather than being logged
   somewhere the caller will not read. Nothing is repaired silently.

3. **Whether it is stale.** Derived from the timeframe, not a global constant,
   and reported rather than acted on. **A stale feed never triggers anything.**
   It is a fact attached to the answer; deciding what to do about it belongs
   to the strategy and the risk engine.

4. **What gets stored.** Bars are cached with their provenance so ingestion is
   idempotent and replay is deterministic. The provider stays the source of
   truth; this is a cache that remembers where each row came from.

**Failover is deliberately not automatic.** Step 15 of this level asks for
controlled failover, and the control is this: `fallbacks` are only consulted
when the caller passes them, the substitute provider is named in the response,
and bars from two providers are never merged into one series. Two providers'
"EURUSD" are different books with different spreads and different stamps, and
quietly swapping one for the other would put another venue's prices behind a
broker symbol -- which is the mistake that makes a backtest unfalsifiable.

**Nothing here can place an order.** Market data is an input to a decision, not
a decision. The path from a price to a broker runs Strategy -> Signal -> AI ->
Risk -> Sizing -> OMS, and none of those is imported by this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ValidationFailed
from app.marketdata.base import (
    HistoricalProvider,
    MarketDataProvider,
    ProviderStatus,
    ProviderUnavailable,
    QuoteProvider,
)
from app.marketdata.types import (
    Bar,
    Provider,
    Quote,
    Series,
    SeriesQuality,
    Timeframe,
)
from app.marketdata.validation import (
    check_bar,
    check_quote,
    deduplicate,
    inspect_series,
    is_stale,
    staleness_limit,
)
from app.models.market import MarketBar, Symbol
from app.symbols import service as symbols
from app.symbols.errors import SymbolError

log = logging.getLogger("app.marketdata")

MAX_BARS = 5000


@dataclass(frozen=True)
class QuoteResult:
    quote: Quote
    stale: bool
    problems: list[str]
    provider_used: Provider

    def as_dict(self) -> dict[str, object]:
        return {
            **self.quote.as_dict(),
            "stale": self.stale,
            "problems": list(self.problems),
            "provider_used": str(self.provider_used),
        }


@dataclass(frozen=True)
class SeriesResult:
    series: Series
    stale: bool
    provider_used: Provider
    fell_back: bool
    stored: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            **self.series.as_dict(),
            "stale": self.stale,
            "provider_used": str(self.provider_used),
            # True means the requested provider could not serve and a named
            # substitute did. Never silent: a caller comparing two runs has to
            # be able to see that the book changed underneath them.
            "fell_back": self.fell_back,
            "stored": self.stored,
        }


class MarketDataService:
    def __init__(self, providers: dict[Provider, MarketDataProvider] | None = None) -> None:
        self.providers: dict[Provider, MarketDataProvider] = providers or {}

    def register(self, provider: MarketDataProvider) -> MarketDataProvider:
        if provider.name in self.providers:
            raise ValueError(f"provider {provider.name} is already registered")
        self.providers[provider.name] = provider
        return provider

    def get(self, name: Provider) -> MarketDataProvider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise NotFound(
                f"no market-data provider {name}; registered: "
                f"{', '.join(sorted(str(p) for p in self.providers)) or 'none'}"
            ) from exc

    async def statuses(self) -> list[ProviderStatus]:
        return [await p.status() for p in self.providers.values()]

    # ------------------------------------------------------------- symbols

    async def provider_symbol(
        self, db: AsyncSession, internal_symbol: str, provider: Provider
    ) -> str:
        """Internal code -> the provider's name for it. Never a passthrough."""
        mapping = await symbols.mapping_for(db, internal_symbol, str(provider))
        return mapping.provider_symbol

    # -------------------------------------------------------------- quotes

    async def get_quote(
        self, db: AsyncSession, internal_symbol: str, provider: Provider
    ) -> QuoteResult:
        source = self.get(provider)
        if not isinstance(source, QuoteProvider):
            raise ValidationFailed(
                f"{provider} does not serve quotes; it is a historical source. "
                "A daily close returned as a live price would be a fabricated quote."
            )
        name = await self.provider_symbol(db, internal_symbol, provider)
        quote = await source.get_quote(name)
        now = datetime.now(UTC)
        problems = check_quote(quote, now=now)
        # A quote's freshness is judged against the shortest bar the platform
        # serves: anything older than that is no longer top of book.
        stale = quote.age_seconds(now) > staleness_limit(Timeframe.M1)
        if problems:
            log.warning(
                "quote failed validation",
                extra={
                    "event": "market_quote_invalid",
                    "symbol": internal_symbol,
                    "provider": str(provider),
                    "problems": problems,
                },
            )
        return QuoteResult(
            quote=Quote(
                symbol=internal_symbol,  # answer in the platform's own code
                provider=quote.provider,
                at=quote.at,
                received_at=quote.received_at,
                bid=quote.bid,
                ask=quote.ask,
                last=quote.last,
                volume=quote.volume,
                spread_availability=quote.spread_availability,
            ),
            stale=stale,
            problems=problems,
            provider_used=provider,
        )

    # ------------------------------------------------------------- history

    async def get_bars(
        self,
        db: AsyncSession,
        internal_symbol: str,
        timeframe: Timeframe,
        provider: Provider,
        *,
        limit: int = 500,
        fallbacks: tuple[Provider, ...] = (),
        market_open: bool | None = None,
    ) -> SeriesResult:
        if limit < 1 or limit > MAX_BARS:
            raise ValidationFailed(f"limit must be between 1 and {MAX_BARS}")

        attempted: list[Provider] = [provider, *fallbacks]
        last_error: Exception | None = None
        # Tracked apart from provider failures. A symbol with no mapping is a
        # caller error, not an outage, and reporting it as one sends an
        # operator to look at a terminal that is working fine.
        symbol_error: SymbolError | None = None
        for index, candidate in enumerate(attempted):
            source = self.get(candidate)
            if not isinstance(source, HistoricalProvider):
                last_error = ValidationFailed(f"{candidate} does not serve history")
                continue
            try:
                name = await self.provider_symbol(db, internal_symbol, candidate)
            except SymbolError as exc:
                # Each provider has its own mapping, so a miss here is specific
                # to this candidate and the next one may still resolve.
                symbol_error = exc
                continue
            try:
                raw = await source.get_bars(name, timeframe, limit=limit)
            except Exception as exc:  # noqa: BLE001 - recorded, then the next candidate
                last_error = exc
                log.warning(
                    "market-data provider failed",
                    extra={
                        "event": "market_provider_failed",
                        "provider": str(candidate),
                        "symbol": internal_symbol,
                        "error": type(exc).__name__,
                    },
                )
                continue

            now = datetime.now(UTC)
            # Sorted and deduplicated here, once, so no downstream consumer has
            # to assume an ordering a provider never promised.
            bars = deduplicate(raw)
            quality = inspect_series(raw, timeframe, now=now)
            quality = self._merge_provider_report(source, quality)
            stale = bool(bars) and is_stale(
                bars[-1].bar_time, timeframe, now, market_open=market_open
            )
            series = Series(
                symbol=internal_symbol,
                provider=candidate,
                timeframe=timeframe,
                bars=bars,
                quality=quality,
            )
            return SeriesResult(
                series=series,
                stale=stale,
                provider_used=candidate,
                fell_back=index > 0,
            )

        if symbol_error is not None and last_error is None:
            # Every candidate failed on the mapping and none on the wire. The
            # symbol layer's own status (404) is the truthful answer.
            raise symbol_error
        raise ProviderUnavailable(
            f"no provider served {internal_symbol} {timeframe}; tried "
            f"{', '.join(str(p) for p in attempted)}"
            + (f" (last error: {type(last_error).__name__})" if last_error else "")
        )

    @staticmethod
    def _merge_provider_report(source: MarketDataProvider, quality: SeriesQuality) -> SeriesQuality:
        """Fold an adapter's own validation record into the series quality.

        The yfinance adapter carries `tools/market.validate_ohlcv`'s per-ticker
        report, which measures the open-equals-close rate that marks a
        synthesised series. Losing it here would hand back bars that look clean
        and are not.
        """
        report = getattr(source, "last_validation", None)
        if not report:
            return quality
        notes = list(quality.notes)
        note = report.get("note")
        if note and note not in notes:
            notes.append(f"{source.name}: {note}")
        return SeriesQuality(
            bars=quality.bars,
            invalid_ohlc=max(quality.invalid_ohlc, int(report.get("invalid_bars", 0))),
            duplicates=quality.duplicates,
            out_of_order=quality.out_of_order,
            gaps=quality.gaps,
            future_stamped=quality.future_stamped,
            open_equals_close_rate=max(
                quality.open_equals_close_rate, float(report.get("open_equals_close_rate", 0.0))
            ),
            notes=notes,
        )

    # ------------------------------------------------------------- storage

    async def store_bars(
        self, db: AsyncSession, bars: list[Bar], *, symbol_id: str | None = None
    ) -> int:
        """Write bars idempotently. Returns how many rows were inserted.

        A bar that fails validation is **not stored and not repaired**; it is
        counted and logged. Storing a corrupt candle would put the artefact
        beyond the reach of the validator that caught it, and a repaired one
        would be a fabricated price with a real timestamp.

        Re-running is a no-op by the unique key, which is what makes a provider
        retry safe.
        """
        if not bars:
            return 0
        now = datetime.now(UTC)
        rows: list[dict[str, object]] = []
        rejected = 0
        for bar in bars:
            problems = check_bar(bar, now=now)
            if problems:
                rejected += 1
                log.warning(
                    "refusing to store an invalid bar",
                    extra={
                        "event": "market_bar_rejected",
                        "symbol": bar.symbol,
                        "provider": str(bar.provider),
                        "bar_time": bar.bar_time.isoformat(),
                        "problems": problems,
                    },
                )
                continue
            if not bar.complete:
                # A forming bar changes under the reader. Storing it would make
                # a cached series disagree with itself between runs, and a
                # strategy that saw it would be reading the future.
                rejected += 1
                continue
            rows.append(
                {
                    "provider": str(bar.provider),
                    "provider_symbol": bar.symbol,
                    "symbol_id": symbol_id,
                    "timeframe": str(bar.timeframe),
                    "bar_time": bar.bar_time.replace(tzinfo=None),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "spread": bar.spread,
                    "spread_availability": str(bar.spread_availability),
                    "ingested_at": now.replace(tzinfo=None),
                }
            )
        if not rows:
            return 0

        dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
        if dialect == "postgresql":
            statement = (
                pg_insert(MarketBar)
                .values(rows)
                .on_conflict_do_nothing(constraint="market_bar_identity")
            )
            result = await db.execute(statement)
            await db.flush()
            # ON CONFLICT DO NOTHING reports how many rows it actually
            # inserted, which is the figure the caller wants: a replayed
            # fetch returns 0 rather than the number of bars offered.
            return int(getattr(result, "rowcount", 0) or 0)

        # SQLite in tests: no ON CONFLICT helper on this construct, so the
        # existing keys are read first. Same outcome, different dialect.
        inserted = 0
        for row in rows:
            exists = await db.scalar(
                select(MarketBar.id).where(
                    MarketBar.provider == row["provider"],
                    MarketBar.provider_symbol == row["provider_symbol"],
                    MarketBar.timeframe == row["timeframe"],
                    MarketBar.bar_time == row["bar_time"],
                )
            )
            if exists is None:
                db.add(MarketBar(**row))
                inserted += 1
        await db.flush()
        return inserted

    async def stored_bars(
        self,
        db: AsyncSession,
        provider: Provider,
        provider_symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
    ) -> list[MarketBar]:
        statement = (
            select(MarketBar)
            .where(
                MarketBar.provider == str(provider),
                MarketBar.provider_symbol == provider_symbol,
                MarketBar.timeframe == str(timeframe),
            )
            .order_by(MarketBar.bar_time.desc())
            .limit(min(limit, MAX_BARS))
        )
        rows = list((await db.scalars(statement)).all())
        return list(reversed(rows))

    async def symbol_id_for(self, db: AsyncSession, internal_symbol: str) -> str | None:
        row = await db.scalar(select(Symbol).where(Symbol.code == internal_symbol.upper()))
        return row.id if row else None


def default_service(environment: str = "development") -> MarketDataService:
    """The providers this platform registers, in preference order.

    MT5 first because it is the venue the platform actually trades at, so its
    prices are the ones an order would meet. yfinance is research history for
    equities. The simulator is registered only outside production, and refuses
    to construct there anyway.
    """
    from app.marketdata.providers.mt5 import MT5MarketData
    from app.marketdata.providers.yfinance import YFinanceMarketData

    service = MarketDataService()
    service.register(MT5MarketData())
    service.register(YFinanceMarketData())
    if environment != "production":
        from app.marketdata.providers.simulator import SimulatorMarketData

        service.register(SimulatorMarketData(environment=environment))
    return service
