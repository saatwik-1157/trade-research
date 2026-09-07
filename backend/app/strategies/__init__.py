"""Strategies: one contract, a registry, and an engine that cannot trade.

Nothing in this package imports a broker adapter, the risk engine, position
sizing or the OMS, and a test asserts that absence rather than trusting it. A
strategy produces a `StrategySignal`; the path onward is

    Signal -> AI -> Risk -> Sizing -> OMS -> BrokerAdapter

and none of it is shortened here.

The three built-in rules call `tools/mt5_paper`'s functions rather than
reimplementing them, so the engine runs exactly what produced the 252 recorded
demo trades. All three are `research_only`, which is a measurement and not
caution: none separates from a coin flip at this broker's spreads.
"""

from app.strategies.base import (
    Candles,
    ConfigError,
    DataRequirement,
    SignalTiming,
    SignalType,
    Strategy,
    StrategyError,
    StrategyMetadata,
    StrategySignal,
    StrategyTier,
)
from app.strategies.built import BuiltStrategy
from app.strategies.definition import (
    Comparison,
    DefinitionError,
    Logical,
    StrategyDefinition,
    parse_definition,
)
from app.strategies.engine import Evaluation, Outcome, StrategyEngine, validate_signal
from app.strategies.registry import (
    StrategyNotAvailable,
    StrategyRegistry,
    UnknownStrategy,
    default_registry,
)

__all__ = [
    "BuiltStrategy",
    "Candles",
    "Comparison",
    "DefinitionError",
    "ConfigError",
    "DataRequirement",
    "Evaluation",
    "Logical",
    "Outcome",
    "SignalTiming",
    "SignalType",
    "Strategy",
    "StrategyDefinition",
    "StrategyEngine",
    "StrategyError",
    "StrategyMetadata",
    "StrategyNotAvailable",
    "StrategyRegistry",
    "StrategySignal",
    "StrategyTier",
    "UnknownStrategy",
    "default_registry",
    "parse_definition",
    "validate_signal",
]
