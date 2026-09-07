"""The three live rules, wrapped in the Strategy interface.

**The trading logic is not reimplemented here.** Each `generate_signal` builds
the recarray `tools/mt5_paper` expects and calls `rule_sma_cross`,
`rule_rsi_reversion` or `rule_random` -- the exact functions that produced the
252 trades in `data/track_record.jsonl`. A regression test calls the toolkit
function directly on the same input and asserts the two agree, because the
point of an interface migration is that behaviour does not move.

Why the *live* functions and not `rule_backtest.signals_*`: those are the
vectorised twins used by the backtester, and `tests/test_rule_backtest.py`'s
`test_indicators_match_live` already pins them to each other. Wrapping the live
side means the engine runs what actually traded, and the existing parity test
keeps the backtest honest about it.

**One deliberate behavioural difference, and it is documented rather than
hidden.** The toolkit rules drop the forming bar themselves with
`rates["close"][:-1]`. `Candles.of` has already dropped it, so passing the
candles through unchanged would drop a second, real bar and shift every signal
back one. The adapter therefore appends a **duplicate of the last closed bar**
as a stand-in for the forming bar, so the rule's own `[:-1]` removes the
stand-in and sees exactly the closed history it would have seen live. A test
pins this: the wrapper's signal equals the toolkit's on the same closed set.

What each rule *is* is in CLAUDE.md and repeated in `evidence` below. Neither
`rsi_reversion` nor `sma_cross` separates from a coin flip on 20,000 H1 bars
across seven majors, and at this broker's real spreads both are net negative.
That is why every one of them is `research_only`.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.marketdata.types import Timeframe
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

# Warm-up requirements, taken from the rules' own guards rather than guessed.
# `rule_sma_cross` returns None below 60 closed bars and `rule_rsi_reversion`
# below 3*n. One extra bar covers the stand-in the adapter appends.
SMA_MIN_BARS = 61
RSI_MIN_BARS = 43


def _toolkit():  # noqa: ANN202 - the toolkit module, imported lazily
    """Import `tools/mt5_paper.py` without moving or copying it.

    Added to `sys.path` rather than vendored, so every command in README.md and
    NIGHTLY.md keeps running against the same source and a fix lands once. The
    module imports MetaTrader5 inside `connect()`, not at module scope, so this
    works on a host with no terminal.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    tools = os.path.join(root, "tools")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.insert(0, tools)
    import mt5_paper

    return mt5_paper


def rates_from(candles: Candles):  # noqa: ANN201 - numpy recarray
    """Closed bars -> the recarray the toolkit rules read.

    The last closed bar is duplicated as a stand-in for the forming bar the
    live loop would have had, because the rules drop their own last element.
    Without it the rules would see one bar less than they did live and every
    signal would shift back by one, which is a behaviour change wearing an
    interface migration.
    """
    import numpy as np

    if not candles.bars:
        raise ValueError("no closed bars")
    closes = [float(b.close) for b in candles.bars]
    opens = [float(b.open) for b in candles.bars]
    highs = [float(b.high) for b in candles.bars]
    lows = [float(b.low) for b in candles.bars]
    times = [int(b.bar_time.timestamp()) for b in candles.bars]
    # The stand-in. Its values are never read: the rules slice it off.
    closes.append(closes[-1])
    opens.append(opens[-1])
    highs.append(highs[-1])
    lows.append(lows[-1])
    times.append(times[-1])
    # numpy's stubs cannot resolve this overload without `formats=`, which the
    # runtime does not need; the call is the documented `names=` form. Whether
    # the stubs flag it at all moves with the numpy version, so the suppression
    # covers its own disuse -- otherwise `warn_unused_ignores` fails CI on the
    # versions that happen to accept the call.
    return np.rec.fromarrays(  # type: ignore[call-overload, unused-ignore]
        [np.array(times), np.array(opens), np.array(highs), np.array(lows), np.array(closes)],
        names="time,open,high,low,close",
    )


def _no_extra_keys(config: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(config) - allowed
    if unknown:
        # Refused, not ignored: a typo'd parameter that is silently dropped
        # runs a strategy nobody configured.
        raise ConfigError(
            f"unknown configuration keys {sorted(unknown)}; this strategy accepts "
            f"{sorted(allowed) or 'no parameters'}"
        )


def _positive_int(config: dict[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    raw = config.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{key} must be an integer, not {type(raw).__name__}")
    if not (low <= raw <= high):
        raise ConfigError(f"{key} must be between {low} and {high}, not {raw}")
    return raw


class _ToolkitRule(Strategy):
    """Shared adapter for a rule that lives in `tools/mt5_paper.py`."""

    rule_name: str = ""
    min_bars: int = 60

    def required_data(self) -> DataRequirement:
        return DataRequirement(
            min_bars=self.min_bars,
            # The rules are timeframe-agnostic -- they read closes. The live
            # loop runs H1 and the searches ran H1, H4 and D1, so those are
            # declared; nothing stops another, and nothing claims evidence for
            # one.
            timeframes=(Timeframe.H1, Timeframe.H4, Timeframe.D1),
        )

    def _call_rule(self, rates) -> str | None:  # noqa: ANN001
        raise NotImplementedError

    def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
        need = self.required_data().min_bars
        if len(candles) < need:
            return self._no_signal(
                candles,
                now,
                f"{len(candles)} closed bars; this rule needs {need} before its "
                "indicator is an indicator rather than a number",
            )
        raw = self._call_rule(rates_from(candles))
        last = candles.last
        signal_type = {
            "buy": SignalType.entry_long,
            "sell": SignalType.entry_short,
        }.get(raw or "", SignalType.hold)
        return StrategySignal(
            strategy_key=self.metadata().key,
            symbol=candles.symbol,
            timeframe=candles.timeframe,
            signal_type=signal_type,
            bar_time=last.bar_time,
            generated_at=now,
            reference_price=last.close,
            # No confidence is asserted. No rule here has a measured basis for
            # one, and an invented number reads downstream as evidence.
            confidence=None,
            reasoning=(
                f"{self.metadata().key} on the bar closing {last.bar_time.isoformat()}: "
                f"{raw or 'no signal'}"
            ),
            metadata={
                "toolkit_rule": self.rule_name,
                "raw_signal": raw,
                "closed_bars": len(candles),
            },
        )


class SmaCross(_ToolkitRule):
    """`mt5_paper.rule_sma_cross`, unchanged: 20/50 SMA cross on closed bars."""

    rule_name = "sma_cross"
    min_bars = SMA_MIN_BARS

    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            key="sma_cross",
            name="SMA 20/50 cross",
            description="Long when the 20 crosses above the 50, short on the reverse.",
            tier=StrategyTier.research_only,
            timing=SignalTiming.bar_close,
            evidence=(
                "Measured on 20,000 H1 bars across 7 FX majors: does not separate from "
                "a coin flip, and at this broker's real spreads is net -6.54 points per "
                "trade. A 36-cell bracket sweep found no configuration reaching an "
                "uncorrected t of 1.96. See reports/rule_backtest.json."
            ),
        )

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        # The live rule hardcodes 20 and 50. Accepting parameters here would
        # mean this strategy is not the one that traded, so it takes none.
        _no_extra_keys(config, set())
        return {}

    def _call_rule(self, rates) -> str | None:  # noqa: ANN001
        return _toolkit().rule_sma_cross(rates)


class RsiReversion(_ToolkitRule):
    """`mt5_paper.rule_rsi_reversion`, unchanged: Wilder RSI, 30/70 bands."""

    rule_name = "rsi_reversion"
    min_bars = RSI_MIN_BARS

    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            key="rsi_reversion",
            name="RSI reversion 14 (30/70)",
            description="Long below RSI 30, short above 70, on closed bars.",
            tier=StrategyTier.research_only,
            timing=SignalTiming.bar_close,
            evidence=(
                "Measured on 20,000 H1 bars across 7 FX majors: does not separate from "
                "a coin flip, net -1.50 points per trade at real spreads, and averages "
                "about a point of win rate BELOW the 50.5-52.7% breakeven the spread "
                "hurdle requires. See reports/rule_backtest.json and cost_hurdle.json."
            ),
        )

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        _no_extra_keys(config, {"period"})
        period = _positive_int(config, "period", 14, low=2, high=200)
        return {"period": period}

    def _call_rule(self, rates) -> str | None:  # noqa: ANN001
        return _toolkit().rule_rsi_reversion(rates, self.config["period"])

    def required_data(self) -> DataRequirement:
        base = super().required_data()
        # 3*n is the rule's own guard, plus one for the stand-in bar.
        return DataRequirement(min_bars=3 * self.config["period"] + 1, timeframes=base.timeframes)


class RandomRule(_ToolkitRule):
    """`mt5_paper.rule_random`, with its randomness made explicit.

    The benchmark every other rule has to beat to mean anything -- and the one
    that produced this project's most instructive number: a t-statistic of 9.33
    on 14 trades, which was arithmetic rather than evidence. A random entry
    with a 6:1 adverse bracket wins about six times in seven by construction;
    the losses had not landed yet.

    The toolkit function uses the global `random` module and is therefore not
    reproducible. Step 27 requires randomness to be explicit and controllable,
    so this wrapper seeds a private generator from the strategy's `seed`
    config and the bar time. The *distribution* is unchanged -- buy, sell,
    None, None -- so the benchmark still measures what it measured, and the
    same bar now always produces the same draw, which is what a backtest and a
    replay need.
    """

    rule_name = "random"
    min_bars = 1

    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            key="random",
            name="Random (benchmark)",
            description="A coin flip. The null every other rule must beat.",
            tier=StrategyTier.research_only,
            timing=SignalTiming.bar_close,
            evidence=(
                "Not a strategy. The only result in the whole exercise that reaches "
                "significance is that this rule LOSES, at t = -3.60: cost drag is the "
                "one effect large enough to measure. Its 93% win rate under a 6:1 "
                "adverse bracket is arithmetic, not edge."
            ),
        )

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        _no_extra_keys(config, {"seed"})
        seed = config.get("seed", 0)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ConfigError("seed must be an integer")
        return {"seed": seed}

    def generate_signal(self, candles: Candles, *, now: datetime) -> StrategySignal:
        import random as _random

        if not candles.bars:
            return self._no_signal(candles, now, "no closed bars")
        last = candles.last
        # Seeded from the bar, so the same bar always draws the same way and a
        # replay reproduces a run exactly.
        rng = _random.Random(f"{self.config['seed']}:{candles.symbol}:{last.bar_time}")
        raw = rng.choice(["buy", "sell", None, None])
        signal_type = {
            "buy": SignalType.entry_long,
            "sell": SignalType.entry_short,
        }.get(raw or "", SignalType.hold)
        return StrategySignal(
            strategy_key="random",
            symbol=candles.symbol,
            timeframe=candles.timeframe,
            signal_type=signal_type,
            bar_time=last.bar_time,
            generated_at=now,
            reference_price=last.close,
            confidence=None,
            reasoning="coin flip; this is the benchmark, not a prediction",
            metadata={"toolkit_rule": "random", "raw_signal": raw, "seed": self.config["seed"]},
        )


def _price(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


BUILT_IN: tuple[type[Strategy], ...] = (SmaCross, RsiReversion, RandomRule)
