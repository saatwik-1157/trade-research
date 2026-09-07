"""The Strategy contract and the signal it produces.

A strategy answers one question -- *what does this data say?* -- and it answers
it with a `StrategySignal`. It does not size, approve, or send anything, and it
cannot: nothing in this package imports a broker adapter, the risk engine or
the OMS, and a test asserts that absence rather than trusting it.

**Two rules are load-bearing and both are enforced here rather than left to
each strategy to remember.**

1. **No look-ahead.** At bar T a strategy may use information available at or
   before T. `Candles.closed` drops the forming bar before a strategy ever
   sees the data, which is the same thing `mt5_paper`'s live rules do with
   `rates["close"][:-1]`. Reading the bar you enter on is the cheapest way to
   manufacture an edge that does not exist, and it is invisible in an equity
   curve.

2. **Signals fire on a closed bar.** The live loop and the backtester both
   assume it, and mixing candle-close with intrabar timing silently makes a
   backtest describe something the live tool does not do. `SignalTiming` names
   the assumption so a strategy that wanted intrabar behaviour would have to
   declare it rather than acquire it by accident.

The input is the normalized `Bar` from L08, never an MT5 recarray or a
TradingView payload, so the same strategy runs against a live feed, a
historical fetch, the replay store or a fixture without knowing which.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.marketdata.types import Bar, Timeframe


class SignalType(StrEnum):
    """What a strategy is saying. Seven values and no more.

    ENTRY_* and EXIT_* are deliberately separate: "get out of a long" and "go
    short" are different instructions, and a vocabulary that collapsed them
    would open a short every time a strategy wanted flat.

    HOLD and NO_SIGNAL are also separate. HOLD means the strategy looked and
    has a view -- stay as you are. NO_SIGNAL means it could not form one, for
    want of history or data. A caller counting "how often did this strategy
    have an opinion" needs to tell those apart.
    """

    entry_long = "ENTRY_LONG"
    entry_short = "ENTRY_SHORT"
    exit_long = "EXIT_LONG"
    exit_short = "EXIT_SHORT"
    close = "CLOSE"
    hold = "HOLD"
    no_signal = "NO_SIGNAL"

    @property
    def is_actionable(self) -> bool:
        """Whether anything downstream should consider doing something.

        Actionable does **not** mean approved. It means "worth showing to the
        risk engine", and the risk engine may still veto every one of them.
        """
        return self not in (SignalType.hold, SignalType.no_signal)

    @property
    def direction(self) -> str:
        """The platform's `signals.direction` vocabulary: buy, sell or flat."""
        if self in (SignalType.entry_long, SignalType.exit_short):
            return "buy"
        if self in (SignalType.entry_short, SignalType.exit_long):
            return "sell"
        return "flat"


class SignalTiming(StrEnum):
    """When a strategy considers a signal generated."""

    bar_close = "bar_close"  # evaluated on completed bars only
    intrabar = "intrabar"  # evaluated on a forming bar; nothing uses this yet


class StrategyTier(StrEnum):
    """How much evidence stands behind a strategy.

    `research_only` is the default and it is not a formality. Every rule in
    this repository has been measured and none separates from a coin flip at
    this broker's spreads; a tier that had to be raised deliberately is what
    stops a candidate from a parameter search being run live because it
    happened to be at the top of a table.
    """

    research_only = "research_only"
    paper_approved = "paper_approved"
    live_approved = "live_approved"


class StrategyError(Exception):
    """A strategy could not run. Never resolved to a neutral signal.

    Distinct from NO_SIGNAL on purpose: "the strategy said nothing" and "the
    strategy broke" must not look the same, or a crashing strategy reads as a
    quiet market.
    """


class ConfigError(StrategyError):
    """Invalid configuration. Refused, never silently corrected."""


@dataclass(frozen=True)
class StrategyMetadata:
    """What a strategy is, independent of any configuration of it."""

    key: str
    name: str
    description: str
    tier: StrategyTier = StrategyTier.research_only
    timing: SignalTiming = SignalTiming.bar_close
    # What the project has actually measured about this rule. Carried in the
    # metadata so it reaches an operator looking at a strategy list rather than
    # living only in a report nobody opens.
    evidence: str = "no measured edge; see CLAUDE.md"

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "tier": str(self.tier),
            "timing": str(self.timing),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DataRequirement:
    """What a strategy needs before it can say anything.

    `min_bars` is the warm-up. A strategy asked to run on less returns
    NO_SIGNAL rather than a signal computed from a half-filled indicator --
    an RSI over three bars is a number, and it is not an RSI.
    """

    min_bars: int
    timeframes: tuple[Timeframe, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "min_bars": self.min_bars,
            "timeframes": [str(t) for t in self.timeframes],
        }


@dataclass(frozen=True)
class StrategySignal:
    """A strategy's output. **Not an order, and not an approval.**

    `suggested_stop_loss` and `suggested_take_profit` are named "suggested"
    because that is what they are. Position sizing computes the volume and the
    risk engine may reject the trade outright; a strategy that could set its
    own bracket could set its own risk.

    `confidence` is None unless a strategy has a measured basis for one. None
    is the honest value here -- no rule in this repository has one -- and an
    invented number would be read downstream as evidence.
    """

    strategy_key: str
    symbol: str
    timeframe: Timeframe
    signal_type: SignalType
    # The close time of the bar the decision was made on, never "now": two runs
    # over the same data must produce the same signal time.
    bar_time: datetime
    generated_at: datetime
    strategy_version: int | None = None
    reference_price: Decimal | None = None
    confidence: Decimal | None = None
    suggested_stop_loss: Decimal | None = None
    suggested_take_profit: Decimal | None = None
    # Why the strategy said this. Diagnostic, never an instruction.
    reasoning: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.signal_type.is_actionable

    @property
    def direction(self) -> str:
        return self.signal_type.direction

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy_key": self.strategy_key,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "signal_type": str(self.signal_type),
            "direction": self.direction,
            "actionable": self.actionable,
            "bar_time": self.bar_time.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "reference_price": (
                str(self.reference_price) if self.reference_price is not None else None
            ),
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "suggested_stop_loss": (
                str(self.suggested_stop_loss) if self.suggested_stop_loss is not None else None
            ),
            "suggested_take_profit": (
                str(self.suggested_take_profit) if self.suggested_take_profit is not None else None
            ),
            "reasoning": self.reasoning,
            "metadata": dict(self.metadata),
            "note": "a signal, not an order; it has been sized by nothing and approved by nothing",
        }


@dataclass(frozen=True)
class Candles:
    """The bars a strategy is given, with the forming bar already removed.

    Construction is where look-ahead is prevented, once, rather than in every
    strategy. `Candles.of` drops any bar whose `complete` flag is false, so a
    strategy physically cannot read a bar that is still forming -- it is not
    in the object.
    """

    symbol: str
    timeframe: Timeframe
    bars: tuple[Bar, ...]

    @staticmethod
    def of(symbol: str, timeframe: Timeframe, bars: list[Bar]) -> Candles:
        closed = tuple(b for b in bars if b.complete)
        # Sorted here so no strategy has to assume an ordering a provider never
        # promised, and so two runs over the same set agree.
        closed = tuple(sorted(closed, key=lambda b: b.bar_time))
        return Candles(symbol=symbol, timeframe=timeframe, bars=closed)

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def last(self) -> Bar:
        if not self.bars:
            raise StrategyError("no closed bars")
        return self.bars[-1]

    def closes(self) -> list[Decimal]:
        return [b.close for b in self.bars]

    def opens(self) -> list[Decimal]:
        return [b.open for b in self.bars]

    def highs(self) -> list[Decimal]:
        return [b.high for b in self.bars]

    def lows(self) -> list[Decimal]:
        return [b.low for b in self.bars]


class Strategy(ABC):
    """The contract every strategy exposes.

    Stateless by design. A strategy holds its validated configuration and
    nothing else -- no database session, no broker connection, no cached bars
    -- so two instances of the same strategy on different symbols cannot
    interfere, and the same object runs live, in a backtest, in replay and in a
    test without knowing which.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = self.validate_config(config or {})

    @abstractmethod
    def metadata(self) -> StrategyMetadata: ...

    @abstractmethod
    def required_data(self) -> DataRequirement: ...

    @classmethod
    @abstractmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Return the validated configuration or raise `ConfigError`.

        A classmethod so a configuration can be checked before an instance is
        built -- the strategy builder (L13) and the registry both need that.
        Unknown keys are refused rather than ignored: a typo'd parameter that
        is silently dropped runs a strategy nobody configured.
        """

    @abstractmethod
    def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
        """Decide from closed bars only.

        `now` is passed rather than read from the clock so a replay or a
        backtest produces the same signal it would have produced at the time.
        A strategy that called `datetime.now()` would be non-deterministic and
        unreplayable.
        """

    # ------------------------------------------------------------- helpers

    def _no_signal(self, candles: Candles, now: datetime, reason: str) -> StrategySignal:
        """The honest answer when there is not enough to say anything."""
        return StrategySignal(
            strategy_key=self.metadata().key,
            symbol=candles.symbol,
            timeframe=candles.timeframe,
            signal_type=SignalType.no_signal,
            bar_time=candles.bars[-1].bar_time if candles.bars else now,
            generated_at=now,
            reasoning=reason,
        )
