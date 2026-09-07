"""The paper account: lifecycle, position accounting, P&L and equity.

Everything here is instance state. There is **no module-level mutable state in
this package**, which is what makes two paper accounts isolated by
construction rather than by convention -- account A cannot see or change
account B's balance, positions or statistics because there is no shared object
between them. `test_two_paper_accounts_do_not_share_state` asserts it.

Money in this module is not money. It is denominated in the account's
currency, and it moves only because a simulated fill said so.

**Value per price unit is measured, never assumed.** One unit of price
movement on one unit of volume is worth `tick_value / tick_size` in account
currency, and both come from the broker's contract spec. A missing figure
refuses the calculation rather than defaulting -- this repository has already
paid for the alternative: a lot sized from an absent tick value is a real
order for the wrong amount.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.symbols.precision import SpecIncomplete as SpecIncomplete
from app.symbols.precision import value_per_price_unit as value_per_price_unit

ZERO = Decimal("0")


# ============================================================== the lifecycle


class AccountState(StrEnum):
    """Where a paper account is.

    Stored on `paper_accounts.status`. The pre-L16 model carried only
    `is_active`, which cannot distinguish "never started" from "paused by the
    user" from "disabled by an admin" -- three states that authorise different
    operations.
    """

    created = "created"
    active = "active"
    paused = "paused"
    disabled = "disabled"
    closed = "closed"


TRANSITIONS: dict[AccountState, frozenset[AccountState]] = {
    AccountState.created: frozenset({AccountState.active, AccountState.disabled}),
    AccountState.active: frozenset(
        {AccountState.paused, AccountState.disabled, AccountState.closed}
    ),
    AccountState.paused: frozenset(
        {AccountState.active, AccountState.disabled, AccountState.closed}
    ),
    # Terminal for trading. A disabled account can be closed for the record but
    # never returns to active without an explicit reset.
    AccountState.disabled: frozenset({AccountState.closed}),
    AccountState.closed: frozenset(),
}

# The states in which a new order may be created. `created` is deliberately
# absent: an account that has never been activated is not a trading venue.
TRADEABLE = frozenset({AccountState.active})


class IllegalAccountTransition(Exception):
    """A move the lifecycle does not allow. Never silently ignored."""


class AccountNotTradeable(Exception):
    """An order was proposed against an account that cannot accept one."""


def check_transition(current: AccountState, wanted: AccountState) -> None:
    if wanted not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(str(s) for s in TRANSITIONS[current])) or "nothing"
        raise IllegalAccountTransition(
            f"a {current} paper account cannot become {wanted}; it may become {allowed}"
        )


def check_tradeable(state: AccountState) -> None:
    if state not in TRADEABLE:
        raise AccountNotTradeable(
            f"a {state} paper account does not accept orders; it must be "
            f"{', '.join(sorted(str(s) for s in TRADEABLE))}"
        )


# ============================================================ value per point

# Moved to `app.symbols.precision` at L18 and re-exported here, so the paper
# portfolio, position sizing and anything else that converts price movement
# into money all call one function. The name stays importable from this module
# because `app.paper.engine`, `app.paper.service` and the paper tests already
# import it from here, and moving a working import is churn without a gain.
__all__ = ["SpecIncomplete", "value_per_price_unit"]


# ================================================================= positions


class PositionError(Exception):
    """An accounting operation that would produce an impossible position."""


@dataclass
class PaperPosition:
    """One open simulated position. Quantity is always strictly positive."""

    symbol: str
    side: str  # "long" | "short"
    quantity: Decimal
    average_entry: Decimal
    opened_at: datetime
    updated_at: datetime
    value_per_unit: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    trailing_stop: Decimal | None = None
    realized_pnl: Decimal = ZERO
    strategy_id: str | None = None

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    def unrealized(self, price: Decimal) -> Decimal:
        """Mark to a price, in account currency."""
        move = (Decimal(price) - self.average_entry) * self.direction
        return move * self.quantity * self.value_per_unit

    def as_dict(self, price: Decimal | None = None) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "average_entry": str(self.average_entry),
            "current_price": str(price) if price is not None else None,
            "unrealized_pnl": str(self.unrealized(price)) if price is not None else None,
            "realized_pnl": str(self.realized_pnl),
            "stop_loss": str(self.stop_loss) if self.stop_loss is not None else None,
            "take_profit": str(self.take_profit) if self.take_profit is not None else None,
            "trailing_stop": (str(self.trailing_stop) if self.trailing_stop is not None else None),
            "opened_at": self.opened_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "strategy_id": self.strategy_id,
            "execution_mode": "PAPER",
        }


@dataclass(frozen=True)
class ClosedTrade:
    """A closed round trip, or the closed part of one. The journal's unit."""

    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_pnl: Decimal
    commission: Decimal
    net_pnl: Decimal
    exit_reason: str
    strategy_id: str | None = None
    execution_mode: str = "PAPER"

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "gross_pnl": str(self.gross_pnl),
            "commission": str(self.commission),
            "net_pnl": str(self.net_pnl),
            "exit_reason": self.exit_reason,
            "strategy_id": self.strategy_id,
            "execution_mode": self.execution_mode,
            # `net_points` lets L14's `compute_metrics` read this record, so
            # paper, replay and backtest statistics come from one implementation.
            "net_points": float(self.exit_price - self.entry_price)
            * (1 if self.side == "long" else -1),
        }


# ================================================================= portfolio


@dataclass
class PaperPortfolio:
    """Balance, positions and the closed-trade record for ONE paper account.

    Isolation is structural: everything is on this instance and nothing in the
    package is global, so two portfolios cannot interfere.
    """

    account_id: str
    currency: str
    starting_balance: Decimal
    balance: Decimal = ZERO
    peak_equity: Decimal = ZERO
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    trades: list[ClosedTrade] = field(default_factory=list)
    commission_paid: Decimal = ZERO
    state: AccountState = AccountState.created

    def __post_init__(self) -> None:
        if self.balance == ZERO:
            self.balance = self.starting_balance
        if self.peak_equity == ZERO:
            self.peak_equity = self.starting_balance

    # ----------------------------------------------------------- accounting

    def apply_fill(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        at: datetime,
        value_per_unit: Decimal,
        exit_reason: str = "signal",
        strategy_id: str | None = None,
    ) -> list[ClosedTrade]:
        """Apply one fill. Handles open, add, reduce, close and reverse.

        Returns the trades this fill closed -- empty when it opened or added.
        Commission is cash and leaves the balance immediately, whether the fill
        opened or closed.
        """
        quantity = Decimal(quantity)
        price = Decimal(price)
        if quantity <= 0:
            raise PositionError(f"a fill quantity must be positive, not {quantity}")

        self.balance -= Decimal(commission)
        self.commission_paid += Decimal(commission)

        wanted = "long" if side == "buy" else "short"
        existing = self.positions.get(symbol)

        if existing is None:
            self.positions[symbol] = PaperPosition(
                symbol=symbol,
                side=wanted,
                quantity=quantity,
                average_entry=price,
                opened_at=at,
                updated_at=at,
                value_per_unit=value_per_unit,
                strategy_id=strategy_id,
            )
            return []

        if existing.side == wanted:
            # Adding. The average moves; nothing is realised.
            total = existing.quantity + quantity
            existing.average_entry = (
                existing.average_entry * existing.quantity + price * quantity
            ) / total
            existing.quantity = total
            existing.updated_at = at
            return []

        # Opposite direction: reduce, close, or reverse.
        closing = min(existing.quantity, quantity)
        trade = self._realise(existing, closing, price, at, commission, exit_reason)
        remainder = quantity - closing

        existing.quantity -= closing
        existing.updated_at = at
        if existing.quantity <= 0:
            # `<= 0` rather than `== 0` so a rounding artefact can never leave a
            # position with a negative quantity sitting in the book.
            del self.positions[symbol]

        if remainder > 0:
            # A reversal: the surplus opens a new position on the other side,
            # at this fill's price. It is a new position, not a mutated one.
            self.positions[symbol] = PaperPosition(
                symbol=symbol,
                side=wanted,
                quantity=remainder,
                average_entry=price,
                opened_at=at,
                updated_at=at,
                value_per_unit=value_per_unit,
                strategy_id=strategy_id,
            )
        return [trade]

    def _realise(
        self,
        position: PaperPosition,
        quantity: Decimal,
        price: Decimal,
        at: datetime,
        commission: Decimal,
        exit_reason: str,
    ) -> ClosedTrade:
        move = (price - position.average_entry) * position.direction
        gross = move * quantity * position.value_per_unit
        # Commission was already taken off the balance above; it is carried on
        # the trade so the journal's net figure is the whole cost of the round
        # trip, not the gross with a separate line nobody adds up.
        net = gross - Decimal(commission)
        self.balance += gross
        position.realized_pnl += gross
        trade = ClosedTrade(
            symbol=position.symbol,
            side=position.side,
            quantity=quantity,
            entry_price=position.average_entry,
            exit_price=price,
            opened_at=position.opened_at,
            closed_at=at,
            gross_pnl=gross,
            commission=Decimal(commission),
            net_pnl=net,
            exit_reason=exit_reason,
            strategy_id=position.strategy_id,
        )
        self.trades.append(trade)
        return trade

    # ------------------------------------------------------------- marking

    def unrealized(self, prices: dict[str, Decimal]) -> Decimal:
        """Sum of open-position marks. A symbol with no price contributes 0.

        A missing price is reported by `marks_missing`, never treated as a
        loss or a gain: an unpriced position is unknown, not flat.
        """
        total = ZERO
        for symbol, position in self.positions.items():
            price = prices.get(symbol)
            if price is not None:
                total += position.unrealized(price)
        return total

    def marks_missing(self, prices: dict[str, Decimal]) -> tuple[str, ...]:
        return tuple(sorted(s for s in self.positions if s not in prices))

    def equity(self, prices: dict[str, Decimal]) -> Decimal:
        value = self.balance + self.unrealized(prices)
        self.peak_equity = max(self.peak_equity, value)
        return value

    def exposure(self, prices: dict[str, Decimal]) -> Decimal:
        """Notional value of open positions, in account currency."""
        total = ZERO
        for symbol, position in self.positions.items():
            price = prices.get(symbol, position.average_entry)
            total += abs(price * position.quantity * position.value_per_unit)
        return total

    @property
    def realized_pnl(self) -> Decimal:
        return self.balance - self.starting_balance

    def drawdown(self, prices: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
        """(absolute, percent) from the running peak."""
        value = self.equity(prices)
        fall = max(ZERO, self.peak_equity - value)
        pct = (fall / self.peak_equity * 100) if self.peak_equity > 0 else ZERO
        return fall, pct

    def realised_since(self, start: datetime) -> Decimal:
        """Realised P&L over trades closed at or after `start`. The day figure."""
        return sum(
            (t.net_pnl for t in self.trades if t.closed_at >= start),
            ZERO,
        )

    def trades_since(self, start: datetime) -> int:
        return sum(1 for t in self.trades if t.closed_at >= start)

    # ------------------------------------------------------------- reporting

    def snapshot(self, prices: dict[str, Decimal] | None = None) -> dict[str, object]:
        prices = prices or {}
        equity = self.equity(prices)
        fall, pct = self.drawdown(prices)
        unrealized = self.unrealized(prices)
        return {
            "account_id": self.account_id,
            "execution_mode": "PAPER",
            "currency": self.currency,
            "state": str(self.state),
            "starting_balance": str(self.starting_balance),
            "balance": str(self.balance),
            "equity": str(equity),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(unrealized),
            "total_pnl": str(self.realized_pnl + unrealized),
            "commission_paid": str(self.commission_paid),
            "exposure": str(self.exposure(prices)),
            "peak_equity": str(self.peak_equity),
            "drawdown": str(fall),
            "drawdown_pct": str(pct),
            "open_positions": len(self.positions),
            "closed_trades": len(self.trades),
            "marks_missing": list(self.marks_missing(prices)),
            "note": "Virtual capital. No real money is involved at any point.",
        }
