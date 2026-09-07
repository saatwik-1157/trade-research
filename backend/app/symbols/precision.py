"""Quantity and price normalization against a broker's contract spec.

**One implementation, used by everyone.** `app.brokers.validation` imports
`floor_to_step` from here rather than keeping its own copy, and position sizing
(L18) and the OMS (L19) will do the same. Two functions that round a lot size
differently is how the same signal sends two different volumes depending on
which path reached the venue.

The governing rule, inherited from `tools/mt5_paper.lot_for_risk`:

    **Never silently change a quantity in a way that materially changes risk.**

So the default is to *refuse* a quantity that is not already valid, and every
adjustment that does happen is reported in the result rather than applied
invisibly. A caller that wants the nearest valid volume asks for it explicitly
and is told what it got.

Direction matters and is never chosen for the caller. Flooring a lot size is
safe because it can only risk less than budgeted; rounding one up risks more.
For a *price* there is no universally safe direction -- rounding a stop-loss
toward the entry tightens risk and away from it widens risk, and which of
those is "safe" depends on what the price means. So `normalize_price`
quantizes and reports, and `snap_stop_loss` / `snap_take_profit` exist for
callers that know the side and want the conservative direction named.

Two arithmetic details this project has already paid for:

  * **Decimal throughout.** A float remainder on a 0.1 step gives
    0.09999999999999998 and rejects a valid order.
  * **Round the step count before flooring.** A budget worth exactly five
    steps arrives from a float ATR as 4.999999999, and a bare floor drops a
    whole step -- so a 5-step order and a 4-step order send identical volume
    with nothing in the record to say which was intended.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, InvalidOperation
from enum import StrEnum

# How close a ratio must be to a whole number before it is treated as one.
# This is the float-floor guard: a step count of 4.999999999 is five steps that
# arrived through a float, not four steps and a bit.
STEP_EPSILON = Decimal("1e-9")


class PrecisionError(Exception):
    """A value that cannot be normalized. Never resolved to a default."""


class Adjustment(StrEnum):
    none = "none"  # already valid; nothing was changed
    floored = "floored"  # rounded down to the nearest valid value
    quantized = "quantized"  # snapped to the instrument's tick grid


@dataclass(frozen=True)
class Normalized:
    """A normalized value and an honest account of what happened to it.

    `adjusted` is never silently true: a caller that ignores it still gets a
    correct value, and a caller that reads it can see the difference between
    "this was already valid" and "this was moved to make it valid".
    """

    value: Decimal
    adjustment: Adjustment = Adjustment.none
    original: Decimal | None = None
    detail: str = ""

    @property
    def adjusted(self) -> bool:
        return self.adjustment is not Adjustment.none

    def as_dict(self) -> dict[str, object]:
        return {
            "value": str(self.value),
            "adjustment": str(self.adjustment),
            "original": str(self.original) if self.original is not None else None,
            "adjusted": self.adjusted,
            "detail": self.detail,
        }


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PrecisionError(f"{name} is not a number: {value!r}") from exc
    if not parsed.is_finite():
        raise PrecisionError(f"{name} is not a finite number: {value!r}")
    return parsed


def steps_in(value: Decimal, step: Decimal) -> Decimal:
    """How many whole steps fit in `value`, guarding the float floor.

    The ratio is rounded to the nearest whole number when it is within
    `STEP_EPSILON` of one, because a count that arrived through a float is a
    whole count that lost its last bits, not a smaller count.
    """
    if step <= 0:
        raise PrecisionError(f"step must be positive, not {step}")
    ratio = value / step
    nearest = ratio.to_integral_value(rounding=ROUND_HALF_EVEN)
    if abs(ratio - nearest) <= STEP_EPSILON:
        return nearest
    return ratio.to_integral_value(rounding=ROUND_DOWN)


def is_step_multiple(value: Decimal, step: Decimal) -> bool:
    """True when `value` sits exactly on the step grid."""
    if step <= 0:
        return True
    return abs(value - (steps_in(value, step) * step)) <= STEP_EPSILON


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """The largest valid value at or below `value`.

    Down, always. Down can only risk less than intended; up silently risks
    more, which is what undoes position sizing.
    """
    if step <= 0:
        return value
    return steps_in(value, step) * step


# --------------------------------------------------------------- quantity


def normalize_quantity(
    quantity: object,
    *,
    minimum: object | None,
    maximum: object | None,
    step: object | None,
    allow_floor: bool = False,
) -> Normalized:
    """Check a quantity against a venue's volume rules.

    `allow_floor=False` (the default) **refuses** a quantity off the step grid
    and names the nearest valid value below it. That is the right default for
    an order arriving from anywhere: silently moving it changes the risk the
    caller budgeted.

    `allow_floor=True` is for position sizing, which computes a raw figure from
    a risk budget and legitimately needs the largest tradable volume at or
    below it. Even then the result records that it floored and from what.

    A quantity below the venue minimum is **always** refused, never raised to
    the minimum. Raising it would risk more than was budgeted, and when the
    minimum lot already exceeds the budget the correct answer is "this trade
    cannot be taken at this size", not "take a bigger one".
    """
    value = _decimal(quantity, "quantity")
    if value <= 0:
        raise PrecisionError(f"quantity must be positive, not {value}")

    if minimum is not None:
        low = _decimal(minimum, "minimum volume")
        if value < low:
            raise PrecisionError(
                f"quantity {value} is below the venue minimum {low}. Refusing rather "
                "than raising it: the minimum lot exceeding the risk budget means the "
                "trade cannot be taken at this size, not that it should be taken larger"
            )
    if maximum is not None:
        high = _decimal(maximum, "maximum volume")
        if value > high:
            raise PrecisionError(f"quantity {value} is above the venue maximum {high}")

    if step is None:
        return Normalized(value)
    grid = _decimal(step, "volume step")
    if grid <= 0:
        raise PrecisionError(f"volume step must be positive, not {grid}")
    if is_step_multiple(value, grid):
        # Re-expressed on the grid so 0.010000 and 0.01 are the same value.
        return Normalized(floor_to_step(value, grid))

    nearest = floor_to_step(value, grid)
    if not allow_floor:
        raise PrecisionError(
            f"quantity {value} is not a multiple of the volume step {grid}; the nearest "
            f"valid quantity at or below it is {nearest}. Refusing rather than rounding, "
            "because rounding up would risk more than was budgeted"
        )
    if nearest <= 0:
        raise PrecisionError(
            f"quantity {value} floors to zero at a step of {grid}; there is no tradable "
            "size at or below it"
        )
    if minimum is not None and nearest < _decimal(minimum, "minimum volume"):
        raise PrecisionError(
            f"quantity {value} floors to {nearest}, which is below the venue minimum "
            f"{minimum}. Reported as a gap rather than silently accepted"
        )
    return Normalized(
        nearest,
        Adjustment.floored,
        original=value,
        detail=f"floored from {value} to the {grid} step grid",
    )


# ------------------------------------------------------------------ price


def normalize_price(
    price: object, *, tick_size: object | None, digits: object | None = None
) -> Normalized:
    """Snap a price to the instrument's tick grid.

    Quantizes to the nearest tick with banker's rounding, which is symmetric
    and therefore does not drift in either direction over many calls. It does
    **not** choose a risk-safe direction, because there is no direction that is
    safe for every price: moving a stop toward the entry tightens risk and away
    from it widens it, and only the caller knows which the price is. Use
    `snap_stop_loss` or `snap_take_profit` when the side is known.
    """
    value = _decimal(price, "price")
    if value <= 0:
        raise PrecisionError(f"price must be positive, not {value}")
    if tick_size is None:
        if digits is None:
            return Normalized(value)
        places = int(_decimal(digits, "digits"))
        snapped = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
        return _price_result(value, snapped, f"quantized to {places} digits")

    tick = _decimal(tick_size, "tick size")
    if tick <= 0:
        raise PrecisionError(f"tick size must be positive, not {tick}")
    ticks = (value / tick).to_integral_value(rounding=ROUND_HALF_EVEN)
    return _price_result(value, ticks * tick, f"snapped to the {tick} tick grid")


def _price_result(original: Decimal, snapped: Decimal, detail: str) -> Normalized:
    if snapped == original:
        return Normalized(snapped)
    return Normalized(snapped, Adjustment.quantized, original=original, detail=detail)


def snap_stop_loss(price: object, *, side: str, tick_size: object | None) -> Normalized:
    """Snap a stop-loss to the tick grid in the direction that tightens risk.

    A long's stop sits below the entry, so rounding it **up** moves it closer
    and risks less. A short's stop sits above, so rounding it **down** does the
    same. Tightening is the safe direction to be wrong in: the trade risks
    slightly less than budgeted rather than slightly more.
    """
    return _snap_directed(price, tick_size, up=side in ("buy", "long"), what="stop_loss")


def snap_take_profit(price: object, *, side: str, tick_size: object | None) -> Normalized:
    """Snap a take-profit to the tick grid in the direction that is harder to reach.

    A long's target is above the entry, so rounding it **up** makes it slightly
    harder to hit; a short's is below, so rounding **down** does. This never
    flatters a result: a target that was snapped closer would book a win the
    instrument did not actually reach.
    """
    return _snap_directed(price, tick_size, up=side in ("buy", "long"), what="take_profit")


def _snap_directed(price: object, tick_size: object | None, *, up: bool, what: str) -> Normalized:
    value = _decimal(price, what)
    if value <= 0:
        raise PrecisionError(f"{what} must be positive, not {value}")
    if tick_size is None:
        return Normalized(value)
    tick = _decimal(tick_size, "tick size")
    if tick <= 0:
        raise PrecisionError(f"tick size must be positive, not {tick}")
    rounding = ROUND_UP if up else ROUND_DOWN
    ticks = (value / tick).to_integral_value(rounding=rounding)
    return _price_result(
        value, ticks * tick, f"snapped {'up' if up else 'down'} to the {tick} tick grid"
    )


# ================================================== money per price movement


class SpecIncomplete(Exception):
    """A contract term needed for money arithmetic is missing. Refused."""


def value_per_price_unit(tick_value: object, tick_size: object) -> Decimal:
    """Account currency per 1.0 of price movement, per 1.0 of volume.

    Moved here at L18 from `app.paper.portfolio`, which now re-exports it.
    It was the paper portfolio's private helper and position sizing had its
    own inline copy of the same arithmetic (`stop / tick_size * tick_value`).
    Two expressions of one conversion is how the money a stop costs comes out
    differently depending on which module asked, so there is now one.

    Both inputs come from the broker's measured contract spec. A missing or
    zero tick size refuses rather than defaulting: a number derived from an
    absent measurement is not a conservative estimate, it is a wrong one that
    looks right.
    """
    if tick_value is None or tick_size is None:
        raise SpecIncomplete(
            "tick_value and tick_size are both required to convert price movement "
            "into account currency; one of them is missing from the contract spec"
        )
    tv, ts = Decimal(str(tick_value)), Decimal(str(tick_size))
    if not tv.is_finite() or not ts.is_finite():
        raise SpecIncomplete(f"tick_value {tv} and tick_size {ts} must both be finite")
    if ts <= 0:
        raise SpecIncomplete(f"tick_size must be positive, not {ts}")
    if tv <= 0:
        raise SpecIncomplete(f"tick_value must be positive, not {tv}")
    return tv / ts
