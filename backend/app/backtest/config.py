"""Backtest configuration, and the assumptions it makes visible.

**Every assumption is a field, and every field appears in the report.** That is
the whole design. A backtest whose costs were implicit would produce a number
nobody can argue with, and the one measured result this repository has is that
cost drag is the only effect large enough to see -- the `random` rule loses at
t = -3.60 because it pays the spread every time.

So there is **no zero-cost default**. `spread_points` has no default at all:
a caller must state it, and `CostModel.describe()` puts it in the report. The
project's own measurements say a single live quote understates this broker by
3-8x, and that bars recording a 0 spread are unrecorded rather than free.

Reproducibility (Step 20) is why `fingerprint()` exists: two identical
configurations over the same dataset produce the same hash, and the hash is
stored with the result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.marketdata.types import Provider, Timeframe

# What the engine is. Bumped when execution semantics change, so an old result
# is never silently compared against a new one.
ENGINE_VERSION = "1.0.0"

MAX_BARS = 200_000
MAX_DAYS = 3650


class ConfigError(Exception):
    """An invalid configuration. Refused, never adjusted into validity."""


class SizingMode(StrEnum):
    """How volume is chosen. All three are wired as of L18.

    They are the same three `app.sizing` offers, and the backtester calls that
    engine rather than carrying a formula of its own -- a `BacktestPositionSizer`
    beside a `LivePositionSizer` is two answers to one question, and the one
    that disagrees is always the one that was not being watched.

    The risk modes need the instrument's contract spec (tick size, tick value,
    volume step) and a per-trade stop distance. A run configured for one
    without a spec is REFUSED, not quietly sized at a flat lot.
    """

    fixed_quantity = "fixed_quantity"
    fixed_risk = "fixed_risk"
    percent_equity = "percent_equity"


@dataclass(frozen=True)
class CostModel:
    """What a round trip costs. No field defaults to zero.

    `spread_points` is charged **once per trade**, which is what
    `simulate()` does and what `test_spread_is_charged_once_per_trade` pins.
    """

    spread_points: Decimal
    commission_per_trade: Decimal = Decimal("0")
    slippage_points: Decimal = Decimal("0")
    # Signed, per night, in price units. Negative is a charge. AUDUSD pays to
    # be long at this broker and charges to be short, and flattening that to a
    # cost would be as wrong as ignoring it.
    swap_long_per_night: Decimal | None = None
    swap_short_per_night: Decimal | None = None

    def describe(self) -> dict[str, object]:
        return {
            "spread_points": str(self.spread_points),
            "commission_per_trade": str(self.commission_per_trade),
            "slippage_points": str(self.slippage_points),
            "swap_long_per_night": (
                str(self.swap_long_per_night) if self.swap_long_per_night is not None else None
            ),
            "swap_short_per_night": (
                str(self.swap_short_per_night) if self.swap_short_per_night is not None else None
            ),
            "financing_charged": self.swap_long_per_night is not None,
            "note": (
                "Spread is charged once per round trip. Financing is OFF unless both "
                "swap figures are given: every figure in this repository was measured "
                "without it, and a default that silently restated them would make the "
                "history unreadable."
            ),
        }


@dataclass(frozen=True)
class ExecutionModel:
    """When and how a signal becomes a fill.

    None of this is adjustable, and that is deliberate: these are the
    semantics `tools/rule_backtest.simulate` implements and that
    `tests/test_rule_backtest.py` pins. They are reported so a reader knows
    what the number means, not so a caller can pick a friendlier set.
    """

    entry: str = "next_bar_open"
    signal_timing: str = "bar_close"
    straddled_bar: str = "books_the_loss"
    overlapping_positions: str = "not_allowed"
    liquidity: str = "assumed_sufficient_at_the_bar"

    def describe(self) -> dict[str, str]:
        return {
            "entry": "the next bar's open; the signal reads bars up to i-1",
            "signal_timing": "evaluated on closed bars only",
            "straddled_bar": (
                "a bar covering both stop and target books the LOSS; intrabar order is "
                "unknown and resolving it in the strategy's favour is how a backtest "
                "flatters itself"
            ),
            "overlapping_positions": "flat before the next entry; one position at a time",
            "liquidity": (
                "assumed sufficient at the bar's open. This is an assumption, not a "
                "measurement: no order book is modelled"
            ),
            "bid_ask": (
                "not modelled. Only OHLC is available, so the spread model stands in "
                "for the book -- this data does not contain actual bid/ask"
            ),
        }


@dataclass(frozen=True)
class BacktestConfig:
    strategy_key: str
    symbol: str
    timeframe: Timeframe
    costs: CostModel
    provider: Provider = Provider.mt5
    strategy_config: dict = field(default_factory=dict)
    strategy_version: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    initial_capital: Decimal = Decimal("100000")
    account_currency: str = "USD"
    sizing_mode: SizingMode = SizingMode.fixed_quantity
    quantity: Decimal = Decimal("0.01")
    # Risk sizing (L18). `risk_amount` is a sum of account currency per trade;
    # `risk_percent` is a share of the equity as it stood BEFORE the trade
    # opened, which is what keeps the walk free of look-ahead.
    risk_amount: Decimal | None = None
    risk_percent: Decimal | None = None
    # ATR multiples for the bracket. These are `simulate()`'s own parameters;
    # a strategy's suggested stop is not used, and the report says so.
    stop_atr: Decimal = Decimal("1.5")
    target_atr: Decimal = Decimal("1.5")
    max_hold_bars: int = 240
    max_bars: int = 5000
    execution: ExecutionModel = field(default_factory=ExecutionModel)
    seed: int = 0

    def validate(self) -> None:
        if self.start and self.end and self.start >= self.end:
            raise ConfigError("start must be before end")
        if self.start and self.end and (self.end - self.start).days > MAX_DAYS:
            raise ConfigError(f"the window is longer than {MAX_DAYS} days")
        if self.initial_capital <= 0:
            raise ConfigError("initial_capital must be positive")
        if self.quantity <= 0:
            raise ConfigError("quantity must be positive")
        if self.costs.spread_points < 0:
            raise ConfigError("spread_points cannot be negative")
        if self.stop_atr <= 0 or self.target_atr <= 0:
            raise ConfigError("stop_atr and target_atr must be positive")
        if not (1 <= self.max_hold_bars <= 10_000):
            raise ConfigError("max_hold_bars must be between 1 and 10000")
        if not (100 <= self.max_bars <= MAX_BARS):
            raise ConfigError(f"max_bars must be between 100 and {MAX_BARS}")
        if self.sizing_mode is SizingMode.fixed_risk and (
            self.risk_amount is None or self.risk_amount <= 0
        ):
            raise ConfigError("fixed_risk sizing needs a positive risk_amount")
        if self.sizing_mode is SizingMode.percent_equity and (
            self.risk_percent is None or not (0 < self.risk_percent <= 100)
        ):
            raise ConfigError("percent_equity sizing needs a risk_percent between 0 and 100")

    def fingerprint(self) -> str:
        """A hash of everything that could change the result.

        Two identical configurations over the same dataset produce the same
        hash, so a stored result can be checked against the configuration that
        claims to have produced it.
        """
        material = json.dumps(
            {
                "engine": ENGINE_VERSION,
                "strategy": self.strategy_key,
                "strategy_config": self.strategy_config,
                "strategy_version": self.strategy_version,
                "symbol": self.symbol,
                "timeframe": str(self.timeframe),
                "provider": str(self.provider),
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None,
                "capital": str(self.initial_capital),
                "sizing": str(self.sizing_mode),
                "quantity": str(self.quantity),
                "risk_amount": str(self.risk_amount) if self.risk_amount is not None else None,
                "risk_percent": (str(self.risk_percent) if self.risk_percent is not None else None),
                "stop_atr": str(self.stop_atr),
                "target_atr": str(self.target_atr),
                "max_hold": self.max_hold_bars,
                "max_bars": self.max_bars,
                "costs": self.costs.describe(),
                "seed": self.seed,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def describe(self) -> dict[str, object]:
        data = {k: v for k, v in asdict(self).items() if k not in ("costs", "execution")}
        return {
            **{
                k: (str(v) if isinstance(v, Decimal | Timeframe | Provider) else v)
                for k, v in data.items()
            },
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "engine_version": ENGINE_VERSION,
            "fingerprint": self.fingerprint(),
            "costs": self.costs.describe(),
            "execution": self.execution.describe(),
            "bracket": (
                "The stop and target come from the ATR multiples above, not from the "
                "strategy's suggestion. A strategy's suggested bracket is not used by "
                "this engine."
            ),
        }
