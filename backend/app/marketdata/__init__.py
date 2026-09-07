"""Market data: one normalized model, several providers, one service.

Nothing in this package can place an order. Market data is an input to a
decision, never a decision: the path from a price to a broker runs Strategy ->
Signal -> AI -> Risk -> Sizing -> OMS, and none of those is imported here.

A stale or invalid feed is reported, never acted on. Deciding what to do about
a stale price belongs to the strategy and to the risk engine, which is where
the veto lives.
"""

from app.marketdata.service import MarketDataService, default_service
from app.marketdata.types import Availability, Bar, Provider, Quote, Series, Timeframe

__all__ = [
    "Availability",
    "Bar",
    "MarketDataService",
    "Provider",
    "Quote",
    "Series",
    "Timeframe",
    "default_service",
]
