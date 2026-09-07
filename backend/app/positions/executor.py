"""Closing a position, and refusing to pretend it closed.

The contract every executor must satisfy:

  CONFIRMED  the venue reported a fill. Only then is the position closed.
  REJECTED   the venue refused. The position is still open.
  UNKNOWN    we do not know. The request may or may not have reached the
             venue - an IPC timeout after `order_send` looks exactly like a
             rejection from here.

UNKNOWN is the reason this module exists. `tools/mt5_paper.place()` treats a
missing result as REJECTED, and that is safe for an open but not for a close:
retrying an uncertain close can double-close, and treating it as failed can
leave a position nobody is watching. So UNKNOWN parks the position for
reconciliation and nothing retries it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.positions.policies import ExitDecision, MarketState, PositionView


class CloseStatus(StrEnum):
    confirmed = "confirmed"
    rejected = "rejected"
    unknown = "unknown"


@dataclass(frozen=True)
class CloseOutcome:
    status: CloseStatus
    detail: str
    # Populated only when CONFIRMED. The fill is the venue's number, never the
    # quote the decision was made against.
    fill_price: Decimal | None = None
    closed_at: datetime | None = None
    broker_deal_id: str | None = None
    # Where the fill came from, so a simulated close can never be mistaken for
    # a broker one further down the pipeline.
    fill_source: str = "unknown"
    #: What the ACCOUNT received, in account currency, as the venue booked it.
    #: None when the venue did not say -- and for a broker close that means the
    #: figure is NOT booked, because the alternative arithmetic omits the
    #: contract size and would state a number nobody measured.
    realized_pnl: Decimal | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.status is CloseStatus.confirmed


class ExitExecutor(Protocol):
    """Closes a position at a venue. Implementations must never invent a fill."""

    mode: str

    async def close(
        self, position: PositionView, market: MarketState, decision: ExitDecision
    ) -> CloseOutcome: ...


@dataclass
class PaperExitExecutor:
    """Internal simulator. No broker, no money, and it says so on every fill.

    The fill is the exit side of the quote, so the spread is paid on the way
    out exactly as it is in `rule_backtest.simulate()`. Nothing is randomised:
    a simulator that sometimes succeeds would make a test's outcome depend on
    a coin flip rather than on the code under test.
    """

    mode: str = "paper"
    fill_source: str = "simulator"

    async def close(
        self, position: PositionView, market: MarketState, decision: ExitDecision
    ) -> CloseOutcome:
        if market.symbol != position.symbol:
            return CloseOutcome(
                CloseStatus.rejected,
                f"quote is for {market.symbol}, position is {position.symbol}",
                fill_source=self.fill_source,
            )
        price = market.exit_price(position.is_long)
        if price <= 0:
            return CloseOutcome(
                CloseStatus.rejected,
                f"no usable quote for {position.symbol} ({price})",
                fill_source=self.fill_source,
            )
        return CloseOutcome(
            CloseStatus.confirmed,
            f"simulated fill at the {'bid' if position.is_long else 'ask'}",
            fill_price=price,
            closed_at=market.as_of,
            broker_deal_id=None,
            fill_source=self.fill_source,
        )


@dataclass
class UnavailableExitExecutor:
    """Stands in for a venue this platform cannot reach yet.

    Used for demo and live until the broker adapter exists. It returns
    REJECTED with a reason rather than raising, so a monitor keeps running and
    the position stays open and visible, and it never returns CONFIRMED.
    """

    mode: str
    reason: str = "no broker adapter is built yet (level 10)"

    async def close(
        self, position: PositionView, market: MarketState, decision: ExitDecision
    ) -> CloseOutcome:
        return CloseOutcome(
            CloseStatus.rejected,
            f"cannot close a {self.mode} position: {self.reason}",
            fill_source="none",
        )
