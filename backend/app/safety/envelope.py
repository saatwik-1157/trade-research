"""The bounds an autonomous action must sit inside. **L62 section 5.**

L61 classified actions by DIRECTION -- does this take permission away or grant
it -- and that is the property that decides whether an action may ever be
automatic. It says nothing about size. "Reduce the allocation" is classified
identically whether it reduces by 2% or by 100%, and one of those is a
reduction while the other is a liquidation nobody sized.

This is the missing half: the magnitude, the frequency and the freshness. It
does not re-decide direction, and `verification.py` runs it BEFORE
`app.portfolio.control.classify`, feeding it the `within_bounds` that L61's
`Action` has always taken on trust from its caller.

**No component can widen its own envelope.** The envelope is frozen, it is
built from configuration rather than from anything a control loop produces, and
every field of it is a hard limit -- so an action that proposes to change one
is an action that LOOSENS a hard constraint, which `classify()` already returns
FORBIDDEN for, with no approval path. The rule needs no new mechanism; it needs
the envelope to be inside the thing L61 already protects. There is a test.

Every default here is a **configuration decision, not a measurement.** No
autonomous action has ever been applied on this platform, so there is no
observed distribution of allocation changes or action rates to fit to. They are
recorded in `AUTONOMOUS_ACTION_SAFETY_ENVELOPE.md` as assumptions requiring
approval, and they are deliberately tight: the cost of a too-small envelope is
an action that needs a person, and the cost of a too-large one is the thing
this level exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class SafetyEnvelope:
    """What an autonomous action may do without a person.

    Frozen, and built from configuration. A control loop that could construct
    its own envelope would be a control loop with no envelope.
    """

    #: Fraction of an account's capital one action may move between strategies.
    max_allocation_change: Decimal = Decimal("0.10")
    #: Absolute capital one action may move, in the account's currency.
    max_capital_movement: Decimal = Decimal("1000")
    #: Fraction of gross exposure one action may change.
    max_exposure_change: Decimal = Decimal("0.10")
    #: Actions per target per window.
    max_actions_per_window: int = 6
    action_window: timedelta = timedelta(hours=1)
    #: Time that must pass before the same target is touched again.
    min_cooldown: timedelta = timedelta(minutes=5)
    #: Actions in flight at once, across all targets.
    max_concurrent_actions: int = 3
    #: How many actions may follow from one observation. A chain is how a
    #: control loop turns one surprise into a cascade.
    max_chain_length: int = 3
    max_retries: int = 1
    #: Confidence an advisory input must carry before it may move anything.
    min_confidence: Decimal = Decimal("0.60")
    #: How old the evidence may be. Matches `decision.DEFAULT_MAX_AGE`; the
    #: two are separate numbers with the same value, and a test asserts they
    #: agree so a change to one cannot silently diverge from the other.
    max_data_age: timedelta = timedelta(minutes=5)


DEFAULT_ENVELOPE = SafetyEnvelope()


@dataclass(frozen=True)
class EnvelopeBreach:
    """One bound that a proposal exceeds, with both numbers in it.

    Both numbers, always. "Exceeds the allocation limit" is not something an
    operator can act on; "moves 0.25 against a limit of 0.10" is.
    """

    bound: str
    limit: str
    proposed: str

    def __str__(self) -> str:
        return f"{self.bound}: proposed {self.proposed} against a limit of {self.limit}"


def within_envelope(
    *,
    envelope: SafetyEnvelope = DEFAULT_ENVELOPE,
    allocation_change: Decimal | None = None,
    capital_movement: Decimal | None = None,
    exposure_change: Decimal | None = None,
    confidence: Decimal | None = None,
    data_age: timedelta | None = None,
    actions_in_window: int = 0,
    concurrent_actions: int = 0,
    chain_length: int = 1,
    retries: int = 0,
) -> tuple[EnvelopeBreach, ...]:
    """Every bound this proposal exceeds. Empty means it fits.

    Magnitudes are compared on their ABSOLUTE value: a 25% cut and a 25% raise
    are the same size, and the direction is L61's question rather than this
    one's. A magnitude that is `None` is not checked -- absent is not zero, and
    a proposal that does not state its size is caught by
    `verification.py`, which requires the sizes an action's kind implies.
    """
    breaches: list[EnvelopeBreach] = []

    def over(name: str, value: Decimal | None, limit: Decimal) -> None:
        if value is not None and abs(value) > limit:
            breaches.append(EnvelopeBreach(name, str(limit), str(value)))

    over("max_allocation_change", allocation_change, envelope.max_allocation_change)
    over("max_capital_movement", capital_movement, envelope.max_capital_movement)
    over("max_exposure_change", exposure_change, envelope.max_exposure_change)

    if confidence is not None and confidence < envelope.min_confidence:
        breaches.append(
            EnvelopeBreach("min_confidence", str(envelope.min_confidence), str(confidence))
        )
    if data_age is not None and data_age > envelope.max_data_age:
        breaches.append(EnvelopeBreach("max_data_age", str(envelope.max_data_age), str(data_age)))
    if actions_in_window > envelope.max_actions_per_window:
        breaches.append(
            EnvelopeBreach(
                "max_actions_per_window",
                str(envelope.max_actions_per_window),
                str(actions_in_window),
            )
        )
    if concurrent_actions > envelope.max_concurrent_actions:
        breaches.append(
            EnvelopeBreach(
                "max_concurrent_actions",
                str(envelope.max_concurrent_actions),
                str(concurrent_actions),
            )
        )
    if chain_length > envelope.max_chain_length:
        breaches.append(
            EnvelopeBreach("max_chain_length", str(envelope.max_chain_length), str(chain_length))
        )
    if retries > envelope.max_retries:
        breaches.append(EnvelopeBreach("max_retries", str(envelope.max_retries), str(retries)))

    return tuple(breaches)


#: Targets that ARE the envelope. An action aimed at one of these is asking to
#: change the bounds it is being judged against, which is why it is named here
#: and marked as touching a hard limit rather than being left to look like an
#: ordinary parameter change.
ENVELOPE_TARGETS: frozenset[str] = frozenset(
    {
        "max_allocation_change",
        "max_capital_movement",
        "max_exposure_change",
        "max_actions_per_window",
        "action_window",
        "min_cooldown",
        "max_concurrent_actions",
        "max_chain_length",
        "max_retries",
        "min_confidence",
        "max_data_age",
        "safety_envelope",
    }
)


__all__ = [
    "DEFAULT_ENVELOPE",
    "ENVELOPE_TARGETS",
    "EnvelopeBreach",
    "SafetyEnvelope",
    "within_envelope",
]
