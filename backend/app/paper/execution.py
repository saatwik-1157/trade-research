"""Paper execution: how a virtual order becomes a virtual fill.

The provider is deterministic. The same order against the same reference
produces the same fill, every time, which is what lets the P&L test check
against independently calculated numbers rather than against itself.

**The spread is charged by filling at the right side of the book, not by
subtracting a number.** A buy fills at the ask and a sell at the bid, so a
round trip pays the spread exactly once -- the same total L14 charges with its
single `spread_points` deduction, arrived at the way a venue actually arrives
at it. Charging both would double it, and a test pins the round trip.

Where the feed carries no bid/ask, the spread is **modelled** from the
configured `CostModel` and the fill says so: `bid_ask` is `measured` or
`modelled` on every fill, so a number that came from a model can never be read
as one that came from the book. This is the same distinction L08 draws with
`Availability`, and the reason it exists is in this repository's own history --
a single live quote understates this broker by 3-8x, and bars recording a zero
spread are unrecorded rather than free.

Slippage is **always adverse**: added to a buy, subtracted from a sell. A
slippage model that could help is one that flatters.

Nothing here can reach a venue. `assert_paper_execution` runs before every
fill, and the module imports no broker adapter -- a test parses it to prove it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.backtest.config import CostModel
from app.marketdata.types import Availability, Bar, Quote
from app.paper.router import ExecutionMode, ExecutionProvider, assert_paper_execution
from app.symbols.precision import normalize_price

# Sides, in the order-book sense. Positions use long/short; orders use buy/sell.
BUY = "buy"
SELL = "sell"


class ExecutionRefused(Exception):
    """The fill could not be computed. Never a fill at a guessed price."""


@dataclass(frozen=True)
class Reference:
    """The market a fill was computed against, and how well it was known.

    Carrying the reference on the fill is what makes an execution auditable:
    "filled at 1.10312" is not checkable, and "filled at the ask of 1.10312,
    measured, quoted at 12:04:01Z" is.
    """

    at: datetime
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    source: str  # "quote" | "bar_close"
    bid_ask: Availability

    @classmethod
    def from_quote(cls, quote: Quote) -> Reference:
        measured = quote.bid is not None and quote.ask is not None
        return cls(
            at=quote.at,
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last if quote.last is not None else quote.mid,
            source="quote",
            bid_ask=Availability.available if measured else Availability.not_available,
        )

    @classmethod
    def from_bar(cls, bar: Bar) -> Reference:
        """A closed bar. OHLC carries no book, so bid/ask is absent, not zero."""
        return cls(
            at=bar.bar_time,
            bid=None,
            ask=None,
            last=bar.close,
            source="bar_close",
            bid_ask=Availability.not_available,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "last": str(self.last) if self.last is not None else None,
            "source": self.source,
            "bid_ask": str(self.bid_ask),
        }


@dataclass(frozen=True)
class Fill:
    """A simulated fill. `execution_mode` is on it so it cannot be mistaken."""

    price: Decimal
    quantity: Decimal
    at: datetime
    side: str
    reference_price: Decimal
    slippage_points: Decimal
    spread_points: Decimal
    commission: Decimal
    bid_ask: Availability
    reference: Reference
    execution_mode: str = str(ExecutionMode.paper)
    execution_provider: str = str(ExecutionProvider.paper_execution_only)
    fill_source: str = "simulator"
    assumptions: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "price": str(self.price),
            "quantity": str(self.quantity),
            "at": self.at.isoformat(),
            "side": self.side,
            "reference_price": str(self.reference_price),
            "slippage_points": str(self.slippage_points),
            "spread_points": str(self.spread_points),
            "commission": str(self.commission),
            "bid_ask": str(self.bid_ask),
            "reference": self.reference.as_dict(),
            "execution_mode": self.execution_mode,
            "execution_provider": self.execution_provider,
            "fill_source": self.fill_source,
            "assumptions": self.assumptions,
        }


class PaperExecution:
    """The only execution provider a paper order can reach.

    It holds no adapter, no session and no credentials. Construct it with a
    `CostModel` -- L14's, so a paper fill and a backtest fill are priced by one
    set of assumptions rather than two that drift.
    """

    def __init__(self, costs: CostModel) -> None:
        self.costs = costs

    # ------------------------------------------------------------------ book

    def book(self, reference: Reference) -> tuple[Decimal, Decimal, Availability]:
        """(bid, ask, how well known). Modelled symmetrically when absent.

        A modelled book puts half the configured spread either side of the
        last price. That is a model, and the third return value says so on
        every fill it produces.
        """
        if reference.bid is not None and reference.ask is not None:
            return reference.bid, reference.ask, Availability.available
        if reference.last is None:
            raise ExecutionRefused(
                "the reference carries no bid/ask and no last price; there is "
                "nothing to fill against, and a fill at a guessed price is worse "
                "than no fill"
            )
        half = Decimal(self.costs.spread_points) / Decimal(2)
        return reference.last - half, reference.last + half, Availability.not_available

    # ------------------------------------------------------------------ fill

    def fill(
        self,
        *,
        mode: ExecutionMode | str,
        side: str,
        quantity: Decimal,
        reference: Reference,
        tick_size: Decimal | None = None,
        price_precision: int | None = None,
        at: datetime | None = None,
    ) -> Fill:
        """Compute a deterministic simulated fill.

        `assert_paper_execution` runs first, so this method refuses to fill an
        order whose mode does not route to PAPER_EXECUTION_ONLY -- even if a
        caller constructed it directly.
        """
        assert_paper_execution(mode)
        if side not in (BUY, SELL):
            raise ExecutionRefused(f"side must be {BUY!r} or {SELL!r}, not {side!r}")
        if quantity <= 0:
            raise ExecutionRefused(f"quantity must be positive, not {quantity}")

        bid, ask, known = self.book(reference)
        if ask < bid:
            raise ExecutionRefused(
                f"inverted book: ask {ask} is below bid {bid}. Filling against it "
                "would manufacture a profit that the market did not offer"
            )

        # BUY lifts the ask, SELL hits the bid. The round trip pays the spread
        # once, which is exactly L14's single charge.
        raw = ask if side == BUY else bid
        slippage = Decimal(self.costs.slippage_points)
        # Always adverse. A slippage model that could help is one that flatters.
        raw = raw + slippage if side == BUY else raw - slippage

        price = raw
        if tick_size is not None:
            price = normalize_price(raw, tick_size=tick_size, digits=price_precision).value

        return Fill(
            price=price,
            quantity=Decimal(quantity),
            at=at or reference.at,
            side=side,
            reference_price=reference.last if reference.last is not None else (bid + ask) / 2,
            slippage_points=slippage,
            spread_points=ask - bid,
            commission=Decimal(self.costs.commission_per_trade),
            bid_ask=known,
            reference=reference,
            assumptions=self.describe(),
        )

    def describe(self) -> dict[str, str]:
        return {
            "market_buy": "fills at the ASK",
            "market_sell": "fills at the BID",
            "spread": (
                "charged by filling at the correct side of the book, so a round trip "
                "pays it once. It is NOT also subtracted, which would double it"
            ),
            "modelled_book": (
                "where the feed carries no bid/ask, half the configured spread is "
                "placed either side of the last price and the fill is marked "
                "bid_ask=not_available. A modelled number never reads as a measured one"
            ),
            "slippage": "always adverse: added to a buy, subtracted from a sell",
            "commission": "charged per fill, in account currency, from the CostModel",
            "liquidity": (
                "assumed sufficient at the reference. No order book is modelled, and "
                "partial fills from depth are not simulated"
            ),
            "determinism": "the same order against the same reference always fills the same",
        }
