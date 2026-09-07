"""Fill accounting: requested, filled, remaining, and the average price.

The rule this module exists to enforce is brief §21's, and it is one sentence:
**a requested quantity is not an executed quantity until the venue says so.**
Everything here follows from that.

  * `filled_quantity` only ever moves because a `FillRecord` was added, and a
    `FillRecord` carries where it came from. A simulated fill and a broker
    fill are both recorded, and `fill_source` keeps them distinguishable
    forever -- the `executions` table has had that column since L05 for the
    same reason.
  * `remaining_quantity` is derived, never stored. Two fields that must agree
    are two fields that will eventually disagree.
  * An overfill is **refused**. A venue reporting more than was asked for is a
    fault -- a duplicated execution report, a mis-parsed message, a
    reconciliation applying a fill twice -- and silently accepting it puts a
    position on the book that no order authorised.
  * The average price is quantity-weighted and computed from the whole list,
    so it does not drift with the order fills arrive in and does not
    accumulate rounding. `40 @ 100.20` then `60 @ 100.25` is exactly
    `100.23`, and recomputing from the list is what makes that reproducible
    after a restart that reloaded the fills from the database.

Nothing here talks to a venue, a database or a clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

ZERO = Decimal("0")


class FillError(Exception):
    """A fill that cannot be applied. Never applied partially."""


@dataclass(frozen=True)
class FillRecord:
    """One execution as the venue reported it. Never the quote we sent."""

    quantity: Decimal
    price: Decimal
    at: datetime
    #: 'broker', 'simulator' or 'import'. The `executions` table's vocabulary,
    #: reused rather than restated, so a simulated fill can never be read as a
    #: broker one downstream.
    fill_source: str
    broker_deal_id: str | None = None
    commission: Decimal = ZERO
    swap: Decimal = ZERO
    slippage_points: Decimal | None = None
    #: What the ACCOUNT received for this fill, as the venue booked it. Beside
    #: `commission` and `swap` rather than derived from them, and None when the
    #: venue did not say -- a gap, not a zero.
    realized_pnl: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise FillError(f"a fill quantity must be positive and finite, not {self.quantity}")
        if not self.price.is_finite() or self.price <= 0:
            raise FillError(f"a fill price must be positive and finite, not {self.price}")

    def as_dict(self) -> dict[str, object]:
        return {
            "quantity": str(self.quantity),
            "price": str(self.price),
            "at": self.at.isoformat(),
            "fill_source": self.fill_source,
            "broker_deal_id": self.broker_deal_id,
            "commission": str(self.commission),
            "swap": str(self.swap),
            "slippage_points": (
                str(self.slippage_points) if self.slippage_points is not None else None
            ),
        }


@dataclass
class FillBook:
    """Every fill against one order, and the figures derived from them."""

    requested: Decimal
    fills: list[FillRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.requested.is_finite() or self.requested <= 0:
            raise FillError(f"a requested quantity must be positive, not {self.requested}")

    # ------------------------------------------------------------ derived

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills), ZERO)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested - self.filled_quantity

    @property
    def complete(self) -> bool:
        return self.remaining_quantity <= ZERO

    @property
    def partial(self) -> bool:
        return bool(self.fills) and not self.complete

    @property
    def average_price(self) -> Decimal | None:
        """Quantity-weighted, recomputed from the whole list every time.

        `None` when nothing has filled -- not zero. A zero average price is a
        number that reads like a fill at no cost, and this project has already
        paid for a figure that looked real and was not.
        """
        total = self.filled_quantity
        if total <= ZERO:
            return None
        notional = sum((f.quantity * f.price for f in self.fills), ZERO)
        return notional / total

    @property
    def commission(self) -> Decimal:
        return sum((f.commission for f in self.fills), ZERO)

    @property
    def swap(self) -> Decimal:
        return sum((f.swap for f in self.fills), ZERO)

    @property
    def filled_at(self) -> datetime | None:
        """When the LAST fill landed, which is when the order became filled."""
        return max((f.at for f in self.fills), default=None)

    # -------------------------------------------------------------- adding

    def add(self, fill: FillRecord) -> None:
        """Apply one execution report. Refuses an overfill, never truncates it.

        Truncating would record a smaller fill than the venue reported and
        leave the difference on the book with nothing pointing at it. The
        caller gets an error naming both figures and reconciles.
        """
        if fill.quantity > self.remaining_quantity:
            raise FillError(
                f"a fill of {fill.quantity} exceeds the {self.remaining_quantity} "
                f"remaining on an order for {self.requested} ({self.filled_quantity} "
                "already filled). Refusing rather than truncating: the venue and this "
                "record disagree, and that has to be reconciled, not rounded away"
            )
        self.fills.append(fill)

    def already_recorded(self, broker_deal_id: str | None) -> bool:
        """Whether this venue deal is already in the book.

        Execution reports arrive more than once -- a reconnect replays them, a
        reconciliation sweep re-reads history. A deal id applied twice is a
        position twice the size it should be, so the check is by identity and
        not by shape: two genuine fills of the same size at the same price are
        a real thing that happens.
        """
        if broker_deal_id is None:
            return False
        return any(f.broker_deal_id == broker_deal_id for f in self.fills)

    def as_dict(self) -> dict[str, object]:
        average = self.average_price
        return {
            "requested_quantity": str(self.requested),
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "average_fill_price": str(average) if average is not None else None,
            "fill_count": len(self.fills),
            "fills": [f.as_dict() for f in self.fills],
        }
