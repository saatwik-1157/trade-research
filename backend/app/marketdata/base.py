"""The provider interface every market-data source implements.

Two interfaces, not one, because live streaming and historical ingestion have
different failure modes and mixing them produces a class that is bad at both:

    `QuoteProvider`       top of book, now. Fails by going stale.
    `HistoricalProvider`  bars over a window. Fails by returning short.

A provider may implement either or both. MT5 does both; yfinance is historical
only and says so, rather than growing a `get_quote` that returns the last daily
close dressed up as a live price.

**Availability is reported, never assumed.** `available()` says whether this
provider can actually serve right now and why not when it cannot. A provider
that needs a Windows terminal reports that on Linux instead of raising an
ImportError somewhere deep in a request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.marketdata.types import Bar, Provider, Quote, Timeframe


class ProviderUnavailable(Exception):
    """This provider cannot serve. Carries why, and is never swallowed."""


class SymbolNotSupported(Exception):
    """This provider does not list this symbol. Never resolved to a guess."""


@dataclass(frozen=True)
class ProviderStatus:
    name: Provider
    usable: bool
    detail: str
    supports_quotes: bool
    supports_history: bool
    timeframes: tuple[Timeframe, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": str(self.name),
            "usable": self.usable,
            "detail": self.detail,
            "supports_quotes": self.supports_quotes,
            "supports_history": self.supports_history,
            "timeframes": [str(t) for t in self.timeframes],
        }


class MarketDataProvider(ABC):
    """Base for every source. Subclasses add quotes, history, or both."""

    name: Provider
    supports_quotes: bool = False
    supports_history: bool = False
    timeframes: tuple[Timeframe, ...] = ()

    @abstractmethod
    async def status(self) -> ProviderStatus:
        """Whether this provider can serve right now, and why not if it cannot.

        Must not raise. A provider whose dependency is missing reports it as a
        status; discovering that through an exception in the middle of a
        request makes an ordinary condition look like a fault.
        """

    def supports(self, timeframe: Timeframe) -> bool:
        return timeframe in self.timeframes

    def require_timeframe(self, timeframe: Timeframe) -> None:
        if not self.supports(timeframe):
            raise SymbolNotSupported(
                f"{self.name} does not serve {timeframe}; it serves "
                f"{', '.join(str(t) for t in self.timeframes)}"
            )


class QuoteProvider(MarketDataProvider):
    supports_quotes = True

    @abstractmethod
    async def get_quote(self, provider_symbol: str) -> Quote:
        """Top of book for one symbol, as the provider names it.

        Takes the *provider's* symbol, not the internal code. Translation is
        the symbol layer's job (L11) and doing it here would put a second
        mapping table in the codebase.
        """


class HistoricalProvider(MarketDataProvider):
    supports_history = True

    @abstractmethod
    async def get_bars(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Up to `limit` bars ending at or before `end`, oldest first.

        Returning fewer than asked is normal and is not an error: the provider
        may not have the history. Returning bars the caller did not ask for --
        a different timeframe, a forming bar presented as complete -- is a
        defect, and the service validates for it rather than trusting.
        """
