"""The indicator catalogue the builder may use.

**Four indicators, and no more, because four is what the numpy rule pipeline
actually has.** `wilder_rsi`, `sma` and `atr_series` live in
`tools/rule_backtest.py` and `ema` in `tools/rule_search.py`; they are called
here, not reimplemented, so a builder strategy and a searched candidate compute
the same RSI.

`tools/indicators.py` has MACD, Bollinger, ADX and more, and they are
deliberately **not** exposed. They are pandas functions built for the equity
snapshot pipeline over a different data shape. Offering them here would mean
either a second implementation or a conversion layer, and Step 4 is explicit:
do not add indicators merely for visual variety.

Every indicator declares its parameters with bounds, and every parameter is
validated before an indicator is ever computed. An RSI period of 0 is not a
degenerate RSI, it is a division by zero waiting for market data.

**Units matter and are declared.** `Unit` is what makes `PRICE > RSI`
refusable: a price and an oscillator are not the same quantity, and comparing
them is the metals-points error in miniature. The builder checks units before
it checks anything else.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Unit(StrEnum):
    """What kind of quantity a value is.

    Two operands may only be compared when their units match. This is the
    check that refuses `PRICE > RSI` -- one is a price level and the other is a
    0-100 oscillator, and a comparison between them is arithmetic that means
    nothing.
    """

    price = "price"  # a price level, in the instrument's own quote units
    oscillator = "oscillator"  # bounded 0-100
    volatility = "volatility"  # a price *distance*, not a level
    volume = "volume"
    ratio = "ratio"  # dimensionless
    # A bare number. Compatible with anything, because a constant takes its
    # meaning from what it is compared against.
    scalar = "scalar"


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: str  # "int" | "float"
    default: int | float
    minimum: int | float
    maximum: int | float
    step: int | float = 1
    description: str = ""

    def validate(self, value: Any) -> int | float:
        if isinstance(value, bool):
            raise ValueError(f"{self.name} must be a number, not a boolean")
        if self.kind == "int":
            if not isinstance(value, int):
                raise ValueError(f"{self.name} must be an integer, not {type(value).__name__}")
        else:
            if not isinstance(value, int | float):
                raise ValueError(f"{self.name} must be a number, not {type(value).__name__}")
            value = float(value)
        if not (self.minimum <= value <= self.maximum):
            raise ValueError(
                f"{self.name} must be between {self.minimum} and {self.maximum}, not {value}"
            )
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "step": self.step,
            "description": self.description,
        }


@dataclass(frozen=True)
class IndicatorSpec:
    """One indicator: what it is, what it takes, and what it returns."""

    key: str
    name: str
    unit: Unit
    parameters: tuple[ParameterSpec, ...]
    description: str
    # How many bars it needs before its output means anything, as a multiple of
    # its longest period. An RSI over three bars is a number and not an RSI.
    warmup_multiplier: int = 3

    def validate_params(self, given: dict[str, Any]) -> dict[str, int | float]:
        known = {p.name for p in self.parameters}
        unknown = set(given) - known
        if unknown:
            raise ValueError(
                f"{self.key} has no parameter {sorted(unknown)}; it accepts "
                f"{sorted(known) or 'none'}"
            )
        return {p.name: p.validate(given.get(p.name, p.default)) for p in self.parameters}

    def warmup(self, params: dict[str, int | float]) -> int:
        longest = max((int(v) for v in params.values()), default=1)
        return max(longest * self.warmup_multiplier, longest + 1)

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "unit": str(self.unit),
            "description": self.description,
            "parameters": [p.as_dict() for p in self.parameters],
        }


def _period(default: int, low: int = 2, high: int = 500) -> ParameterSpec:
    return ParameterSpec(
        "period", "int", default, low, high, 1, "Lookback in bars. Must be at least 2."
    )


CATALOGUE: dict[str, IndicatorSpec] = {
    "SMA": IndicatorSpec(
        key="SMA",
        name="Simple moving average",
        unit=Unit.price,
        parameters=(_period(20),),
        description="Mean close over the period. A price level.",
    ),
    "EMA": IndicatorSpec(
        key="EMA",
        name="Exponential moving average",
        unit=Unit.price,
        parameters=(_period(20),),
        description="Exponentially weighted mean close. A price level.",
    ),
    "RSI": IndicatorSpec(
        key="RSI",
        name="Wilder RSI",
        unit=Unit.oscillator,
        parameters=(_period(14),),
        description=(
            "Wilder's relative strength index, 0-100. The same function the live "
            "rule uses, so a built strategy and rule_rsi_reversion agree."
        ),
    ),
    "ATR": IndicatorSpec(
        key="ATR",
        name="Average true range",
        unit=Unit.volatility,
        parameters=(_period(14),),
        description=(
            "Average true range: a price DISTANCE, not a level. Comparing it to a "
            "price is refused, because a range and a level are not the same quantity."
        ),
    ),
}

# Price and volume fields a condition may read directly. `close` is the one a
# rule should normally use: the open, the body and the shadows are unreliable on
# some feeds, which `market.validate_ohlcv` measures per source.
PRICE_FIELDS: dict[str, Unit] = {
    "close": Unit.price,
    "open": Unit.price,
    "high": Unit.price,
    "low": Unit.price,
    "volume": Unit.volume,
}


def spec_for(key: str) -> IndicatorSpec:
    try:
        return CATALOGUE[key]
    except KeyError as exc:
        raise ValueError(
            f"unknown indicator {key!r}; available indicators are {', '.join(sorted(CATALOGUE))}"
        ) from exc


def _toolkit(module: str):  # noqa: ANN202
    """Import a toolkit module without copying it."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    tools = os.path.join(root, "tools")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.insert(0, tools)
    import importlib

    # A fixed module name from this file's own constants, never from user
    # input. The two callers below pass literals.
    return importlib.import_module(module)


def compute(
    key: str,
    params: dict[str, int | float],
    opens: list[Decimal],
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
) -> list[float | None]:
    """One indicator series, computed by the toolkit's own functions.

    Returns a list the same length as the input, with `None` where the
    indicator has not warmed up. `None` rather than 0: a zero RSI is a reading
    and an absent one is not, and a condition evaluated against a substituted
    zero fires for the wrong reason.
    """
    import numpy as np

    backtest = _toolkit("rule_backtest")
    close_array = np.array([float(c) for c in closes], dtype=float)

    if key == "SMA":
        series = backtest.sma(close_array, int(params["period"]))
    elif key == "EMA":
        search = _toolkit("rule_search")
        series = search.ema(close_array, int(params["period"]))
    elif key == "RSI":
        series = backtest.wilder_rsi(close_array, int(params["period"]))
    elif key == "ATR":
        series = backtest.atr_series(
            np.array([float(h) for h in highs], dtype=float),
            np.array([float(low) for low in lows], dtype=float),
            close_array,
            int(params["period"]),
        )
    else:  # pragma: no cover - spec_for refuses first
        raise ValueError(f"unknown indicator {key!r}")

    return [None if v is None or np.isnan(v) else float(v) for v in series]


def catalogue() -> list[dict[str, object]]:
    return [spec.as_dict() for spec in CATALOGUE.values()]
