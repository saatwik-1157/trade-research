"""The label engine: what happened after T.

A label is the one part of a training row that is allowed to read the future,
and it is the reason the whole pipeline has to be careful. `features.py`
computes from bars at or before T; everything here computes from bars strictly
after T, and the two never meet except in `builder.py`, which puts them side by
side in one row and says which is which.

**A label at the tail of a series is `None`, and the row is dropped.** The last
`horizon` bars have no future to look at. Filling them -- with zero, with the
last known value, with anything -- would teach a model that the end of a
dataset is a particular kind of market. Section 8's rule, stated once: labels
are never fabricated.

**Costs are charged, because the project has measured that they decide the
answer.** `CLAUDE.md` records that the only effect in this repository large
enough to reach significance is cost drag: the `random` rule loses at
t = -3.60 because it pays the spread every time, and breakeven at
SL=TP=1.5xATR needs a 50.5-52.7% win rate. A `WIN` label computed without the
spread is a label for a market nobody trades in, so `LabelConfig` has no
zero-cost default -- `spread_points` must be stated.

**The bracket label is path-dependent and the path is walked in order.**
Sections 22 and 57. Given an entry at T's close, a stop and a target, the
outcome is whichever level the *future path* reaches first -- not whichever is
closer, and not whichever the final close is nearer. Where a single bar's range
spans both levels the outcome is `AMBIGUOUS` rather than a guess: bar data
cannot say which came first inside the bar, and the project already has a live
example of a 282-point M1 range that inverted a bracket at the fill.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.marketdata.types import Bar

# Bumped when ANY label's rule changes. A dataset stores the version it used,
# so two datasets built under different rules can never be pooled by accident.
LABEL_SET_VERSION = "1.0"


class LabelError(Exception):
    """A label cannot be produced. Never downgraded to a default value."""


class Outcome(StrEnum):
    """What the future path did to a bracket.

    `TIMEOUT` is a real outcome, not a missing one: the trade was still open
    when the horizon ran out, which is information. `AMBIGUOUS` is the honest
    answer when one bar contains both levels -- bar data cannot order them, and
    a coin flip dressed as a label is worse than a row that is dropped.
    """

    win = "WIN"
    loss = "LOSS"
    timeout = "TIMEOUT"
    ambiguous = "AMBIGUOUS"


class Direction(StrEnum):
    up = "UP"
    down = "DOWN"
    flat = "FLAT"


# No instrument here has a spread anywhere near a tenth of its own price, so
# a value above this is a unit error rather than an expensive market.
IMPLAUSIBLE_SPREAD_PRICE = Decimal("0.1")


@dataclass(frozen=True)
class LabelConfig:
    """Section 21's parameters, all of them explicit.

    `spread_points` has no default, matching `BacktestConfig`: a caller must
    state what trading costs, because the one thing this repository has
    measured to significance is what happens when that is left implicit.
    """

    spread_points: Decimal
    horizon: int = 24
    # A move smaller than this fraction of price is FLAT rather than a
    # direction. Zero would make the label a coin flip on noise.
    flat_threshold: float = 0.0
    stop_atr: float = 1.5
    take_profit_atr: float = 1.5
    atr_period: int = 14

    def __post_init__(self) -> None:
        if self.horizon < 1:
            raise LabelError("horizon must be at least 1 bar")
        if self.spread_points < 0:
            raise LabelError("spread_points cannot be negative")
        # A PLAUSIBILITY FENCE, on swap.py's reasoning, because the field name
        # is misleading and cost this project a whole training round.
        #
        # `spread_points` is subtracted directly from a PRICE in
        # `compute_labels` (`net_exit = closes[end] - spread`), so the value
        # every caller passes is a price -- EURUSD's two points is 0.00002,
        # not 2.0. A caller that reads the name and passes 2.0 gets
        # `1.17 - 2.0 = -0.83`, a forward return of -1.71 on EVERY row, and a
        # dataset where no bar is ever a winner. Measured 2026-09-23: 4,926
        # rows, 0 positive, mean -1.720, and it reached a trained model and an
        # economic report of "profit factor 0.0" before anyone noticed.
        #
        # Nothing traded here has a spread near a tenth of its own price, so
        # the fence is unambiguous and names the likely cause rather than
        # saying the value is invalid.
        if self.spread_points > IMPLAUSIBLE_SPREAD_PRICE:
            raise LabelError(
                f"spread_points={self.spread_points} is larger than "
                f"{IMPLAUSIBLE_SPREAD_PRICE} and is subtracted from a PRICE, so "
                "this is almost certainly points passed where a price is "
                "wanted. EURUSD's two points is 0.00002, not 2.0. Multiply by "
                "the symbol's point size."
            )
        if self.flat_threshold < 0:
            raise LabelError("flat_threshold cannot be negative")
        if self.stop_atr <= 0 or self.take_profit_atr <= 0:
            raise LabelError("the stop and target distances must both be positive")
        if self.atr_period < 2:
            raise LabelError("an ATR period below 2 is not an ATR")

    def as_dict(self) -> dict[str, Any]:
        return {
            "label_set_version": LABEL_SET_VERSION,
            "horizon": self.horizon,
            "flat_threshold": self.flat_threshold,
            "stop_atr": self.stop_atr,
            "take_profit_atr": self.take_profit_atr,
            "atr_period": self.atr_period,
            "spread_points": str(self.spread_points),
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


LABEL_CATALOGUE: dict[str, dict[str, str]] = {
    "forward_return": {
        "description": "Simple return from this bar's close to the close `horizon` bars later, "
        "net of one spread. Dimensionless, so symbols may be pooled.",
        "kind": "regression",
        "uses": "bars strictly after T",
    },
    "direction": {
        "description": "UP / DOWN / FLAT from `forward_return` against `flat_threshold`.",
        "kind": "classification",
        "uses": "bars strictly after T",
    },
    "bracket_outcome": {
        "description": "WIN / LOSS / TIMEOUT / AMBIGUOUS for a long entry at this close with a "
        "stop and target set from the ATR at T. Path-dependent, walked in order.",
        "kind": "classification",
        "uses": "bars strictly after T; the ATR that sizes the bracket is from T",
    },
    "mfe_atr": {
        "description": "Maximum favourable excursion over the horizon, in ATR units.",
        "kind": "regression",
        "uses": "bars strictly after T",
    },
    "mae_atr": {
        "description": "Maximum adverse excursion over the horizon, in ATR units.",
        "kind": "regression",
        "uses": "bars strictly after T",
    },
}

DEFAULT_LABELS: tuple[str, ...] = tuple(LABEL_CATALOGUE)


@dataclass(frozen=True)
class LabelRow:
    """One bar's labels. Every field is None where the horizon ran out."""

    forward_return: float | None = None
    direction: str | None = None
    bracket_outcome: str | None = None
    mfe_atr: float | None = None
    mae_atr: float | None = None
    # Which bar index the bracket resolved on, so a caller can check that the
    # label used no bar beyond the horizon. Purely diagnostic.
    resolved_at_offset: int | None = field(default=None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "forward_return": self.forward_return,
            "direction": self.direction,
            "bracket_outcome": self.bracket_outcome,
            "mfe_atr": self.mfe_atr,
            "mae_atr": self.mae_atr,
        }

    def is_complete(self, names: tuple[str, ...]) -> bool:
        values = self.as_dict()
        return all(values.get(n) is not None for n in names)


def label_catalogue() -> list[dict[str, object]]:
    return [{"name": name, **detail} for name, detail in LABEL_CATALOGUE.items()]


def compute_labels(
    bars: list[Bar],
    config: LabelConfig,
    names: tuple[str, ...] = DEFAULT_LABELS,
) -> list[LabelRow]:
    """One `LabelRow` per bar, same order, `None` past the end of the future.

    Same length as `bars` for the same reason `compute_features` is: alignment
    is by index, and a function that trimmed its own tail would move that
    responsibility to whoever called it.
    """
    for name in names:
        if name not in LABEL_CATALOGUE:
            raise LabelError(
                f"unknown label {name!r}; the catalogue holds {', '.join(sorted(LABEL_CATALOGUE))}"
            )

    n = len(bars)
    if n == 0:
        return []

    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    spread = float(config.spread_points)

    atr = _atr_series(bars, config.atr_period)

    rows: list[LabelRow] = []
    for i in range(n):
        end = i + config.horizon
        if end >= n:
            # No future. Every label is absent, and the builder drops the row.
            rows.append(LabelRow())
            continue

        entry = closes[i]
        # One spread, charged once, on the way in. Charging it on both sides
        # would double-count a round trip the label does not model.
        net_exit = closes[end] - spread
        forward = (net_exit / entry - 1.0) if entry > 0 else None
        forward = _finite(forward)

        direction: str | None = None
        if forward is not None:
            if abs(forward) <= config.flat_threshold:
                direction = str(Direction.flat)
            else:
                direction = str(Direction.up if forward > 0 else Direction.down)

        distance = atr[i]
        outcome: str | None = None
        offset: int | None = None
        mfe: float | None = None
        mae: float | None = None
        if distance is not None and distance > 0:
            stop = entry - config.stop_atr * distance
            target = entry + config.take_profit_atr * distance
            outcome, offset = _walk(highs, lows, i + 1, end, stop=stop, target=target)
            mfe = _finite((max(highs[i + 1 : end + 1]) - entry) / distance)
            mae = _finite((entry - min(lows[i + 1 : end + 1])) / distance)

        rows.append(
            LabelRow(
                forward_return=forward,
                direction=direction,
                bracket_outcome=outcome,
                mfe_atr=mfe,
                mae_atr=mae,
                resolved_at_offset=offset,
            )
        )

    return rows


def _walk(
    highs: list[float],
    lows: list[float],
    start: int,
    end: int,
    *,
    stop: float,
    target: float,
) -> tuple[str, int | None]:
    """Which level the path reaches first, bar by bar, in order.

    A bar whose range contains both is `AMBIGUOUS`: the bar says both prices
    traded and says nothing about the order, and picking one would be inventing
    the half of the record that is missing. Position 10200315596 in the live
    log is what that looks like when it is guessed instead -- a 282-point M1
    range that put both exits on the wrong side of the fill.
    """
    for j in range(start, end + 1):
        hit_target = highs[j] >= target
        hit_stop = lows[j] <= stop
        if hit_target and hit_stop:
            return str(Outcome.ambiguous), j - start + 1
        if hit_target:
            return str(Outcome.win), j - start + 1
        if hit_stop:
            return str(Outcome.loss), j - start + 1
    return str(Outcome.timeout), None


def _atr_series(bars: list[Bar], period: int) -> list[float | None]:
    """ATR at each bar, from the existing catalogue rather than a second copy."""
    from app.strategies.indicators import compute

    return compute(
        "ATR",
        {"period": period},
        [b.open for b in bars],
        [b.high for b in bars],
        [b.low for b in bars],
        [b.close for b in bars],
    )


def _finite(value: float | None) -> float | None:
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return value


def class_balance(rows: list[LabelRow], name: str) -> dict[str, Any]:
    """The distribution of a classification label, reported and never corrected.

    Section 39: document the balance first. Nothing here oversamples or
    undersamples, because rebalancing a validation or test split leaks, and
    rebalancing a training split is a decision for whoever trains a model —
    with the distribution in front of them.
    """
    values = [getattr(row, name) for row in rows]
    present = [v for v in values if v is not None]
    counts: dict[str, int] = {}
    for value in present:
        counts[str(value)] = counts.get(str(value), 0) + 1
    total = len(present)
    return {
        "label": name,
        "labelled": total,
        "unlabelled": len(values) - total,
        "counts": counts,
        "shares": {k: round(v / total, 6) for k, v in counts.items()} if total else {},
        "note": "reported, not corrected; balancing is a training-split decision",
    }


def label_set_manifest(config: LabelConfig, names: tuple[str, ...]) -> dict[str, Any]:
    return {
        **config.as_dict(),
        "labels": [{"name": n, **LABEL_CATALOGUE[n]} for n in names],
        "fingerprint": config.fingerprint(),
        "tail_policy": (
            f"the last {config.horizon} bars of every series have no future and are "
            "dropped, never filled"
        ),
    }
