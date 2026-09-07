"""Broker adapters: one interface, several venues, no global connection.

    BrokerAdapter          the interface every venue implements
    MT5Adapter             the real terminal, demo-fenced on connect
    FakeBroker             the PAPER venue and the fault injector
    BrokerRegistry         one adapter per account, never a singleton
    validate_order         venue constraints, checked before anything is sent
    reconcile_positions    compares and reports; never repairs

The rule that shapes all of it: **a result is what the venue said, never what
we asked for.** `OrderStatus.unknown` is a first-class outcome, not an error to
tidy away, and it is resolved by reconciling against the venue rather than by
retrying.

Nothing in this package decides *whether* to trade. Risk, sizing and the OMS
are upstream; an adapter only speaks to a venue.
"""

from app.brokers.base import (
    Account,
    AccountMode,
    BrokerAdapter,
    BrokerError,
    BrokerHealth,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    Deal,
    NotConnected,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Quote,
    RefuseToTrade,
    SymbolInfo,
)
from app.brokers.fake import FakeBroker
from app.brokers.reconcile import (
    Finding,
    InternalPosition,
    Mismatch,
    Reconciliation,
    reconcile_positions,
)
from app.brokers.registry import BrokerRegistry, UnknownAccount
from app.brokers.validation import OrderRejected, ValidationReport, validate_order

__all__ = [
    "Account",
    "AccountMode",
    "BrokerAdapter",
    "BrokerError",
    "BrokerHealth",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerRegistry",
    "ConnectionState",
    "Deal",
    "FakeBroker",
    "Finding",
    "InternalPosition",
    "Mismatch",
    "NotConnected",
    "OrderRejected",
    "OrderRequest",
    "OrderResult",
    "OrderStatus",
    "Quote",
    "Reconciliation",
    "RefuseToTrade",
    "SymbolInfo",
    "UnknownAccount",
    "ValidationReport",
    "reconcile_positions",
    "validate_order",
]
