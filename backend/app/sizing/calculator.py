"""The Position Sizing Engine.

It answers exactly one question:

    *Given the account, the entry, the stop, the risk budget and the broker's
    contract terms, what is the largest quantity that keeps the loss at the
    stop inside the budget?*

It does **not** answer "is this trade allowed?". That is `app.risk`, which
runs after this module and is the only thing that can produce an `Approval`.
Sizing proposes a number; risk vetoes or lets it through. Nothing here
submits, executes, or talks to a broker, and a test asserts this package
imports neither `app.brokers` nor `app.paper`.

**Determinism.** The same request produces the same result, always. There is
no clock, no randomness, no I/O and no model in this module. A quantity that
came from an LLM is an opinion; a quantity has to be a measurement.

Four rules carry the module, and each one is here because the alternative
costs real money.

**1. A missing measurement refuses.** Inherited from
`tools/mt5_paper.lot_for_risk`: a lot sized from an absent tick value is a
real order for the wrong amount. Every refusal names what was missing.

**2. Rounding goes DOWN, never up.** Flooring to the volume step can only risk
less than budgeted. Rounding up risks more, which is the one thing sizing
exists to prevent.

**3. A quantity below the broker's minimum is REFUSED, not raised to it.**
This is the safety fix L18 made to the pre-existing module, which used to
raise the volume to the minimum and record the overshoot in a `gap` string
that no caller had to read. When the minimum lot risks more than the budget,
the correct answer is "this trade cannot be taken at this size", not "take a
bigger one". `app.symbols.precision.normalize_quantity` already refused on
exactly this reasoning; sizing disagreeing with it was the duplication.

**4. Actual risk is recomputed after rounding and checked against the
budget.** Both figures are returned. A result whose `risk_actual` exceeds
`risk_requested` is refused rather than reported.

Sizing does not improve expectancy and cannot. Volume is a positive multiplier
on the per-trade result: it scales +0.021R and -1.00R by the same factor. The
reason to size is that the record becomes comparable across symbols, which is
what makes the R-multiple a measured quantity.

---

**The modes.** The brief names seven; this platform has three, because the
other four are the same three under different names and a second name for one
calculation is a second calculation waiting to diverge.

  * `fixed_quantity` -- an explicit volume. This *is* "fixed lot": on every
    instrument this platform trades, MT5 included, the quantity IS the lot.
  * `fixed_risk` -- a sum of account currency ("monetary risk").
  * `percent_equity` -- a fraction of measured equity, which becomes a sum of
    account currency and then behaves exactly like `fixed_risk`.

Both risk modes are *stop-distance* sizing: neither will size without a stop,
because without one there is no denominator. ATR sizing is not a fourth mode
-- it is this arithmetic over a stop that a caller derived from ATR, which is
what `PaperEngine._bracket` and the backtester already do. Adding an `atr`
mode would put the bracket calculation in two places.

Broker-constrained sizing is not a mode either. It is applied to *every*
result, because a volume the venue will not accept is not a size.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import StrEnum

from app.symbols.precision import (
    STEP_EPSILON,
    Adjustment,
    PrecisionError,
    SpecIncomplete,
    floor_to_step,
    normalize_quantity,
    value_per_price_unit,
)
from app.symbols.service import ContractSpec

# Money is reported to four decimal places. Not a rounding of the decision --
# the comparison against the budget happens on the full-precision figure.
MONEY = Decimal("0.0001")


class SizingMethod(StrEnum):
    fixed_quantity = "fixed_quantity"
    fixed_risk = "fixed_risk"  # a fixed sum of account currency
    percent_equity = "percent_equity"


#: Names the brief uses for modes this platform expresses with the three above.
#: Accepted at the edges (API, configuration) and resolved to the real method,
#: so a caller can say `fixed_lot` without the engine growing a fourth branch.
METHOD_ALIASES: dict[str, SizingMethod] = {
    "fixed_lot": SizingMethod.fixed_quantity,
    "monetary_risk": SizingMethod.fixed_risk,
    "stop_loss_risk": SizingMethod.fixed_risk,
    "equity_percent": SizingMethod.percent_equity,
    "percentage_of_equity": SizingMethod.percent_equity,
}

#: The two methods that size from a risk budget and a stop distance.
RISK_METHODS = frozenset({SizingMethod.fixed_risk, SizingMethod.percent_equity})


def resolve_method(name: str) -> SizingMethod:
    """A method name, including the brief's aliases. Unknown names refuse."""
    key = str(name).strip().lower()
    if key in METHOD_ALIASES:
        return METHOD_ALIASES[key]
    try:
        return SizingMethod(key)
    except ValueError as exc:
        known = ", ".join(sorted({*(m.value for m in SizingMethod), *METHOD_ALIASES}))
        raise SizingError(f"unknown sizing method {name!r}; known methods are {known}") from exc


class SizingError(Exception):
    """A request that cannot be interpreted at all. Never resolved to a default."""


class SizingStatus(StrEnum):
    valid = "VALID"
    refused = "REFUSED"


# ---------------------------------------------------------------- direction

#: The two directions, and every spelling the platform uses for them. `buy`
#: and `sell` are the order sides; `long` and `short` are the position sides.
_LONG = frozenset({"buy", "long"})
_SHORT = frozenset({"sell", "short"})


def _direction(side: str) -> str:
    key = str(side).strip().lower()
    if key in _LONG:
        return "long"
    if key in _SHORT:
        return "short"
    raise SizingError(f"unknown side {side!r}; expected one of buy, sell, long, short")


def _finite(value: Decimal | None, name: str) -> Decimal | None:
    """NaN and infinity are refused wherever a number is accepted.

    `Decimal("NaN")` compares False against every bound, so an unchecked NaN
    walks through `<= 0`, through the limit checks, and out the other side as
    a volume. It is refused at the door instead.
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001
            raise SizingError(f"{name} is not a number: {value!r}") from exc
    if not value.is_finite():
        raise SizingError(f"{name} must be a finite number, not {value}")
    return value


@dataclass(frozen=True)
class SizingRequest:
    """What sizing needs. Everything optional is optional for a stated reason.

    `stop_distance` may be given directly (the paper engine computes it from
    its own bracket) or derived from `entry_price` and `stop_loss`. When entry
    and stop are both present the direction is validated: a long stop above
    its entry is a mistake, not a wide stop, and it is refused rather than
    corrected -- silently flipping it would size a trade nobody described.
    """

    method: SizingMethod
    spec: ContractSpec
    # fixed_quantity
    quantity: Decimal | None = None
    # fixed_risk
    risk_amount: Decimal | None = None
    # percent_equity
    equity: Decimal | None = None
    risk_percent: Decimal | None = None
    # Distance from entry to stop, in price units. Required for both risk
    # methods: without it there is no denominator and no sizing.
    stop_distance: Decimal | None = None
    # Optional context. When side/entry/stop are supplied the engine validates
    # stop placement and derives the distance itself.
    side: str | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    # A hard ceiling on the money this trade may risk, independent of the
    # budget. Used by the API so an untrusted caller cannot request a size
    # larger than the account's configured per-trade risk.
    max_risk_amount: Decimal | None = None


@dataclass(frozen=True)
class SizingResult:
    """A volume, or a refusal. Never both, never a fallback."""

    volume: Decimal | None
    method: SizingMethod
    reason: str
    risk_requested: Decimal | None = None
    risk_actual: Decimal | None = None
    risk_per_unit: Decimal | None = None
    gap: str | None = None
    # L18 additions. Every one is reported on every result, so "not computed"
    # and "computed as zero" are never the same value on screen.
    raw_volume: Decimal | None = None
    stop_distance: Decimal | None = None
    side: str | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    symbol: str | None = None
    warnings: tuple[str, ...] = ()
    constraints: dict[str, str] | None = None

    @property
    def ok(self) -> bool:
        return self.volume is not None

    @property
    def refused(self) -> bool:
        return self.volume is None

    @property
    def status(self) -> SizingStatus:
        return SizingStatus.valid if self.ok else SizingStatus.refused

    #: The brief's name for `volume`. One field, two names, no second value.
    @property
    def final_quantity(self) -> Decimal | None:
        return self.volume

    def as_dict(self) -> dict[str, object]:
        def s(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "status": str(self.status),
            "sizing_mode": str(self.method),
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": s(self.entry_price),
            "stop_loss": s(self.stop_loss),
            "stop_distance": s(self.stop_distance),
            "risk_amount": s(self.risk_requested),
            "risk_per_unit": s(self.risk_per_unit),
            "raw_quantity": s(self.raw_volume),
            "final_quantity": s(self.volume),
            "actual_risk": s(self.risk_actual),
            "reason": self.reason,
            "gap": self.gap,
            "warnings": list(self.warnings),
            "broker_constraints": self.constraints,
        }


def _constraints(spec: ContractSpec) -> dict[str, str]:
    """What the venue allows, reported on every result including refusals.

    A refusal that does not say what the limits were leaves the operator to
    guess whether the budget was too small or the instrument too coarse.
    """
    return {
        "broker_symbol": spec.broker_symbol,
        "provider": spec.provider,
        "minimum_volume": str(spec.minimum_volume),
        "maximum_volume": str(spec.maximum_volume),
        "volume_step": str(spec.volume_step),
        "contract_size": str(spec.contract_size),
        "tick_size": str(spec.tick_size),
        "tick_value": str(spec.tick_value),
        "volume_precision": str(spec.volume_precision),
        "price_precision": str(spec.price_precision),
    }


def _refuse(request: SizingRequest, gap: str, **extra: object) -> SizingResult:
    return SizingResult(
        volume=None,
        method=request.method,
        reason="refused",
        gap=gap,
        symbol=request.spec.internal_symbol,
        side=_side_or_none(request.side),
        entry_price=request.entry_price,
        stop_loss=request.stop_loss,
        constraints=_constraints(request.spec),
        **extra,  # type: ignore[arg-type]
    )


def _side_or_none(side: str | None) -> str | None:
    if side is None:
        return None
    try:
        return _direction(side)
    except SizingError:
        return str(side)


# ================================================================= the engine


def calculate(request: SizingRequest) -> SizingResult:
    """Size one order. Deterministic, side-effect free, and never raises.

    Every failure comes back as a refusal carrying its reason, because a
    caller in the middle of a trading pass needs a decision it can record, not
    an exception it has to classify. `SizingError` is caught here for the same
    reason: a malformed request is a refusal, not a crash in a bot loop.
    """
    try:
        return _calculate(request)
    except (SizingError, SpecIncomplete, PrecisionError) as exc:
        return _refuse(request, str(exc))


def _calculate(request: SizingRequest) -> SizingResult:
    spec = request.spec
    method = request.method

    # Every number that arrives is checked for finiteness first. A NaN
    # compares False against every bound and would otherwise walk straight
    # through the limit checks and out as a volume.
    quantity = _finite(request.quantity, "quantity")
    risk_amount = _finite(request.risk_amount, "risk_amount")
    equity = _finite(request.equity, "equity")
    risk_percent = _finite(request.risk_percent, "risk_percent")
    entry_price = _finite(request.entry_price, "entry_price")
    stop_loss = _finite(request.stop_loss, "stop_loss")
    max_risk = _finite(request.max_risk_amount, "max_risk_amount")
    stop_distance = _finite(request.stop_distance, "stop_distance")

    side = _direction(request.side) if request.side is not None else None

    # ------------------------------------------------------ fixed quantity
    if method is SizingMethod.fixed_quantity:
        if quantity is None or quantity <= 0:
            return _refuse(request, "fixed_quantity needs a positive quantity")
        # Direction is still validated when the caller supplied a bracket: a
        # long stop above its entry is wrong whether or not the size came
        # from it, and passing it on would hand the OMS a broken order.
        derived = _stop_distance(side, entry_price, stop_loss)
        distance = stop_distance if stop_distance is not None else derived
        return _apply_broker_limits(
            request, quantity, budget=None, risk_per_unit=None, side=side, distance=distance
        )

    # ------------------------------------------------------- risk methods
    #
    # Both are stop-distance sizing. The stop may arrive as a distance or as
    # an entry/stop pair; the pair is preferred because it can be validated.
    derived = _stop_distance(side, entry_price, stop_loss)
    if derived is not None and stop_distance is not None and derived != stop_distance:
        return _refuse(
            request,
            f"stop_distance {stop_distance} disagrees with the {derived} implied by entry "
            f"{entry_price} and stop {stop_loss}; refusing rather than choosing one",
        )
    distance = derived if derived is not None else stop_distance

    if distance is None or distance <= 0:
        return _refuse(request, "stop distance is required and must be positive")

    if method is SizingMethod.fixed_risk:
        if risk_amount is None or risk_amount <= 0:
            return _refuse(request, "fixed_risk needs a positive risk_amount")
        budget = risk_amount
    else:
        if equity is None or equity <= 0:
            return _refuse(request, "percent_equity needs positive equity")
        if risk_percent is None or risk_percent <= 0:
            return _refuse(request, "percent_equity needs a positive risk_percent")
        if risk_percent > 100:
            return _refuse(request, f"risk_percent {risk_percent} exceeds 100")
        budget = (equity * risk_percent / Decimal(100)).quantize(MONEY, rounding=ROUND_DOWN)
        if budget <= 0:
            return _refuse(request, "computed risk budget rounds to zero")

    # A ceiling the caller states separately from the budget. Applied before
    # sizing, so an untrusted request cannot ask for more than it may have.
    if max_risk is not None:
        if max_risk <= 0:
            return _refuse(request, f"max_risk_amount must be positive, not {max_risk}")
        if budget > max_risk:
            return _refuse(
                request,
                f"risk budget {budget} exceeds the permitted maximum {max_risk}; refusing "
                "rather than sizing to the smaller figure, because the caller asked for a "
                "trade this account may not take",
            )

    # Loss in account currency if one unit of volume runs to its stop. One
    # conversion, shared with the paper portfolio -- `value_per_price_unit`
    # refuses a missing or non-positive tick figure rather than defaulting.
    risk_per_unit = distance * value_per_price_unit(spec.tick_value, spec.tick_size)
    if risk_per_unit <= 0:
        return _refuse(request, "computed risk per unit is not positive")

    raw = budget / risk_per_unit
    return _apply_broker_limits(
        request, raw, budget=budget, risk_per_unit=risk_per_unit, side=side, distance=distance
    )


def _stop_distance(side: str | None, entry: Decimal | None, stop: Decimal | None) -> Decimal | None:
    """The distance from entry to stop, refusing a stop on the wrong side.

    LONG wants the stop BELOW the entry, SHORT wants it ABOVE. A stop on the
    wrong side is not a wide stop -- it is a target, and sizing a position
    from it produces a volume for a trade that was never described. It is
    refused rather than silently corrected, because both corrections (flip the
    stop, flip the side) change what the caller asked for.
    """
    if entry is None or stop is None:
        return None
    if entry <= 0:
        raise SizingError(f"entry_price must be positive, not {entry}")
    if stop <= 0:
        raise SizingError(f"stop_loss must be positive, not {stop}")
    if entry == stop:
        raise SizingError(
            f"entry_price and stop_loss are both {entry}; the stop distance is zero and "
            "there is no denominator to size with"
        )
    if side is None:
        # No side to check against. The distance is still well defined.
        return abs(entry - stop)
    if side == "long" and stop > entry:
        raise SizingError(
            f"a long stop must sit below its entry; got entry {entry} and stop {stop}. "
            "Refused rather than corrected: that stop is a target"
        )
    if side == "short" and stop < entry:
        raise SizingError(
            f"a short stop must sit above its entry; got entry {entry} and stop {stop}. "
            "Refused rather than corrected: that stop is a target"
        )
    return abs(entry - stop)


def _apply_broker_limits(
    request: SizingRequest,
    raw: Decimal,
    *,
    budget: Decimal | None,
    risk_per_unit: Decimal | None,
    side: str | None,
    distance: Decimal | None,
) -> SizingResult:
    """Snap to the venue's step and bounds, then re-measure the risk.

    The maximum binds downward, which can only reduce risk, so it is applied
    and reported as a warning. The minimum binds upward, which can only
    *increase* risk, so it refuses. That asymmetry is the whole of rule 3.
    """
    spec = request.spec
    warnings: list[str] = []

    if spec.volume_step <= 0:
        return _refuse(request, f"volume step must be positive, not {spec.volume_step}")
    if spec.minimum_volume <= 0 or spec.maximum_volume <= 0:
        return _refuse(
            request,
            f"{spec.internal_symbol} reports minimum volume {spec.minimum_volume} and "
            f"maximum {spec.maximum_volume}; a size cannot be validated against these",
        )
    if spec.minimum_volume > spec.maximum_volume:
        return _refuse(
            request,
            f"{spec.internal_symbol} reports a minimum volume {spec.minimum_volume} above "
            f"its maximum {spec.maximum_volume}; the contract spec is not usable",
        )

    volume = raw
    if volume > spec.maximum_volume:
        warnings.append(
            f"requested {raw} exceeds the venue maximum {spec.maximum_volume}; capped. "
            "The cap reduces risk, so it is applied rather than refused"
        )
        volume = spec.maximum_volume

    # One flooring implementation, shared with the OMS and the broker layer.
    try:
        normalized = normalize_quantity(
            volume,
            minimum=spec.minimum_volume,
            maximum=spec.maximum_volume,
            step=spec.volume_step,
            allow_floor=True,
        )
    except PrecisionError as exc:
        # This is the refusal that used to be a silent upsize. It fires when
        # the budget cannot buy one minimum lot, and the message says so.
        gap = str(exc)
        if budget is not None and risk_per_unit is not None:
            would_risk = (spec.minimum_volume * risk_per_unit).quantize(MONEY)
            gap = (
                f"{gap}. The minimum volume {spec.minimum_volume} on {spec.broker_symbol} "
                f"would risk {would_risk} against a {budget} budget: the volume step is "
                "the binding constraint, not the risk setting"
            )
        return _refuse(
            request,
            gap,
            raw_volume=floor_to_step(raw, spec.volume_step),
            stop_distance=distance,
            risk_per_unit=risk_per_unit.quantize(MONEY) if risk_per_unit else None,
            risk_requested=budget,
        )

    volume = normalized.value
    if normalized.adjustment is Adjustment.floored:
        warnings.append(normalized.detail)

    # Expressed at the venue's OWN volume precision. Flooring already put the
    # value on the step grid, so this changes the representation and never the
    # quantity -- but without it the same 0.20 lots renders as "0.20" or
    # "0.2000000" depending on the scale the spec happened to be stored at,
    # and a volume that reads differently in two places is a volume somebody
    # will eventually reconcile by hand.
    try:
        volume = volume.quantize(Decimal(1).scaleb(-int(spec.volume_precision)))
    except (InvalidOperation, ValueError, TypeError):  # pragma: no cover
        pass

    if volume <= 0:  # pragma: no cover - normalize_quantity refuses first
        return _refuse(request, f"volume rounds to {volume} at step {spec.volume_step}")

    # Full precision for the comparison, four places for the report. Comparing
    # the ROUNDED figure would let a budget fail on the fifth decimal of a
    # currency nobody quotes that finely.
    exact = volume * risk_per_unit if risk_per_unit is not None else None
    actual = exact.quantize(MONEY) if exact is not None else None

    # Rule 4. Flooring can only reduce the risk, so this cannot fire through
    # arithmetic -- it fires if a future change breaks that property, and it
    # refuses rather than reporting, because "the size we sent risked more
    # than you budgeted" is not something to learn from a warning field.
    #
    # The tolerance is not a fudge factor. `steps_in` treats a count within
    # STEP_EPSILON of a whole number AS that whole number, which is the guard
    # against the float floor this repository already paid for -- a budget
    # worth exactly five steps arriving as 4.999999999 and being cut to four.
    # That guard can round the count UP by at most STEP_EPSILON, so the volume
    # can exceed the budget by at most STEP_EPSILON of one step, and the money
    # by that times the risk per unit. Anything above that is a real overshoot
    # and refuses. Without this, the float-floor defect returns wearing a
    # refusal: a valid 5-step order rejected over 1e-10 of currency.
    if exact is not None and budget is not None:
        tolerance = STEP_EPSILON * spec.volume_step * risk_per_unit  # type: ignore[operator]
        if exact > budget + tolerance:
            return _refuse(
                request,
                f"the rounded volume {volume} risks {actual} against a {budget} budget; "
                "refusing rather than exceeding the configured risk",
                raw_volume=raw,
                stop_distance=distance,
                risk_per_unit=risk_per_unit.quantize(MONEY) if risk_per_unit else None,
                risk_requested=budget,
            )

    if (
        request.max_risk_amount is not None
        and actual is not None
        and actual > request.max_risk_amount
    ):
        return _refuse(
            request,
            f"the rounded volume {volume} risks {actual}, above the permitted maximum "
            f"{request.max_risk_amount}",
            raw_volume=raw,
            stop_distance=distance,
            risk_requested=budget,
        )

    return SizingResult(
        volume=volume,
        method=request.method,
        reason=f"sized to {volume} at step {spec.volume_step}",
        risk_requested=budget,
        risk_actual=actual,
        risk_per_unit=risk_per_unit.quantize(MONEY) if risk_per_unit is not None else None,
        gap=None,
        raw_volume=raw,
        stop_distance=distance,
        side=side,
        entry_price=request.entry_price,
        stop_loss=request.stop_loss,
        symbol=spec.internal_symbol,
        warnings=tuple(warnings),
        constraints=_constraints(spec),
    )
