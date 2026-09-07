"""FakeBroker: the PAPER venue.

An in-process simulator. It holds play positions, fills at the quote it is
given, and charges the spread on the way in and out exactly as
`rule_backtest.simulate()` does. Nothing here is random: a simulator that
sometimes fails would make a test's outcome depend on a coin flip.

**Every fill it produces is labelled `fill_source="simulator"`**, and its
`mode` is `paper`. Downstream code that pools a simulated fill with a broker
fill has to do so deliberately, because the label travels with the row.

It is also the fault injector. `fail_next`, `unknown_next` and
`disconnect_after` let a test drive rejection, unknown status and a mid-run
disconnect without touching a real terminal.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.auth.models import utcnow
from app.brokers.base import (
    Account,
    AccountMode,
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
    ConnectionState,
    Deal,
    NotConnected,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Quote,
    SymbolInfo,
)


@dataclass
class FakeBroker(BrokerAdapter):
    """Deterministic paper venue."""

    name: str = "fake"
    mode: str = "paper"
    currency: str = "USD"
    starting_balance: Decimal = Decimal("100000")

    quotes: dict[str, Quote] = field(default_factory=dict)
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)

    # Fault injection, all off by default.
    fail_next: str | None = None
    unknown_next: bool = False
    disconnect_after: int | None = None

    _state: ConnectionState = ConnectionState.disconnected
    _positions: dict[str, BrokerPosition] = field(default_factory=dict)
    _deals: list[Deal] = field(default_factory=list)
    _balance: Decimal | None = None
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _orders_placed: int = 0

    def __post_init__(self) -> None:
        if self._balance is None:
            self._balance = self.starting_balance

    # ---------------------------------------------------------- connection
    @property
    def state(self) -> ConnectionState:
        return self._state

    async def connect(self) -> Account:
        self._state = ConnectionState.connected
        return await self.get_account()

    async def disconnect(self) -> None:
        self._state = ConnectionState.disconnected

    def force_disconnect(self) -> None:
        """Simulate the venue going away mid-session."""
        self._state = ConnectionState.error

    def _require_connection(self) -> None:
        if self._state is not ConnectionState.connected:
            raise NotConnected(f"{self.name} is {self._state.value}")

    # ---------------------------------------------------------------- read
    async def get_account(self) -> Account:
        self._require_connection()
        floating = sum((p.profit or Decimal(0)) for p in self._positions.values())
        balance = self._balance or Decimal(0)
        return Account(
            login="fake-1",
            server="simulator",
            currency=self.currency,
            balance=balance,
            equity=balance + Decimal(floating),
            mode=AccountMode.demo,
            trade_allowed=True,
        )

    async def get_symbols(self) -> list[SymbolInfo]:
        self._require_connection()
        return list(self.symbols.values())

    async def get_quote(self, symbol: str) -> Quote:
        self._require_connection()
        quote = self.quotes.get(symbol)
        if quote is None:
            raise NotConnected(f"no quote for {symbol} in the simulator")
        return quote

    async def get_positions(self, magic: int | None = None) -> list[BrokerPosition]:
        self._require_connection()
        rows = list(self._positions.values())
        return [p for p in rows if magic is None or p.magic == magic]

    async def get_orders(self, magic: int | None = None) -> list[BrokerOrder]:
        self._require_connection()
        return []  # the simulator fills market orders immediately

    async def get_order_history(
        self, since: datetime, until: datetime | None = None, magic: int | None = None
    ) -> list[Deal]:
        self._require_connection()
        until = until or utcnow()
        return [
            d for d in self._deals if since <= d.at <= until and (magic is None or d.magic == magic)
        ]

    # --------------------------------------------------------------- write
    def _take_fault(self) -> OrderResult | None:
        if self.unknown_next:
            self.unknown_next = False
            return OrderResult(
                OrderStatus.unknown,
                "simulated: no reply from the venue",
                fill_source="simulator",
            )
        if self.fail_next:
            detail, self.fail_next = self.fail_next, None
            return OrderResult(
                OrderStatus.rejected, f"simulated rejection: {detail}", fill_source="simulator"
            )
        return None

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self._require_connection()
        fault = self._take_fault()
        if fault is not None:
            return fault

        quote = await self.get_quote(request.symbol)
        is_buy = request.side == "buy"
        # Pay the spread on entry: a buy lifts the ask, a sell hits the bid.
        fill = quote.ask if is_buy else quote.bid
        position_id = f"fake-pos-{next(self._ids)}"
        now = quote.at

        self._positions[position_id] = BrokerPosition(
            position_id=position_id,
            symbol=request.symbol,
            side="long" if is_buy else "short",
            volume=request.volume,
            entry_price=fill,
            opened_at=now,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            profit=Decimal(0),
            magic=request.magic,
        )
        self._deals.append(
            Deal(
                deal_id=f"fake-deal-{next(self._ids)}",
                position_id=position_id,
                symbol=request.symbol,
                entry="in",
                volume=request.volume,
                price=fill,
                at=now,
                magic=request.magic,
            )
        )
        self._orders_placed += 1
        if self.disconnect_after is not None and self._orders_placed >= self.disconnect_after:
            self.force_disconnect()

        return OrderResult(
            OrderStatus.accepted,
            "simulated fill at the " + ("ask" if is_buy else "bid"),
            order_id=position_id,
            position_id=position_id,
            fill_price=fill,
            filled_volume=request.volume,
            filled_at=now,
            retcode=0,
            fill_source="simulator",
        )

    async def modify_order(
        self,
        position_id: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> OrderResult:
        self._require_connection()
        fault = self._take_fault()
        if fault is not None:
            return fault
        position = self._positions.get(position_id)
        if position is None:
            return OrderResult(
                OrderStatus.rejected, f"no position {position_id}", fill_source="simulator"
            )
        self._positions[position_id] = BrokerPosition(
            **{
                **position.__dict__,
                "stop_loss": stop_loss if stop_loss is not None else position.stop_loss,
                "take_profit": take_profit if take_profit is not None else position.take_profit,
            }
        )
        return OrderResult(
            OrderStatus.accepted,
            "levels modified",
            position_id=position_id,
            fill_source="simulator",
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        self._require_connection()
        return OrderResult(
            OrderStatus.rejected,
            "the simulator fills market orders immediately; nothing is pending",
            fill_source="simulator",
        )

    async def close_position(self, position_id: str, volume: Decimal | None = None) -> OrderResult:
        self._require_connection()
        fault = self._take_fault()
        if fault is not None:
            return fault
        position = self._positions.pop(position_id, None)
        if position is None:
            return OrderResult(
                OrderStatus.rejected, f"no position {position_id}", fill_source="simulator"
            )
        quote = await self.get_quote(position.symbol)
        is_long = position.side == "long"
        # Pay the spread again on exit.
        fill = quote.bid if is_long else quote.ask
        move = (fill - position.entry_price) if is_long else (position.entry_price - fill)
        profit = move * position.volume
        self._balance = (self._balance or Decimal(0)) + profit
        self._deals.append(
            Deal(
                deal_id=f"fake-deal-{next(self._ids)}",
                position_id=position_id,
                symbol=position.symbol,
                entry="out",
                volume=position.volume,
                price=fill,
                at=quote.at,
                profit=profit,
                magic=position.magic,
            )
        )
        return OrderResult(
            OrderStatus.accepted,
            "simulated close",
            position_id=position_id,
            fill_price=fill,
            filled_volume=position.volume,
            filled_at=quote.at,
            retcode=0,
            fill_source="simulator",
        )

    # ------------------------------------------------------------- helpers
    def set_quote(self, symbol: str, bid: str, ask: str, at: datetime | None = None) -> Quote:
        quote = Quote(symbol, Decimal(bid), Decimal(ask), at or utcnow())
        self.quotes[symbol] = quote
        return quote
