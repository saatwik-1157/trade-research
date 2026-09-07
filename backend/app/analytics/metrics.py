"""The metric definitions. One implementation, and it is this one.

Section 5: *"Do not silently change metric definitions between screens. Create a
centralized metric implementation."*

The audit found **three** implementations of win rate, profit factor and maximum
drawdown already in the repository:

    app/backtest/runner.py     compute_metrics   — denominated in POINTS
    app/training/metrics.py    economic          — denominated in LABEL RETURNS
    app/services/journal.py    statistics        — SQL over `trades`, ACCOUNT CURRENCY

They agree today. Three copies of one formula do not stay agreeing, and the one
nobody looked at is the one somebody quotes — which is the reasoning L18 recorded
when it pulled `value_per_price_unit` out of the paper portfolio, and L22 and L27
when they extracted `AiVerdict` and `AiPolicy`. This module is that extraction
for the metric formulas, and the existing callers delegate to it.

**The unit travels with the numbers.** `Series` carries one, and `core()` refuses
to pool two series with different units. This is the metals-points lesson written
as a type: median H1 ATR is 160 points in silver against 9,386 in palladium, and
pooling those produced the +4,236-point "result" that `CLAUDE.md` records as
arithmetic rather than a finding. Points, R and account currency are three
different quantities and a metric set that did not say which it was in would be
quoted as whichever the reader assumed.

**A sample too small returns INSUFFICIENT_DATA, never a number.** Section 9 and
§25. A ratio from six trades is noise wearing a decimal point, and this project's
own live record is the cautionary case: a t of 9.33 that meant nothing because a
random entry with a 6:1 adverse bracket wins six times in seven by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

#: Sentinel for a metric that could not be computed from the sample available.
#: A STRING rather than `None`, so it survives JSON and a reader cannot mistake
#: it for "zero" or for a field the API forgot. §9 and §25.
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

#: Below this, a mean-over-deviation ratio is noise. Twenty is the figure
#: `app/backtest/runner.py` already chose, kept rather than re-picked so the
#: backtest and the live journal answer the same question the same way.
MIN_TRADES_FOR_RATIO = 20

#: Below this, a distribution statistic (median, percentile, deviation) is not
#: worth reporting. Lower than the ratio floor because a median is a far weaker
#: claim than a Sharpe.
MIN_TRADES_FOR_DISTRIBUTION = 5


class Unit(StrEnum):
    """What the numbers in a series ARE. Never assumed, never defaulted.

    `points` and `currency` are not interchangeable and neither pools with `r`.
    The three are separated because this repository has already paid for
    conflating two of them.
    """

    #: Price movement in the symbol's own point size. NOT poolable across
    #: symbols whose point sizes differ by more than ~5x -- `rule_search.py`
    #: measures that spread and raises a data gap when it is exceeded.
    points = "points"
    #: Account currency. NOT poolable across trades sized by different stop
    #: distances: `track_record.py` raises a data gap at a 2x spread, because
    #: the money at risk is what differs and the currency figure hides it.
    currency = "currency"
    #: Realised P&L divided by the money at risk. THE poolable one, and the
    #: reason `CLAUDE.md` tells the reader to quote R rather than net.
    r = "r"
    #: A fractional return. Poolable within one instrument.
    percent = "percent"


#: Which units may be pooled across heterogeneous instruments without a warning.
POOLABLE: frozenset[Unit] = frozenset({Unit.r, Unit.percent})


class UnitMismatch(Exception):
    """Two series in different units were about to be combined."""


@dataclass(frozen=True)
class Observation:
    """One completed trade, as the metric layer needs it.

    Deliberately tiny. Analytics reads what the trade journal recorded and adds
    nothing: §55 says the journal owns the lifecycle, and an observation that
    carried an eleventh field would be the second trade representation §41 warns
    against.
    """

    value: float
    at: datetime
    opened_at: datetime | None = None
    #: Costs, kept apart so a gross and a net series can be built from one read.
    costs: float = 0.0

    @property
    def duration(self) -> timedelta | None:
        if self.opened_at is None:
            return None
        return self.at - self.opened_at


@dataclass(frozen=True)
class Series:
    """Observations that share a unit, in time order."""

    observations: tuple[Observation, ...]
    unit: Unit

    @classmethod
    def of(cls, values: list[Observation], unit: Unit) -> Series:
        return cls(tuple(sorted(values, key=lambda o: o.at)), unit)

    def __len__(self) -> int:
        return len(self.observations)

    @property
    def values(self) -> list[float]:
        return [o.value for o in self.observations]

    def concat(self, other: Series) -> Series:
        """Two series joined -- and refused when the units disagree."""
        if self.unit is not other.unit:
            raise UnitMismatch(
                f"a {self.unit} series cannot be pooled with a {other.unit} one. "
                "They are different quantities, and adding them produces a number "
                "in no unit at all."
            )
        return Series.of(list(self.observations) + list(other.observations), self.unit)


# =========================================================== the definitions
#
# Each one is a named function rather than an inline expression, so the
# definition has one home and a test can name it. Section 5 asks for exactly
# that, and the docstrings ARE the definitions.


def win_rate(values: list[float]) -> float | str:
    """winning trades / total closed trades.

    A break-even trade (exactly zero) counts in the denominator and in neither
    numerator. Dropping it would inflate both the win rate and the loss rate,
    and they would no longer sum to less than one in the way a reader expects.
    """
    if not values:
        return INSUFFICIENT_DATA
    return sum(1 for v in values if v > 0) / len(values)


def loss_rate(values: list[float]) -> float | str:
    if not values:
        return INSUFFICIENT_DATA
    return sum(1 for v in values if v < 0) / len(values)


def gross_profit(values: list[float]) -> float:
    """The sum of the winners. Always >= 0."""
    return sum(v for v in values if v > 0)


def gross_loss(values: list[float]) -> float:
    """The absolute sum of the losers. Always >= 0, so the ratio reads naturally."""
    return -sum(v for v in values if v < 0)


def net_profit(values: list[float]) -> float:
    return sum(values)


def profit_factor(values: list[float]) -> float | str:
    """gross profit / abs(gross loss).

    `INSUFFICIENT_DATA` when there are no losers, NOT infinity and not a large
    number. A run of winners with no loss has an undefined profit factor, and
    reporting a big one invites reading a small sample as a strong edge -- which
    is precisely how this repository's 93%-win-rate live regime looked decisive
    while being arithmetic.
    """
    if not values:
        return INSUFFICIENT_DATA
    loss = gross_loss(values)
    if loss <= 0:
        return INSUFFICIENT_DATA
    return gross_profit(values) / loss


def expectancy(values: list[float]) -> float | str:
    """The average trade result. Section 5 defines it as average trade P&L."""
    if not values:
        return INSUFFICIENT_DATA
    return sum(values) / len(values)


def payoff_ratio(values: list[float]) -> float | str:
    """average win / abs(average loss)."""
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    if not wins or not losses:
        return INSUFFICIENT_DATA
    return (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))


def standard_deviation(values: list[float]) -> float | str:
    """The sample deviation, n-1. `INSUFFICIENT_DATA` below two observations."""
    if len(values) < 2:
        return INSUFFICIENT_DATA
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def downside_deviation(values: list[float], *, target: float = 0.0) -> float | str:
    """Root-mean-square of the observations BELOW target.

    Divided by the count of downside observations, not by the total. Both
    conventions exist; this one is what `app/backtest/runner.py` already used,
    and §9 asks that the definition be documented rather than that a particular
    one be chosen.
    """
    downside = [v for v in values if v < target]
    if not downside:
        return INSUFFICIENT_DATA
    return math.sqrt(sum((v - target) ** 2 for v in downside) / len(downside))


def sharpe_per_trade(values: list[float], *, minimum: int = MIN_TRADES_FOR_RATIO) -> float | str:
    """mean / standard deviation, PER TRADE. Not annualised.

    §9 asks for the annualisation assumption to be documented, and the honest
    answer is that there is none: annualising needs a trades-per-year figure
    that a fixed window does not supply, and inventing one is how a Sharpe of
    0.3 becomes a Sharpe of 2. The name carries the caveat.

    The risk-free rate is ZERO and that is a stated assumption, not an
    oversight: these are per-trade excess returns over no position, and a
    financing rate belongs in the trade's own costs where `swap.py` already
    charges it.
    """
    if len(values) < minimum:
        return INSUFFICIENT_DATA
    sd = standard_deviation(values)
    if not isinstance(sd, float) or sd <= 0:
        return INSUFFICIENT_DATA
    return (sum(values) / len(values)) / sd


def sortino_per_trade(values: list[float], *, minimum: int = MIN_TRADES_FOR_RATIO) -> float | str:
    """mean / downside deviation, per trade. Same caveats as Sharpe."""
    if len(values) < minimum:
        return INSUFFICIENT_DATA
    dd = downside_deviation(values)
    if not isinstance(dd, float) or dd <= 0:
        return INSUFFICIENT_DATA
    return (sum(values) / len(values)) / dd


def t_statistic(values: list[float]) -> float | str:
    """mean / (sd / sqrt(n)). Reported, and never reported alone.

    `CLAUDE.md` is unambiguous about this figure: *"Do not quote a live t-stat
    without saying how many trades it rests on."* So `core()` always returns it
    beside `trades`, and the caveat travels in the payload.
    """
    if len(values) < 2:
        return INSUFFICIENT_DATA
    sd = standard_deviation(values)
    if not isinstance(sd, float) or sd <= 0:
        return INSUFFICIENT_DATA
    return (sum(values) / len(values)) / (sd / math.sqrt(len(values)))


@dataclass(frozen=True)
class Streaks:
    """Section 19. Consecutive runs, over COMPLETED trades in time order."""

    longest_win: int = 0
    longest_loss: int = 0
    current_win: int = 0
    current_loss: int = 0
    longest_breakeven: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "longest_win": self.longest_win,
            "longest_loss": self.longest_loss,
            "current_win": self.current_win,
            "current_loss": self.current_loss,
            "longest_breakeven": self.longest_breakeven,
            "note": (
                "consecutive completed trades in close-time order. A partial close is "
                "not an independent trade -- the journal records one row per position "
                "episode, so a scale-out cannot inflate a streak."
            ),
        }


def streaks(values: list[float]) -> Streaks:
    """Longest and current runs. A zero breaks both win and loss runs."""
    longest_win = longest_loss = longest_flat = 0
    run_win = run_loss = run_flat = 0
    for value in values:
        if value > 0:
            run_win, run_loss, run_flat = run_win + 1, 0, 0
        elif value < 0:
            run_win, run_loss, run_flat = 0, run_loss + 1, 0
        else:
            run_win, run_loss, run_flat = 0, 0, run_flat + 1
        longest_win = max(longest_win, run_win)
        longest_loss = max(longest_loss, run_loss)
        longest_flat = max(longest_flat, run_flat)
    return Streaks(
        longest_win=longest_win,
        longest_loss=longest_loss,
        current_win=run_win,
        current_loss=run_loss,
        longest_breakeven=longest_flat,
    )


def percentile(values: list[float], fraction: float) -> float | str:
    """Linear-interpolated percentile over the sorted sample."""
    if len(values) < MIN_TRADES_FOR_DISTRIBUTION:
        return INSUFFICIENT_DATA
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def distribution(values: list[float]) -> dict[str, Any]:
    """Section 18. Shape, without removing anything.

    **Outliers are reported, never trimmed.** §18 is explicit that extreme
    trades must not be silently removed, and the reason is this repository's own
    history: the largest figures it has produced were the ones that turned out to
    be unit errors, and a trimmed distribution would have hidden the very
    observations that revealed them.
    """
    if not values:
        return {"count": 0, "note": "no observations"}
    return {
        "count": len(values),
        "mean": expectancy(values),
        "median": percentile(values, 0.5),
        "p05": percentile(values, 0.05),
        "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75),
        "p95": percentile(values, 0.95),
        "min": min(values),
        "max": max(values),
        "standard_deviation": standard_deviation(values),
        "note": (
            "nothing is trimmed. The largest figures this repository has produced "
            "were unit errors, and a winsorised distribution would have hidden the "
            "observations that revealed them."
        ),
    }


def durations(series: Series) -> dict[str, Any]:
    """Section 5's duration figures, in seconds, exact from the timestamps."""
    spans = [o.duration.total_seconds() for o in series.observations if o.duration is not None]
    if not spans:
        return {
            "available": False,
            "why": "no observation carries both an open and a close timestamp",
        }
    return {
        "available": True,
        "average_seconds": sum(spans) / len(spans),
        "median_seconds": percentile(spans, 0.5),
        "longest_seconds": max(spans),
        "shortest_seconds": min(spans),
        "counted": len(spans),
        "note": "closed_at - opened_at, exactly. Never bucketed or rounded.",
    }


@dataclass
class Core:
    """Every §5 figure for one series, computed once."""

    unit: Unit
    trades: int = 0
    values: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        v = self.values
        wins = [x for x in v if x > 0]
        losses = [x for x in v if x < 0]
        return {
            "unit": str(self.unit),
            "poolable_across_instruments": self.unit in POOLABLE,
            "trades": self.trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "breakeven_trades": self.trades - len(wins) - len(losses),
            "win_rate": win_rate(v),
            "loss_rate": loss_rate(v),
            "gross_profit": gross_profit(v),
            "gross_loss": gross_loss(v),
            "net_profit": net_profit(v),
            "average_trade": expectancy(v),
            "average_win": (sum(wins) / len(wins)) if wins else INSUFFICIENT_DATA,
            "average_loss": (sum(losses) / len(losses)) if losses else INSUFFICIENT_DATA,
            "largest_win": max(wins) if wins else INSUFFICIENT_DATA,
            "largest_loss": min(losses) if losses else INSUFFICIENT_DATA,
            "payoff_ratio": payoff_ratio(v),
            "profit_factor": profit_factor(v),
            "expectancy": expectancy(v),
            "standard_deviation": standard_deviation(v),
            "sharpe_per_trade": sharpe_per_trade(v),
            "sortino_per_trade": sortino_per_trade(v),
            "t_statistic": t_statistic(v),
            "streaks": streaks(v).as_dict(),
            "minimum_for_ratio": MIN_TRADES_FOR_RATIO,
            "sample_note": (
                f"{self.trades} trades. A ratio below {MIN_TRADES_FOR_RATIO} is reported "
                "as INSUFFICIENT_DATA rather than as a number, and a t-statistic is "
                "never meaningful without the count beside it."
            ),
        }


def core(series: Series) -> dict[str, Any]:
    """Every §5 metric for one series, plus its distribution and durations."""
    block = Core(unit=series.unit, trades=len(series), values=series.values).as_dict()
    block["distribution"] = distribution(series.values)
    block["durations"] = durations(series)
    return block


def empty(unit: Unit, *, why: str = "no completed trades match this filter") -> dict[str, Any]:
    """The zero-trade answer. Section 48: an empty state, never a fabricated one."""
    block = Core(unit=unit, trades=0, values=[]).as_dict()
    block["distribution"] = {"count": 0, "note": "no observations"}
    block["durations"] = {"available": False, "why": why}
    block["empty_reason"] = why
    return block


def to_float(value: Decimal | float | int | None) -> float | None:
    """A stored Decimal as a float, for the statistics only.

    Money stays `Decimal` everywhere it is a figure a person acts on. A mean,
    a deviation and a ratio are already inexact, and carrying `Decimal` through
    `sqrt` buys precision the statistic does not have.
    """
    return None if value is None else float(value)


__all__ = [
    "INSUFFICIENT_DATA",
    "MIN_TRADES_FOR_DISTRIBUTION",
    "MIN_TRADES_FOR_RATIO",
    "POOLABLE",
    "Core",
    "Observation",
    "Series",
    "Streaks",
    "Unit",
    "UnitMismatch",
    "core",
    "distribution",
    "downside_deviation",
    "durations",
    "empty",
    "expectancy",
    "gross_loss",
    "gross_profit",
    "loss_rate",
    "net_profit",
    "payoff_ratio",
    "percentile",
    "profit_factor",
    "sharpe_per_trade",
    "sortino_per_trade",
    "standard_deviation",
    "streaks",
    "t_statistic",
    "to_float",
    "win_rate",
]
