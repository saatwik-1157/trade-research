"""Risk configuration: the hierarchy, the combination rule, and validation.

Four scopes can each carry limits, and the platform's rule is **safety-first
precedence**: the effective limit is the most restrictive applicable one, never
the most specific one.

    GLOBAL -> ACCOUNT -> STRATEGY -> SYMBOL

The distinction matters and is the reason this is not a dictionary merge. Under
a merge, a strategy that sets `max_risk_per_trade = 3%` would *override* an
account limit of 1%, because it is more specific. Under this rule it cannot:
1% wins, because 1% is smaller. A more specific scope can tighten a limit and
can never loosen one.

Combination is declarative, per field, in `COMBINE`:

  * a cap (`max_*`) combines by **minimum** -- the smallest cap binds;
  * a floor (`min_*`) combines by **maximum** -- the largest floor binds;
  * a restricting boolean combines by **OR** -- any layer that requires a stop
    loss makes it required for everyone.

`None` means "this layer does not speak to this limit" and never constrains.
A configuration where every layer is silent leaves the limit unenforced, which
is reported on every decision rather than read as a pass.

**Validation refuses rather than normalising.** A percentage above 100, a
negative cap, a zero where zero would disable a safety rule -- each is an
error, because silently clamping an unsafe configuration produces a system
trading under limits nobody chose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.risk.engine import RiskLimits


class Scope(StrEnum):
    """Ordered least to most specific. The order is documentation, not
    precedence: precedence is by restrictiveness, not by position."""

    global_ = "global"
    broker_account = "broker_account"
    paper_account = "paper_account"
    strategy = "strategy"
    symbol = "symbol"


SCOPE_ORDER = (
    Scope.global_,
    Scope.broker_account,
    Scope.paper_account,
    Scope.strategy,
    Scope.symbol,
)


class ConfigurationInvalid(Exception):
    """A configuration that would leave the system trading under unclear
    limits. Refused rather than clamped."""


def _min(a: Any, b: Any) -> Any:
    return b if a is None else a if b is None else min(a, b)


def _max(a: Any, b: Any) -> Any:
    return b if a is None else a if b is None else max(a, b)


def _or(a: Any, b: Any) -> Any:
    return bool(a) or bool(b)


# How each limit combines across scopes. Every field of RiskLimits must appear
# here; a test asserts the map is total, so a limit added later cannot silently
# pick a combination rule by accident.
COMBINE: dict[str, Callable[[Any, Any], Any]] = {
    # Caps: the smallest binds.
    "max_risk_per_trade": _min,
    "max_daily_loss": _min,
    "max_drawdown_pct": _min,
    "max_exposure_per_currency": _min,
    "max_open_positions": _min,
    "max_trades_per_day": _min,
    "max_trades_per_hour": _min,
    "max_trades_per_minute": _min,
    "max_leverage": _min,
    "max_position_size": _min,
    "max_concentration_pct": _min,
    "max_margin_utilisation_pct": _min,
    "max_spread_points": _min,
    "max_signal_age_seconds": _min,
    "market_data_max_age_seconds": _min,
    # Floors: the largest binds. A higher minimum reward is more restrictive.
    "min_risk_reward": _max,
    "cooldown_seconds": _max,
    # Restricting booleans: any layer that switches it on binds everyone.
    "one_position_per_symbol": _or,
    "require_stop_loss": _or,
    "require_market_open": _or,
}


# Limits that must be positive when set. Zero would not be a strict limit, it
# would disable trading entirely or disable the check -- neither is what a
# caller writing 0 means, so it is refused and they say which they meant.
POSITIVE_ONLY = frozenset(
    {
        "max_risk_per_trade",
        "max_daily_loss",
        "max_drawdown_pct",
        "max_exposure_per_currency",
        "max_leverage",
        "max_position_size",
        "max_concentration_pct",
        "max_margin_utilisation_pct",
        "max_spread_points",
        "max_signal_age_seconds",
        "market_data_max_age_seconds",
        "min_risk_reward",
    }
)

# Limits expressed as a percentage. Above 100 is refused: a 150% drawdown limit
# is not a lenient limit, it is a misunderstanding.
PERCENTAGES = frozenset({"max_drawdown_pct", "max_concentration_pct", "max_margin_utilisation_pct"})

NON_NEGATIVE_INTS = frozenset(
    {
        "max_open_positions",
        "max_trades_per_day",
        "max_trades_per_hour",
        "max_trades_per_minute",
        "cooldown_seconds",
    }
)


def validate(limits: RiskLimits) -> list[str]:
    """Every problem with a configuration, not just the first one.

    Returned rather than raised so an admin editing several fields sees all of
    their mistakes at once instead of one per round trip.
    """
    problems: list[str] = []
    for field in fields(limits):
        value = getattr(limits, field.name)
        if value is None or isinstance(value, bool):
            continue
        if field.name in POSITIVE_ONLY and value <= 0:
            problems.append(
                f"{field.name} must be positive when set, not {value}; zero or negative "
                "would disable the check rather than tighten it"
            )
        if field.name in PERCENTAGES and value > 100:
            problems.append(f"{field.name} is a percentage and cannot exceed 100, got {value}")
        if field.name in NON_NEGATIVE_INTS and value < 0:
            problems.append(f"{field.name} cannot be negative, got {value}")
    # Cross-field coherence.
    if limits.max_trades_per_minute is not None and limits.max_trades_per_hour is not None:
        if limits.max_trades_per_minute > limits.max_trades_per_hour:
            problems.append(
                f"max_trades_per_minute ({limits.max_trades_per_minute}) exceeds "
                f"max_trades_per_hour ({limits.max_trades_per_hour}); the tighter "
                "window cannot allow more than the wider one"
            )
    if limits.max_trades_per_hour is not None and limits.max_trades_per_day is not None:
        if limits.max_trades_per_hour > limits.max_trades_per_day:
            problems.append(
                f"max_trades_per_hour ({limits.max_trades_per_hour}) exceeds "
                f"max_trades_per_day ({limits.max_trades_per_day})"
            )
    return problems


def check_valid(limits: RiskLimits) -> RiskLimits:
    problems = validate(limits)
    if problems:
        raise ConfigurationInvalid("; ".join(problems))
    return limits


@dataclass(frozen=True)
class Layer:
    """One scope's contribution. `params` names only the limits it speaks to."""

    scope: Scope
    scope_ref: str | None
    params: dict[str, Any]
    version: int = 1
    name: str = ""


@dataclass(frozen=True)
class Resolved:
    """The effective limits, and an account of where each one came from."""

    limits: RiskLimits
    version: int
    sources: dict[str, str]
    layers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "layers": list(self.layers),
            "limits": {
                f.name: (
                    str(getattr(self.limits, f.name))
                    if getattr(self.limits, f.name) is not None
                    else None
                )
                for f in fields(self.limits)
            },
            "sources": self.sources,
            "precedence": (
                "the most restrictive applicable limit wins, never the most specific. "
                "A strategy can tighten an account limit and can never loosen one."
            ),
        }


# Which limits are money/ratio quantities. JSON has no Decimal, so a stored
# configuration round-trips through strings -- and it must come BACK as a
# Decimal, or a limit compared against a Decimal proposal raises or, worse,
# compares a float against money.
DECIMAL_FIELDS = frozenset(
    {
        "max_risk_per_trade",
        "max_daily_loss",
        "max_drawdown_pct",
        "max_exposure_per_currency",
        "max_leverage",
        "max_position_size",
        "max_concentration_pct",
        "max_margin_utilisation_pct",
        "max_spread_points",
        "min_risk_reward",
    }
)


def to_json(params: dict[str, Any]) -> dict[str, Any]:
    """Render a layer for storage. Decimals become strings, exactly."""
    return {
        key: (format(value, "f") if isinstance(value, Decimal) else value)
        for key, value in params.items()
    }


def from_json(params: dict[str, Any]) -> dict[str, Any]:
    """Read a stored layer back, restoring the Decimals.

    A value that will not parse is an error rather than a skip: a limit that
    silently disappeared because its stored form was malformed would leave the
    system trading unprotected while the configuration still claimed to set it.
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in DECIMAL_FIELDS and value is not None and not isinstance(value, Decimal):
            try:
                out[key] = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise ConfigurationInvalid(
                    f"stored risk limit {key}={value!r} is not a number"
                ) from exc
        else:
            out[key] = value
    return out


def resolve(layers: list[Layer]) -> Resolved:
    """Combine the layers into the effective configuration.

    Order of application does not affect the result -- min, max and OR are all
    associative and commutative -- which is why "the most restrictive wins" is a
    property of this function rather than of the order somebody passed things
    in. A test shuffles the layers and asserts the same answer.
    """
    known = {f.name for f in fields(RiskLimits)}
    unknown: set[str] = set()
    effective: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for layer in layers:
        for key, value in from_json(layer.params).items():
            if key not in known:
                unknown.add(key)
                continue
            if value is None:
                continue
            combiner = COMBINE.get(key)
            if combiner is None:
                unknown.add(key)
                continue
            current = effective.get(key)
            combined = combiner(current, value)
            effective[key] = combined
            # Attribute the limit to the layer that actually set the binding
            # value, so "why can I not trade" has an answer with a name on it.
            if current is None or combined != current:
                sources[key] = f"{layer.scope}:{layer.scope_ref or '*'}"

    if unknown:
        raise ConfigurationInvalid(
            f"unknown risk limits: {', '.join(sorted(unknown))}. Refusing rather than "
            "ignoring them, because an ignored limit reads as an enforced one"
        )

    # Booleans no layer set keep the dataclass default rather than becoming
    # False: `require_stop_loss` defaults to True on purpose.
    limits = replace(RiskLimits(), **effective) if effective else RiskLimits()
    check_valid(limits)
    version = sum(layer.version for layer in layers) or 1
    return Resolved(
        limits=limits,
        version=version,
        sources=sources,
        layers=tuple(
            f"{layer.scope}:{layer.scope_ref or '*'}@v{layer.version}" for layer in layers
        ),
    )


def describe_precedence() -> dict[str, object]:
    return {
        "scopes": [str(s) for s in SCOPE_ORDER],
        "rule": (
            "The effective limit is the most restrictive applicable one, not the most "
            "specific. A cap combines by minimum, a floor by maximum, a restricting "
            "boolean by OR."
        ),
        "example": (
            "global max_risk_per_trade 2%, account 1%, strategy requests 3% -> "
            "effective 1%. The strategy cannot loosen the account's limit."
        ),
        "unset": (
            "A limit no layer sets is NOT enforced, and every decision reports it as "
            "not enforced rather than as passed."
        ),
    }
