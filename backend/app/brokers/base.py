"""BrokerAdapter: the one interface every venue implements.

Twelve methods, and one rule that shapes all of them: **a result is what the
venue said, never what we asked for.** `place_order` returns the fill the
venue reported, or `UNKNOWN` when we cannot tell. It never returns success
because the call did not raise.

`OrderResult.status` has three values for the same reason
`app.positions.executor.CloseStatus` does:

    ACCEPTED   the venue took it, and said so
    REJECTED   the venue refused, and said so
    UNKNOWN    we do not know

UNKNOWN is not an error case to tidy away. An IPC timeout after `order_send`
looks exactly like a rejection from the caller's side, and this project has a
live example of a figure the tool chose being logged as a figure the server
confirmed. The OMS resolves UNKNOWN by reconciling against the venue; nothing
retries it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.auth.models import utcnow
from app.brokers.reconcile import InternalPosition, Reconciliation, reconcile_positions


class BrokerError(Exception):
    """A broker problem the caller must handle, never swallow."""


class NotConnected(BrokerError):
    pass


class RefuseToTrade(BrokerError):
    """The safety fence blocked this. Never relaxed to make a run work."""


class ConnectionState(StrEnum):
    """Where the link to the venue is.

    `connected` is only ever set after a call that the venue answered. It is
    never set because a connect function returned without raising: a terminal
    that is running and not logged in returns cleanly and holds nothing.

    `degraded` and `reconnecting` are distinct on purpose. Degraded means the
    link answers but not properly -- a stale feed, a terminal with trading
    disabled -- and reads are still worth something. Reconnecting means the
    link is gone and we are trying. Collapsing them would let a caller treat a
    half-working venue as a dead one, or worse, the reverse.
    """

    disconnected = "disconnected"
    connecting = "connecting"
    connected = "connected"
    degraded = "degraded"
    reconnecting = "reconnecting"
    reconciling = "reconciling"
    error = "error"


class OrderStatus(StrEnum):
    accepted = "accepted"
    rejected = "rejected"
    unknown = "unknown"


class AccountMode(StrEnum):
    """What the venue reports, not what we hope."""

    demo = "demo"
    contest = "contest"
    real = "real"
    unknown = "unknown"


@dataclass(frozen=True)
class BrokerHealth:
    """What the adapter observed, not what it hopes.

    `trade_allowed` is the venue's own flag. A terminal can be connected and
    logged in with trading disabled on the account or the symbol, and reporting
    that as healthy would let an order be attempted that cannot succeed.
    """

    name: str
    state: ConnectionState
    detail: str
    mode: str
    account_mode: AccountMode = AccountMode.unknown
    trade_allowed: bool = False
    latency_ms: float | None = None
    last_error: str | None = None
    reconnects: int = 0

    @property
    def usable(self) -> bool:
        return self.state is ConnectionState.connected

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": str(self.state),
            "detail": self.detail,
            "mode": self.mode,
            "account_mode": str(self.account_mode),
            "trade_allowed": self.trade_allowed,
            "latency_ms": self.latency_ms,
            "last_error": self.last_error,
            "reconnects": self.reconnects,
            "usable": self.usable,
        }


@dataclass(frozen=True)
class Account:
    login: str
    server: str
    currency: str
    balance: Decimal
    equity: Decimal
    margin: Decimal | None = None
    free_margin: Decimal | None = None
    mode: AccountMode = AccountMode.unknown
    trade_allowed: bool = False


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    at: datetime

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    digits: int
    point: Decimal
    contract_size: Decimal | None = None
    tick_size: Decimal | None = None
    tick_value: Decimal | None = None
    volume_min: Decimal | None = None
    volume_max: Decimal | None = None
    volume_step: Decimal | None = None


@dataclass(frozen=True)
class BrokerPosition:
    position_id: str
    symbol: str
    side: str  # "long" | "short"
    volume: Decimal
    entry_price: Decimal
    opened_at: datetime
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    profit: Decimal | None = None
    swap: Decimal | None = None
    magic: int | None = None


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    symbol: str
    side: str
    volume: Decimal
    price: Decimal | None
    state: str
    placed_at: datetime
    magic: int | None = None


@dataclass(frozen=True)
class Deal:
    """One leg of a round trip, as the venue recorded it."""

    deal_id: str
    position_id: str
    symbol: str
    entry: str  # "in" | "out" | "inout"
    volume: Decimal
    price: Decimal
    at: datetime
    profit: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    swap: Decimal = Decimal(0)
    magic: int | None = None


@dataclass(frozen=True)
class OrderRequest:
    """What the OMS asks for. Prices are absolute levels, as MT5 expects."""

    symbol: str
    side: str  # "buy" | "sell"
    volume: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    comment: str = ""
    magic: int | None = None
    # Carried through so a venue result can be tied back to its intent.
    intent_id: str = ""


@dataclass(frozen=True)
class OrderResult:
    status: OrderStatus
    detail: str
    order_id: str | None = None
    position_id: str | None = None
    #: The venue's ticket for the EXECUTION, as distinct from the order that
    #: asked for it and the position it belongs to. It is what identifies one
    #: fill uniquely, so it is what a fill book deduplicates by: two genuine
    #: partial fills of the same size at the same price differ in nothing else.
    #: None from a venue that does not report one, and the fill then falls back
    #: to the position or order id -- coarser, but never invented.
    deal_id: str | None = None
    # The venue's fill. None on anything but a confirmed acceptance.
    fill_price: Decimal | None = None
    filled_volume: Decimal | None = None
    filled_at: datetime | None = None
    retcode: int | None = None
    # Where the number came from, so a simulated fill can never be mistaken
    # for a broker one downstream.
    fill_source: str = "unknown"
    #: What the ACCOUNT received for this fill, in account currency, as the
    #: venue booked it -- profit plus swap plus commission. None when the venue
    #: did not say, which is a gap to report rather than a number to derive:
    #: `(fill - entry) * quantity` omits the contract size and is wrong for
    #: every instrument whose contract size is not 1.
    realized_pnl: Decimal | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_accepted(self) -> bool:
        return self.status is OrderStatus.accepted

    @property
    def is_unknown(self) -> bool:
        return self.status is OrderStatus.unknown


class BrokerAdapter(ABC):
    """Every venue implements exactly this.

    `name` identifies the implementation and `mode` says which of paper, demo
    or live it represents. Both appear in the journal, so a row always records
    which venue produced it.
    """

    name: str = "adapter"
    mode: str = "paper"

    #: Incremented by an adapter each time it re-establishes a lost link.
    #: Reported by `health()` so a link that keeps dropping is visible as a
    #: pattern rather than as a series of unrelated blips.
    reconnects: int = 0

    # ---------------------------------------------------------- connection
    @abstractmethod
    async def connect(self) -> Account: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def state(self) -> ConnectionState: ...

    # ---------------------------------------------------------------- read
    @abstractmethod
    async def get_account(self) -> Account: ...

    @abstractmethod
    async def get_symbols(self) -> list[SymbolInfo]: ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def get_positions(self, magic: int | None = None) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_orders(self, magic: int | None = None) -> list[BrokerOrder]: ...

    @abstractmethod
    async def get_order_history(
        self, since: datetime, until: datetime | None = None, magic: int | None = None
    ) -> list[Deal]: ...

    # --------------------------------------------------------------- write
    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def modify_order(
        self,
        position_id: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    async def close_position(
        self, position_id: str, volume: Decimal | None = None
    ) -> OrderResult: ...

    # ------------------------------------------------------------- health
    #
    # Concrete, not abstract, and written in terms of the read methods above.
    # One implementation means every venue reports health the same way, and a
    # new adapter cannot forget to implement it or implement it optimistically.

    async def health(self) -> BrokerHealth:
        """Observed health. Never raises, and never reports a state it did not see.

        It calls `get_account`, because the only way to know a link works is to
        use it. A terminal that is running but not logged in returns cleanly
        from connect and fails here, which is the distinction that matters.
        """
        started = time.perf_counter()
        if self.state is not ConnectionState.connected:
            return BrokerHealth(
                name=self.name,
                state=self.state,
                detail=f"adapter is {self.state.value}",
                mode=self.mode,
                reconnects=self.reconnects,
                last_error=getattr(self, "last_error", None),
            )
        try:
            account = await self.get_account()
        except Exception as exc:  # noqa: BLE001 - reported as degraded, never raised
            return BrokerHealth(
                name=self.name,
                state=ConnectionState.degraded,
                detail=f"connected but the account could not be read: {type(exc).__name__}",
                mode=self.mode,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                last_error=str(exc)[:200],
                reconnects=self.reconnects,
            )
        latency = round((time.perf_counter() - started) * 1000, 1)
        if not account.trade_allowed:
            # Connected, readable, and unable to trade. Degraded rather than
            # healthy: an order attempted here cannot succeed.
            return BrokerHealth(
                name=self.name,
                state=ConnectionState.degraded,
                detail="connected; the venue reports trading is not allowed on this account",
                mode=self.mode,
                account_mode=account.mode,
                trade_allowed=False,
                latency_ms=latency,
                reconnects=self.reconnects,
            )
        return BrokerHealth(
            name=self.name,
            state=ConnectionState.connected,
            detail="account readable and trading allowed",
            mode=self.mode,
            account_mode=account.mode,
            trade_allowed=True,
            latency_ms=latency,
            reconnects=self.reconnects,
        )

    # ------------------------------------------------------- reconciliation

    async def reconcile(
        self,
        internal: list[InternalPosition] | None = None,
        *,
        unresolved_unknowns: list[str] | None = None,
        known_order_ids: set[str] | None = None,
        magic: int | None = None,
    ) -> Reconciliation:
        """Compare the venue's view against ours and report the differences.

        **Reports; never repairs.** Nothing here opens, closes, cancels or
        rewrites anything, and that restraint is the point -- the instinct to
        close an unexpected position or re-send a missing one is exactly how a
        manual trade gets closed and how one intent becomes two positions.
        """
        positions = await self.get_positions(magic)
        orders = await self.get_orders(magic)
        return reconcile_positions(
            positions,
            internal or [],
            now=utcnow(),
            unresolved_unknowns=unresolved_unknowns,
            broker_orders=orders,
            known_order_ids=known_order_ids,
        )
