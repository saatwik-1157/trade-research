"""`BuiltStrategy`: a `StrategyDefinition` interpreted, never compiled.

This is the module that makes the builder safe. A definition arrives as data,
and this walks it: for a condition, look up two numbers and compare them; for a
group, combine the children. There is no step at which any part of the
definition becomes a Python expression, a string passed to `eval`, or a module
import. The set of things a user can express is exactly the set of things this
evaluator implements, and nothing else can be smuggled through.

It implements the same `Strategy` contract as the hand-written rules, so a
built strategy runs through the same engine, obeys the same no-look-ahead rule,
and produces the same `StrategySignal`. The engine cannot tell them apart, and
should not be able to.

**Indicators are computed over the closed bars only.** `Candles` has already
removed the forming bar, and every series is evaluated at its last index --
which is bar T -- with crosses reading T and T-1. There is no index into the
future to reach for, because the future is not in the array.

**Entry rules are evaluated before exit rules, and the first match wins.**
Stated rather than left to dictionary order: two rules that both match on one
bar would otherwise resolve differently between runs, which breaks
reproducibility.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.strategies.base import (
    Candles,
    ConfigError,
    DataRequirement,
    SignalTiming,
    SignalType,
    Strategy,
    StrategyMetadata,
    StrategySignal,
    StrategyTier,
)
from app.strategies.definition import (
    Comparison,
    Condition,
    DefinitionError,
    Group,
    Logical,
    Node,
    Operand,
    OperandKind,
    Rule,
    StrategyDefinition,
    parse_definition,
)
from app.strategies.indicators import compute


class BuiltStrategy(Strategy):
    """A strategy described by a definition rather than written by hand.

    Constructed from the definition itself rather than registered as a class,
    because there is one implementation and many definitions. The registry
    holds implementations; a built strategy is `BuiltStrategy(definition)`.
    """

    def __init__(
        self,
        definition: StrategyDefinition,
        *,
        key: str | None = None,
        version: int | None = None,
    ) -> None:
        self.definition = definition
        self._key = key or _slug(definition.name)
        self.version = version
        # Deliberately not calling super().__init__: the configuration IS the
        # definition, and it was validated when the definition was parsed.
        self.config: dict[str, Any] = {}

    # ------------------------------------------------------------ contract

    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            key=self._key,
            name=self.definition.name,
            description=self.definition.description or "; ".join(self.definition.summary()),
            # A built strategy has no measured evidence by construction. It has
            # never been backtested, let alone validated against a permutation
            # null, so it starts where every rule in this repository sits.
            tier=StrategyTier.research_only,
            timing=SignalTiming.bar_close,
            evidence=(
                "Built, not measured. No backtest has been run and no validation "
                "report is attached. It stays research_only until one is."
            ),
        )

    def required_data(self) -> DataRequirement:
        return DataRequirement(
            min_bars=self.definition.warmup(), timeframes=(self.definition.timeframe,)
        )

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        # A built strategy carries no separate config: the definition is the
        # configuration and it is validated on parse.
        if config:
            raise ConfigError(
                "a built strategy takes no separate configuration; its parameters live "
                "in the definition"
            )
        return {}

    @staticmethod
    def from_payload(
        payload: object, *, key: str | None = None, version: int | None = None
    ) -> BuiltStrategy:
        """Parse and validate a raw definition, then wrap it."""
        return BuiltStrategy(parse_definition(payload), key=key, version=version)

    # ----------------------------------------------------------- evaluation

    def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
        need = self.required_data().min_bars
        if len(candles) < need:
            return self._no_signal(
                candles,
                now,
                f"{len(candles)} closed bars; this definition needs {need} before its "
                "longest indicator is warmed up",
            )
        if candles.symbol != self.definition.symbol:
            # A definition names its instrument. Running it against another one
            # silently would produce signals nobody asked for.
            return self._no_signal(
                candles,
                now,
                f"this definition is for {self.definition.symbol}, not {candles.symbol}",
            )

        series = self._series(candles)
        last = candles.last
        # Entries first, then exits, first match wins. Stated so two matching
        # rules resolve the same way on every run.
        for rule in (*self.definition.entry_rules, *self.definition.exit_rules):
            if self._holds(rule.when, series, candles):
                return self._signal(rule, candles, now, last.close)

        return StrategySignal(
            strategy_key=self._key,
            symbol=candles.symbol,
            timeframe=candles.timeframe,
            signal_type=SignalType.hold,
            bar_time=last.bar_time,
            generated_at=now,
            reference_price=last.close,
            confidence=None,
            strategy_version=self.version,
            reasoning="no rule matched on this bar",
            metadata={"built": True, "rules": len(self.definition.entry_rules)},
        )

    def _signal(
        self, rule: Rule, candles: Candles, now: datetime, price: Decimal
    ) -> StrategySignal:
        last = candles.last
        return StrategySignal(
            strategy_key=self._key,
            symbol=candles.symbol,
            timeframe=candles.timeframe,
            signal_type=rule.then,
            bar_time=last.bar_time,
            generated_at=now,
            reference_price=price,
            # Built strategies assert no confidence. Nothing has measured one.
            confidence=None,
            strategy_version=self.version,
            reasoning=rule.label(),
            metadata={"built": True, "matched_rule": rule.label()},
        )

    # ------------------------------------------------------------ internals

    def _series(self, candles: Candles) -> dict[str, list[float | None]]:
        """Every referenced indicator, computed once over the closed bars."""
        opens, highs = candles.opens(), candles.highs()
        lows, closes = candles.lows(), candles.closes()
        out: dict[str, list[float | None]] = {}
        for operand in self.definition.indicators():
            out[operand.label()] = compute(operand.ref, operand.params, opens, highs, lows, closes)
        return out

    def _value(
        self,
        operand: Operand,
        series: dict[str, list[float | None]],
        candles: Candles,
        offset: int,
    ) -> float | None:
        """One operand's value at `offset` bars back from the last closed bar.

        `offset` is 0 for the current bar and 1 for the previous. It is never
        negative: there is no index into the future, and the future is not in
        the array to index into.
        """
        index = len(candles) - 1 - offset
        if index < 0:
            return None
        if operand.kind is OperandKind.constant:
            return operand.value
        if operand.kind is OperandKind.price:
            bar = candles.bars[index]
            raw = {
                "close": bar.close,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "volume": bar.volume,
            }[operand.ref]
            return None if raw is None else float(raw)
        values = series.get(operand.label())
        if values is None:  # pragma: no cover - _series covers every operand
            return None
        return values[index]

    def _holds(self, node: Node, series: dict[str, list[float | None]], candles: Candles) -> bool:
        if isinstance(node, Group):
            if node.logical is Logical.not_:
                return not self._holds(node.children[0], series, candles)
            results = [self._holds(c, series, candles) for c in node.children]
            return all(results) if node.logical is Logical.and_ else any(results)

        return self._condition_holds(node, series, candles)

    def _condition_holds(
        self, condition: Condition, series: dict[str, list[float | None]], candles: Candles
    ) -> bool:
        left = self._value(condition.left, series, candles, 0)
        right = self._value(condition.right, series, candles, 0)
        # An indicator that has not warmed up is None, not zero. A condition
        # against a substituted zero would fire for the wrong reason, so an
        # absent value means the condition simply does not hold.
        if left is None or right is None:
            return False

        comparison = condition.comparison
        if comparison is Comparison.greater_than:
            return left > right
        if comparison is Comparison.less_than:
            return left < right
        if comparison is Comparison.greater_or_equal:
            return left >= right
        if comparison is Comparison.less_or_equal:
            return left <= right
        if comparison is Comparison.equal:
            return left == right

        # A cross is defined by two bars. Both must be present; a cross that
        # cannot be evaluated has not happened.
        previous_left = self._value(condition.left, series, candles, 1)
        previous_right = self._value(condition.right, series, candles, 1)
        if previous_left is None or previous_right is None:
            return False
        if comparison is Comparison.crosses_above:
            return previous_left <= previous_right and left > right
        if comparison is Comparison.crosses_below:
            return previous_left >= previous_right and left < right
        raise DefinitionError(f"unhandled comparison {comparison}")  # pragma: no cover


def _slug(name: str) -> str:
    """A stable key from a name. Deterministic, and never executed."""
    cleaned = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned[:64] or "built_strategy"
