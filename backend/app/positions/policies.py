"""Exit policies: when a position should be closed, and why.

Pure decision logic. Nothing here touches a broker, a database or a clock it
was not handed, so every rule below is testable on its own and the same code
decides an exit in paper, demo and (eventually) live.

Two rules are inherited from the research harness rather than invented here:

  Both levels hit in one bar -> book the loss. `rule_backtest.simulate()`
  already refuses to assume the winner when a bar's range covers the stop and
  the target, because intrabar order is unknown and assuming the win is the
  classic way a backtest flatters itself. A live monitor sees the same
  ambiguity every time it polls, so it resolves it the same way.

  Floating P&L means net of carry. `tools/take_profit.net_floating` adds swap
  to `profit`, because a position held overnight can read positive on the
  gross figure and be negative once financing is charged. A risk exit that
  used the gross figure would book those as wins.

A note on the trailing stop, so nobody reads its presence as a
recommendation: this repository measured trailing exits at D1 and they were
WORSE than a fixed bracket - median out-of-sample expectancy -217 for a 3.0
ATR trail against -58 for the 1.5x1.5 bracket, with all eight exits negative.
The policy exists because it is asked for and has to be implemented
correctly; the measurement says it does not help.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class ExitReason(StrEnum):
    stop_loss = "stop_loss"
    take_profit = "take_profit"
    trailing_stop = "trailing_stop"
    strategy_exit = "strategy_exit"
    time_exit = "time_exit"
    risk_exit = "risk_exit"
    emergency_exit = "emergency_exit"
    #: A scaled exit: part of the position is taken off and the rest runs.
    #: Distinct from `take_profit`, which closes all of it -- a table that
    #: showed both as "take_profit" could not tell a finished trade from a
    #: trade that is still open at a smaller size.
    partial_take_profit = "partial_take_profit"


# Evaluation order. Emergency and risk come first because they are about the
# account surviving rather than this trade's merits; the stop precedes the
# target for the both-hit rule above.
PRIORITY: tuple[ExitReason, ...] = (
    ExitReason.emergency_exit,
    ExitReason.risk_exit,
    ExitReason.stop_loss,
    ExitReason.take_profit,
    # Below the full target on purpose: if price has reached the level that
    # closes the whole position, closing part of it instead would leave size on
    # the book that the full target had already decided to remove.
    ExitReason.partial_take_profit,
    ExitReason.trailing_stop,
    ExitReason.time_exit,
    ExitReason.strategy_exit,
)


@dataclass(frozen=True)
class PositionView:
    """What a policy is allowed to know about an open position.

    Deliberately not the ORM row: policies stay pure, and a view can be built
    from the database, from a broker read, or from a replay bar.
    """

    id: str
    #: The tradable CODE, e.g. "EURUSD" -- what a venue, an order and a quote
    #: all speak.
    #:
    #: It held the row's `symbol_id` in production and the code in every
    #: hand-built test view until 2026-09-07. The suite agreed with itself, the
    #: application disagreed with the suite, nothing compared them, and every
    #: close order silently failed to record because the order store resolves a
    #: code. One meaning, from here on.
    #:
    #: Empty when a row's symbol could not be resolved, which is a refusal
    #: rather than a fallback: an order cannot be built without it.
    symbol: str
    side: str  # "long" | "short"
    quantity: Decimal
    entry_price: Decimal
    opened_at: datetime
    mode: str = "paper"
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    # Best price seen since the position opened, in the favourable direction.
    # None means "not tracked yet"; the trailing policy seeds it from the quote.
    extreme_price: Decimal | None = None
    # Net of swap and commission, as the venue reports it. None means unknown,
    # and a risk exit refuses to fire on an unknown figure.
    floating_pnl: Decimal | None = None
    strategy_version_id: str | None = None

    # ---------------------------------------------------------------- L21
    #: Which account's order manager a close must be routed through. A close
    #: sent to the wrong account's venue closes the wrong position.
    account_id: str | None = None
    #: The venue's own id. `id` is ours and is what the record is keyed by;
    #: this is what the venue is asked about. Treating one as the other is how
    #: a reconciliation sweep matches nothing.
    broker_position_id: str | None = None
    #: What the position was opened at, and what has been taken off since.
    #: `quantity` is what is OPEN; these two are the history it was cut from.
    initial_quantity: Decimal | None = None
    closed_quantity: Decimal = Decimal("0")
    #: What the VENUE last reported its protective levels to be. `stop_loss`
    #: and `take_profit` above are what this platform INTENDS. A disagreement
    #: between the two is a position running differently from its record, and
    #: `protection_gap` is what names it.
    broker_stop_loss: Decimal | None = None
    broker_take_profit: Decimal | None = None

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def protection_gap(self) -> str | None:
        """Whether the venue is holding the stop this platform believes in.

        `None` when they agree or when the venue has never been read. A string
        when they do not, because a stop that exists in the record and not at
        the venue is a position running unprotected while the screen says
        otherwise -- and that is the single most dangerous disagreement this
        module can observe.
        """
        if self.stop_loss is None:
            return None
        if self.broker_stop_loss is None:
            # Never read is not the same as absent. Only the caller knows
            # whether a sync has happened, so this says nothing.
            return None
        if self.broker_stop_loss != self.stop_loss:
            return (
                f"the record says the stop is {self.stop_loss} and the venue is holding "
                f"{self.broker_stop_loss}"
            )
        return None


@dataclass(frozen=True)
class MarketState:
    """The quote a decision is made against."""

    #: The tradable CODE, matching `PositionView.symbol`. The two are compared
    #: before a close, so they have to speak the same language.
    symbol: str
    bid: Decimal
    ask: Decimal
    as_of: datetime
    atr: Decimal | None = None

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def exit_price(self, is_long: bool) -> Decimal:
        """A long exits into the bid, a short into the ask. Never the mid."""
        return self.bid if is_long else self.ask


@dataclass(frozen=True)
class RiskContext:
    """Account and session state a position-level policy may consult."""

    emergency_stop: bool = False
    kill_switch_reason: str | None = None
    max_floating_loss: Decimal | None = None
    daily_loss_breached: bool = False
    strategy_exit_signalled: bool = False
    max_hold: timedelta | None = None


@dataclass(frozen=True)
class ExitDecision:
    reason: ExitReason
    detail: str
    # The price the decision was made against; the fill is whatever the venue
    # returns, and the two are recorded separately on purpose.
    reference_price: Decimal
    decided_at: datetime
    #: How much to close. `None` means all of it -- which is what every policy
    #: that predates L21 means, so their behaviour is unchanged. A number means
    #: a partial exit, and it is checked against what is actually open rather
    #: than clamped to it.
    quantity: Decimal | None = None


@dataclass(frozen=True)
class StopModification:
    """A stop moved without closing the position."""

    position_id: str
    old_stop: Decimal | None
    new_stop: Decimal
    reason: str
    at: datetime


class ExitPolicy(Protocol):
    name: str

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None: ...


@dataclass
class EmergencyExitPolicy:
    """Kill switch. Closes regardless of price, P&L or strategy opinion."""

    name: str = "emergency_exit"

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        if not context.emergency_stop:
            return None
        why = context.kill_switch_reason or "emergency stop engaged"
        return ExitDecision(
            ExitReason.emergency_exit,
            why,
            market.exit_price(position.is_long),
            market.as_of,
        )


@dataclass
class RiskExitPolicy:
    """Close on an account- or position-level risk breach.

    Refuses to act on an unknown floating figure: `None` means the venue did
    not tell us, and closing on a number we do not have would be guessing.
    """

    name: str = "risk_exit"

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        price = market.exit_price(position.is_long)
        if context.daily_loss_breached:
            return ExitDecision(
                ExitReason.risk_exit, "daily loss limit breached", price, market.as_of
            )
        limit = context.max_floating_loss
        if limit is None or position.floating_pnl is None:
            return None
        if position.floating_pnl <= -abs(limit):
            return ExitDecision(
                ExitReason.risk_exit,
                f"floating {position.floating_pnl} at or beyond limit {-abs(limit)} (net of swap)",
                price,
                market.as_of,
            )
        return None


@dataclass
class StopLossPolicy:
    """Price has reached the stop.

    Checked before the target: when one poll interval covers both levels the
    order inside it is unknown, and booking the loss is the honest resolution.
    """

    name: str = "stop_loss"

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        stop = position.stop_loss
        if stop is None:
            return None
        price = market.exit_price(position.is_long)
        hit = price <= stop if position.is_long else price >= stop
        if not hit:
            return None
        return ExitDecision(
            ExitReason.stop_loss, f"price {price} reached stop {stop}", price, market.as_of
        )


@dataclass
class TakeProfitPolicy:
    name: str = "take_profit"

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        target = position.take_profit
        if target is None:
            return None
        price = market.exit_price(position.is_long)
        hit = price >= target if position.is_long else price <= target
        if not hit:
            return None
        return ExitDecision(
            ExitReason.take_profit, f"price {price} reached target {target}", price, market.as_of
        )


@dataclass
class TrailingStopPolicy:
    """Ratchet the stop behind the best price seen, never loosen it.

    Two settings, one of which must be given: `atr_multiple` needs an ATR on
    the quote, `distance` is an absolute price distance. The policy never
    closes a position itself - it moves the stop and lets StopLossPolicy fire,
    so there is exactly one place a stop exit is decided.
    """

    atr_multiple: Decimal | None = None
    distance: Decimal | None = None
    name: str = "trailing_stop"

    def new_extreme(self, position: PositionView, market: MarketState) -> Decimal:
        price = market.exit_price(position.is_long)
        if position.extreme_price is None:
            return price
        return (
            max(position.extreme_price, price)
            if position.is_long
            else min(position.extreme_price, price)
        )

    def proposed_stop(self, position: PositionView, market: MarketState) -> StopModification | None:
        gap = self.distance
        if gap is None and self.atr_multiple is not None:
            if market.atr is None:
                return None  # no ATR, no trail: nothing is invented
            gap = self.atr_multiple * market.atr
        if gap is None or gap <= 0:
            return None

        extreme = self.new_extreme(position, market)
        candidate = extreme - gap if position.is_long else extreme + gap
        current = position.stop_loss
        if current is not None:
            improves = candidate > current if position.is_long else candidate < current
            if not improves:
                return None
        return StopModification(
            position_id=position.id,
            old_stop=current,
            new_stop=candidate,
            reason=f"trail {gap} behind {extreme}",
            at=market.as_of,
        )

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        # Moving a stop is a modification, not an exit.
        return None


@dataclass
class BreakEvenPolicy:
    """Move the stop to the entry once the trade is far enough ahead.

    Like the trailing policy, this NEVER closes a position: it moves the stop
    and lets `StopLossPolicy` fire, so there is exactly one place a stop exit
    is decided.

    Two settings, one of which must be given. `atr_multiple` needs an ATR on
    the quote; `trigger_distance` is an absolute price distance. `buffer` is
    added past the entry in the favourable direction so the stop clears the
    spread -- a break-even stop placed exactly at the entry is a stop that
    loses the spread every time it fires, which is not break-even.

    `buffer_atr_multiple` states that same buffer as a multiple of the ATR,
    and it exists so the buffer can be CONFIGURED at all. An absolute buffer
    is a price, and a price means different things on different instruments,
    so a deployment-wide one would be wrong for every symbol but one. When it
    is set it wins over `buffer`, and a quote with no ATR proposes nothing
    rather than falling back to a buffer nobody chose -- the same fail-closed
    rule the trigger already follows.

    **It only ever tightens.** The proposal is dropped unless it improves on
    the current stop in the protective direction, which is the same rule the
    trail follows and the same rule invariant 11 states. A break-even that
    could move a stop backwards would be a way to widen risk under a name that
    sounds safe.

    The research note attached to this one is worth keeping: the move-to-
    breakeven exit was measured across a 16x8 grid at D1 and had a MEDIAN
    out-of-sample expectancy of -123 points, worse than the fixed bracket's
    -58 (`reports/exit_search_d1.json`). It is a risk control, not an edge,
    and it is off by default for that reason.
    """

    atr_multiple: Decimal | None = None
    trigger_distance: Decimal | None = None
    buffer: Decimal = Decimal("0")
    buffer_atr_multiple: Decimal | None = None
    name: str = "break_even"

    def buffer_for(self, market: MarketState) -> Decimal | None:
        """The gap past the entry, in price. None means refuse."""
        if self.buffer_atr_multiple is None:
            return self.buffer
        if market.atr is None:
            return None  # no ATR, no buffer: nothing is invented
        return self.buffer_atr_multiple * market.atr

    def trigger(self, market: MarketState) -> Decimal | None:
        gap = self.trigger_distance
        if gap is None and self.atr_multiple is not None:
            if market.atr is None:
                return None  # no ATR, no trigger: nothing is invented
            gap = self.atr_multiple * market.atr
        if gap is None or gap <= 0:
            return None
        return gap

    def proposed_stop(self, position: PositionView, market: MarketState) -> StopModification | None:
        gap = self.trigger(market)
        if gap is None:
            return None
        price = market.exit_price(position.is_long)
        advanced = (
            price - position.entry_price if position.is_long else position.entry_price - price
        )
        if advanced < gap:
            return None

        buffer = self.buffer_for(market)
        if buffer is None:
            return None
        candidate = (
            position.entry_price + buffer if position.is_long else position.entry_price - buffer
        )
        current = position.stop_loss
        if current is not None:
            improves = candidate > current if position.is_long else candidate < current
            if not improves:
                # Already at or past break-even. Sending it again would be a
                # duplicate modification for no change, which section 14 names
                # explicitly.
                return None
        return StopModification(
            position_id=position.id,
            old_stop=current,
            new_stop=candidate,
            reason=f"break-even: {advanced} ahead, at or past the {gap} trigger",
            at=market.as_of,
        )

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        # Moving a stop is a modification, not an exit.
        return None


@dataclass
class PartialTakeProfitPolicy:
    """Take part of the position off at a level, and let the rest run.

    `fraction` is of the INITIAL size, not the remaining size, so a 0.3 rule
    takes 30 of a 100-lot position once and not 30, then 21, then 14.7. The
    policy fires only while less than `fraction` of the initial size has been
    closed, which is what makes running it twice on the same tick a no-op --
    section 34's idempotency requirement, satisfied by the arithmetic rather
    than by a flag somebody has to clear.

    It is off by default: `level` must be given, and no default level exists,
    because a partial target this platform invented would be a price nobody
    chose.
    """

    level: Decimal | None = None
    fraction: Decimal = Decimal("0.5")
    name: str = "partial_take_profit"

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        if self.level is None or self.fraction <= 0 or self.fraction >= 1:
            return None
        initial = position.initial_quantity or position.quantity
        if initial <= 0:
            return None
        wanted = (initial * self.fraction).quantize(position.quantity)
        if wanted <= 0:
            return None
        # Already taken. Re-firing would scale out of a position that has
        # already been scaled out of.
        if position.closed_quantity >= wanted:
            return None
        remaining_to_take = wanted - position.closed_quantity
        if remaining_to_take > position.quantity:
            remaining_to_take = position.quantity
        if remaining_to_take <= 0:
            return None

        price = market.exit_price(position.is_long)
        hit = price >= self.level if position.is_long else price <= self.level
        if not hit:
            return None
        return ExitDecision(
            ExitReason.partial_take_profit,
            f"price {price} reached the partial target {self.level}; taking "
            f"{remaining_to_take} of {initial}",
            price,
            market.as_of,
            quantity=remaining_to_take,
        )


@dataclass
class TimeExitPolicy:
    """Close after a maximum holding time.

    The research note attached to this one matters: a 120-bar hold showed a
    positive median across unrelated entries that dissolved into market drift
    once cut into era blocks. A time exit is a position-management control,
    not an edge.
    """

    max_hold: timedelta | None = None
    name: str = "time_exit"

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        limit = self.max_hold or context.max_hold
        if limit is None:
            return None
        held = market.as_of - position.opened_at
        if held < limit:
            return None
        return ExitDecision(
            ExitReason.time_exit,
            f"held {held} at or beyond {limit}",
            market.exit_price(position.is_long),
            market.as_of,
        )


@dataclass
class StrategyExitPolicy:
    """The strategy that opened the position says to leave."""

    name: str = "strategy_exit"

    def evaluate(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        if not context.strategy_exit_signalled:
            return None
        return ExitDecision(
            ExitReason.strategy_exit,
            "strategy signalled exit",
            market.exit_price(position.is_long),
            market.as_of,
        )


@dataclass
class PolicySet:
    """The policies applied to one position, evaluated in PRIORITY order."""

    policies: list[ExitPolicy] = field(default_factory=list)

    @classmethod
    def default(
        cls,
        *,
        trailing: TrailingStopPolicy | None = None,
        max_hold: timedelta | None = None,
        break_even: BreakEvenPolicy | None = None,
        partial: PartialTakeProfitPolicy | None = None,
    ) -> PolicySet:
        """The standard set. `break_even` and `partial` are OFF unless asked
        for: both need a level or a trigger the caller chooses, and a default
        this platform invented would be a price nobody picked."""
        policies: list[ExitPolicy] = [
            EmergencyExitPolicy(),
            RiskExitPolicy(),
            StopLossPolicy(),
            TakeProfitPolicy(),
            trailing or TrailingStopPolicy(),
            TimeExitPolicy(max_hold=max_hold),
            StrategyExitPolicy(),
        ]
        if break_even is not None:
            policies.append(break_even)
        if partial is not None:
            policies.append(partial)
        return cls(policies)

    def trailing(self) -> TrailingStopPolicy | None:
        for policy in self.policies:
            if isinstance(policy, TrailingStopPolicy):
                return policy
        return None

    def stop_movers(self) -> list[TrailingStopPolicy | BreakEvenPolicy]:
        """Every policy that proposes a stop move, in the order they appear.

        The manager applies the most protective of their proposals rather than
        the first: a break-even and a trail can both want to move the stop on
        the same tick, and taking whichever ran first would make the outcome
        depend on list order. Section 20 asks for deterministic priority
        between competing management actions; for stops, "the most protective
        wins" is that rule, and it cannot loosen anything because each policy
        has already refused to propose a loosening of its own.
        """
        return [p for p in self.policies if isinstance(p, TrailingStopPolicy | BreakEvenPolicy)]

    def decide(
        self, position: PositionView, market: MarketState, context: RiskContext
    ) -> ExitDecision | None:
        """First decision in priority order, or None to hold."""
        by_reason: dict[ExitReason, ExitDecision] = {}
        for policy in self.policies:
            decision = policy.evaluate(position, market, context)
            if decision is not None:
                by_reason.setdefault(decision.reason, decision)
        for reason in PRIORITY:
            if reason in by_reason:
                return by_reason[reason]
        return None
