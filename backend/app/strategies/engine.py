"""The Strategy Engine: load, validate, run, validate the output, publish.

What it does, in order: resolve the strategy, check the data is sufficient and
fresh, run the strategy inside a boundary that a raised exception cannot cross,
validate the signal it produced, publish `SIGNAL_CREATED` when the signal is
actionable, and record what happened.

**What it cannot do, asserted by a test:** size a position, approve anything,
call a broker, or submit an order. Nothing in `app.strategies` imports
`app.brokers`, `app.risk` or `app.sizing`. The engine's output is a
`StrategySignal` and the path onward is Signal → AI → Risk → Sizing → OMS →
BrokerAdapter, none of which exists yet and none of which this shortens.

Three failure rules, each of which a test enforces:

1. **A strategy error is never a neutral signal.** A crashing strategy that
   returned HOLD would read as a quiet market. The engine records the error,
   returns an outcome that says `error`, and publishes nothing.
2. **Stale data does not produce a signal.** A rule fired on a bar that closed
   an hour ago is a decision about a market that has moved. Staleness is
   measured with L08's timeframe-derived rule, not a constant.
3. **A strategy error never crashes the platform.** One bad strategy stops
   itself, and the outcome names it so a bot supervisor can pause that
   strategy rather than the process.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.events import Event
from app.marketdata.types import Bar, Timeframe
from app.marketdata.validation import is_stale
from app.realtime.catalogue import EventType, Scope
from app.realtime.channels import Channel
from app.strategies.base import (
    Candles,
    SignalType,
    Strategy,
    StrategyError,
    StrategySignal,
)
from app.strategies.registry import StrategyRegistry

log = logging.getLogger("app.strategies.engine")


class Outcome(StrEnum):
    """What one evaluation produced. Deliberately not a trading status."""

    signal = "signal"  # an actionable signal was generated
    hold = "hold"  # the strategy looked and has no action
    no_signal = "no_signal"  # the strategy could not form a view
    insufficient_data = "insufficient_data"  # not enough bars for the warm-up
    stale_data = "stale_data"  # the newest bar is too old to act on
    error = "error"  # the strategy raised; nothing was published


@dataclass(frozen=True)
class Evaluation:
    """One run of one strategy over one series."""

    outcome: Outcome
    strategy_key: str
    symbol: str
    timeframe: Timeframe
    detail: str
    signal: StrategySignal | None = None
    error: str | None = None
    # Enough to answer "how active was this strategy" later without building
    # the analytics system now.
    bars_seen: int = 0
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def actionable(self) -> bool:
        return self.outcome is Outcome.signal

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": str(self.outcome),
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "detail": self.detail,
            "bars_seen": self.bars_seen,
            "evaluated_at": self.evaluated_at.isoformat(),
            "error": self.error,
            "signal": self.signal.as_dict() if self.signal else None,
            "note": (
                "a signal, not an order. Nothing here sized, approved or sent "
                "anything; the path onward is AI -> Risk -> Sizing -> OMS."
            ),
        }


@dataclass
class EngineCounters:
    """Activity, counted. Not the analytics system -- just enough for one."""

    evaluated: int = 0
    signals: int = 0
    holds: int = 0
    no_signals: int = 0
    insufficient_data: int = 0
    stale: int = 0
    errors: int = 0
    published: int = 0
    publish_failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "evaluated": self.evaluated,
            "signals": self.signals,
            "holds": self.holds,
            "no_signals": self.no_signals,
            "insufficient_data": self.insufficient_data,
            "stale": self.stale,
            "errors": self.errors,
            "published": self.published,
            "publish_failures": self.publish_failures,
        }


class StrategyEngine:
    """Runs strategies. Owns no connection and decides nothing about risk."""

    def __init__(self, registry: StrategyRegistry, hub: Any | None = None) -> None:
        self.registry = registry
        # Optional on purpose: a backtest and a replay run the same engine with
        # no hub, and nothing about the evaluation changes when there is none.
        self.hub = hub
        self.counters = EngineCounters()

    async def evaluate(
        self,
        strategy_key: str,
        symbol: str,
        timeframe: Timeframe,
        bars: list[Bar],
        *,
        config: dict[str, Any] | None = None,
        now: datetime | None = None,
        strategy_version: int | None = None,
        market_open: bool | None = None,
        correlation_id: str | None = None,
        publish: bool = True,
    ) -> Evaluation:
        """Run one strategy over one series and report what happened."""
        now = now or datetime.now(UTC)
        self.counters.evaluated += 1

        try:
            strategy: Strategy = self.registry.create(strategy_key, config)
        except StrategyError as exc:
            return self._error(strategy_key, symbol, timeframe, now, exc, 0)

        candles = Candles.of(symbol, timeframe, bars)
        need = strategy.required_data().min_bars

        if len(candles) < need:
            self.counters.insufficient_data += 1
            return Evaluation(
                Outcome.insufficient_data,
                strategy_key,
                symbol,
                timeframe,
                f"{len(candles)} closed bars; {strategy_key} needs {need}",
                bars_seen=len(candles),
                evaluated_at=now,
            )

        # Staleness is L08's rule, derived from the timeframe. A rule fired on
        # a bar that closed an hour ago is a decision about a market that has
        # moved, and one universal threshold would be wrong for every
        # instrument but one.
        if is_stale(candles.last.bar_time, timeframe, now, market_open=market_open):
            self.counters.stale += 1
            return Evaluation(
                Outcome.stale_data,
                strategy_key,
                symbol,
                timeframe,
                (
                    f"the newest closed bar is {candles.last.bar_time.isoformat()}, too "
                    f"old for {timeframe}. No signal was generated: acting on it would "
                    "be a decision about a market that has moved"
                ),
                bars_seen=len(candles),
                evaluated_at=now,
            )

        try:
            signal = strategy.generate_signal(candles, now=now)
        except Exception as exc:  # noqa: BLE001 - one bad strategy stops itself
            return self._error(strategy_key, symbol, timeframe, now, exc, len(candles))

        problem = validate_signal(signal, strategy_key, symbol, timeframe)
        if problem:
            # A malformed signal is an error, not a signal. Publishing one
            # would hand a subscriber something it cannot trust.
            return self._error(
                strategy_key, symbol, timeframe, now, StrategyError(problem), len(candles)
            )

        if signal.strategy_version is None and strategy_version is not None:
            signal = _with_version(signal, strategy_version)

        if signal.signal_type is SignalType.no_signal:
            self.counters.no_signals += 1
            outcome = Outcome.no_signal
        elif not signal.actionable:
            self.counters.holds += 1
            outcome = Outcome.hold
        else:
            self.counters.signals += 1
            outcome = Outcome.signal

        evaluation = Evaluation(
            outcome,
            strategy_key,
            symbol,
            timeframe,
            signal.reasoning,
            signal=signal,
            bars_seen=len(candles),
            evaluated_at=now,
        )

        log.info(
            "strategy evaluated",
            extra={
                "event": "strategy_evaluated",
                "strategy": strategy_key,
                "strategy_version": signal.strategy_version,
                "symbol": symbol,
                "timeframe": str(timeframe),
                "signal_type": str(signal.signal_type),
                "outcome": str(outcome),
                "bar_time": signal.bar_time.isoformat(),
                "correlation_id": correlation_id,
            },
        )

        if publish and outcome is Outcome.signal:
            await self._publish(signal, correlation_id)
        return evaluation

    # ------------------------------------------------------------ internals

    def _error(
        self,
        strategy_key: str,
        symbol: str,
        timeframe: Timeframe,
        now: datetime,
        exc: Exception,
        bars_seen: int,
    ) -> Evaluation:
        self.counters.errors += 1
        log.warning(
            "strategy failed",
            extra={
                "event": "strategy_error",
                "strategy": strategy_key,
                "symbol": symbol,
                "timeframe": str(timeframe),
                "error": type(exc).__name__,
            },
        )
        return Evaluation(
            Outcome.error,
            strategy_key,
            symbol,
            timeframe,
            # Never a neutral signal: a crashing strategy that returned HOLD
            # would read as a quiet market.
            "the strategy failed; no signal was generated",
            error=f"{type(exc).__name__}: {exc}"[:300],
            bars_seen=bars_seen,
            evaluated_at=now,
        )

    async def _publish(self, signal: StrategySignal, correlation_id: str | None) -> None:
        if self.hub is None:
            return
        event = Event(
            type=str(EventType.SIGNAL_CREATED),
            payload={
                "source": "strategy",
                "strategy_key": signal.strategy_key,
                "strategy_version": signal.strategy_version,
                "symbol": signal.symbol,
                "timeframe": str(signal.timeframe),
                "signal_type": str(signal.signal_type),
                "direction": signal.direction,
                "bar_time": signal.bar_time.isoformat(),
                "status": "new",
            },
            # Scoped to the strategy channel, which TRADER and above may watch.
            channel=str(Channel(Scope.strategy, signal.strategy_key)),
            correlation_id=correlation_id,
            source="strategy_engine",
        )
        try:
            await self.hub.publish(event)
            self.counters.published += 1
        except Exception:  # noqa: BLE001 - a lost event is not a lost decision
            self.counters.publish_failures += 1
            log.warning(
                "signal generated but the event was not published",
                extra={
                    "event": "strategy_publish_failed",
                    "strategy": signal.strategy_key,
                    "correlation_id": correlation_id,
                },
            )


def validate_signal(
    signal: StrategySignal, strategy_key: str, symbol: str, timeframe: Timeframe
) -> str | None:
    """Check the shape of what a strategy returned. None means it is well formed.

    A strategy that answered about a different symbol, or invented a
    confidence, is a bug that would otherwise travel downstream wearing a valid
    envelope.
    """
    if not isinstance(signal, StrategySignal):
        return f"{strategy_key} returned {type(signal).__name__}, not a StrategySignal"
    if signal.strategy_key != strategy_key:
        return (
            f"{strategy_key} returned a signal keyed {signal.strategy_key!r}; a strategy "
            "may not answer for another"
        )
    if signal.symbol != symbol:
        return f"{strategy_key} answered about {signal.symbol!r}, not {symbol!r}"
    if signal.timeframe is not timeframe:
        return f"{strategy_key} answered about {signal.timeframe}, not {timeframe}"
    if signal.confidence is not None and not (0 <= signal.confidence <= 1):
        return f"confidence must be between 0 and 1, not {signal.confidence}"
    if signal.reference_price is not None and signal.reference_price <= 0:
        return f"reference_price must be positive, not {signal.reference_price}"
    return None


def _with_version(signal: StrategySignal, version: int) -> StrategySignal:
    from dataclasses import replace

    return replace(signal, strategy_version=version)
