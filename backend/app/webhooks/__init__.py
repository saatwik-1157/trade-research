"""Inbound webhook gateways.

One gateway today: TradingView. It validates, authenticates, deduplicates and
records a Signal. It cannot trade -- nothing here imports a broker adapter, a
risk engine or an OMS, and a test asserts that absence rather than trusting it.

TradingView is an *alert* path, not a market-data provider. Its payloads are
never used as a substitute for the normalized feed built at L08.
"""

from app.webhooks.gateway import Outcome, Result, Unauthorized, WebhookGateway

__all__ = ["Outcome", "Result", "Unauthorized", "WebhookGateway"]
