"""What the platform may do right now, and which layer decided it. **L60.**

Two rules, and they are the whole module:

**1. Missing is never safe.** Every input carries where it came from, when, and
whether that is still true. An input that is `MISSING`, `STALE` or `INVALID`
makes the decision MORE conservative -- never less. L60 step 4 states it and
this platform has already been bitten by the opposite: `PortfolioState.open_symbols`
defaulted to an empty frozenset, which is indistinguishable from "nothing is
open", so the one aggregate risk control enabled by default could not fire
(L53). An empty answer read as a positive claim.

**2. A lower layer cannot relax a higher one.** L60 step 13:

    HARD SAFETY > RISK > PORTFOLIO > STRATEGY > MODEL/SIGNAL > OPTIMIZATION > PREFERENCE

An optimizer that is confident, a model that is confident, an AI that is
confident -- none of them can turn a RISK-layer restriction back into ALLOW.
`decide()` cannot express that outcome: it takes the most restrictive verdict
found and records which layer produced it, so "AI said buy" and "risk said no"
resolve one way by construction rather than by review.

**This module decides nothing about orders.** It produces a recommendation and
the reasons for it. The RiskEngine remains the final veto, the OMS owns order
state, and nothing here reaches either -- asserted by a test, not promised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any


class Layer(IntEnum):
    """Who decided. Higher wins, and the ordering IS the safety property.

    `IntEnum` so the comparison is the ordering rather than a lookup table that
    could disagree with the documentation next to it.
    """

    PREFERENCE = 1
    OPTIMIZATION = 2
    SIGNAL = 3  # models, AI, strategy signals
    STRATEGY = 4
    PORTFOLIO = 5
    RISK = 6
    HARD_SAFETY = 7  # safe mode, kill switches, unhealthy execution path


class Verdict(StrEnum):
    """What may happen. Ordered from permissive to restrictive by `SEVERITY`."""

    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    REDUCE = "REDUCE"
    RESTRICT = "RESTRICT"
    DEFER = "DEFER"
    PAUSE = "PAUSE"
    REJECT = "REJECT"


#: How restrictive each verdict is. `decide()` takes the maximum, so a new
#: verdict added without an entry here would raise rather than silently sort
#: as the most permissive.
SEVERITY: dict[Verdict, int] = {
    Verdict.ALLOW: 0,
    Verdict.REVIEW: 1,
    Verdict.REDUCE: 2,
    Verdict.RESTRICT: 3,
    Verdict.DEFER: 4,
    Verdict.PAUSE: 5,
    Verdict.REJECT: 6,
}


class Freshness(StrEnum):
    """How much an input is still evidence about now.

    L76 section 5 names six states and four existed. The two added here are not
    decoration, and they are added on different terms:

    `CONFLICTED` could not previously be expressed at all. An input whose
    sources disagree is not missing, not stale and not invalid -- it is
    contested, and until now it had to be mislabelled as one of those three.
    The distinction is the one L74 section 52 and L75 section 6 both require:
    a stale figure can be refreshed, a conflicted one needs a person.

    `AGING` is produced ONLY when a caller supplies a threshold for it. There
    is no default, deliberately. `DEFAULT_MAX_AGE` below already records itself
    as an assumption rather than a measurement, and inventing a second
    unmeasured boundary to sit inside the first would compound that rather than
    inform anything.

    Every value except `FRESH` is treated as degraded by the consumers below,
    so adding states can only make a decision more conservative, never less.

    **There is a second `Freshness` in `app/portfolio/state.py`** with lowercase
    values and three states (`fresh`, `stale`, `unknown`), used by 16 call sites
    against this one's 29. They are NOT interchangeable and merging them is not
    a rename: `state.unknown` means "no timestamp was recorded", which is this
    module's `INVALID`, while this module's `MISSING` -- "we asked and got
    nothing" -- has no counterpart there at all. Left as two, deliberately and
    visibly, rather than merged carelessly. See `app.portfolio.state.Freshness`.
    """

    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"
    MISSING = "MISSING"
    INVALID = "INVALID"


#: How old an input may be before it stops being evidence about now. A default
#: rather than a measurement: nothing on this platform has a measured staleness
#: distribution, and it is recorded as an assumption requiring approval.
DEFAULT_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class Staleness:
    """How long one KIND of input stays evidence about now.

    **`max_age=None` means age cannot make this input stale**, and that is not a
    licence to trust it forever. Some facts are invalidated by an EVENT rather
    than by a clock: guidance is exactly as true the day before an earnings call
    as the day after it was issued, and then it can be worthless in a minute. A
    duration cannot express that, and a duration chosen anyway would say
    something false in both directions -- stale while still current, fresh the
    moment it stopped being true.

    Those kinds carry `aging_after` instead, which never yields `STALE`. `AGING`
    on an event-bounded input means "nobody has checked whether the event
    happened", which is a statement about the platform rather than about the
    fact -- the same distinction the review layer draws between UNKNOWN and
    POOR.
    """

    max_age: timedelta | None
    aging_after: timedelta | None
    why: str


#: What each kind of input's freshness actually depends on. **L81 section 6:**
#: *"Do NOT use one universal freshness threshold for all data types."*
#:
#: Every duration here is an ASSUMPTION and not a measurement, on the same terms
#: `DEFAULT_MAX_AGE` records itself. Nothing on this platform has a measured
#: staleness distribution for any of them, and a table of confident-looking
#: numbers would hide that. They are written down so they can be argued with and
#: approved, which is the only thing that makes them better than one value used
#: everywhere.
POLICY: dict[str, Staleness] = {
    "quote": Staleness(
        timedelta(seconds=30), timedelta(seconds=10),
        "a price is evidence about a moment; the venue quotes continuously and a "
        "minute-old quote has been superseded many times over",
    ),
    "position": Staleness(
        timedelta(minutes=5), timedelta(minutes=2),
        "what the venue holds changes only when something fills, but a stale "
        "reading is how a platform trades against a position it no longer has",
    ),
    "account": Staleness(
        timedelta(minutes=5), timedelta(minutes=2),
        "balance and margin move with every open position's float",
    ),
    "risk_state": Staleness(
        timedelta(minutes=1), timedelta(seconds=30),
        "a limit breached a minute ago and not re-read is a limit not enforced",
    ),
    "fundamentals": Staleness(
        timedelta(days=45), timedelta(days=21),
        "a filed quarter stays true until the next one is filed. 45 days spans a "
        "quarter plus filing lag; 21 marks when the next report is near enough "
        "that the figures are about to be superseded",
    ),
    "guidance": Staleness(
        None, timedelta(days=21),
        "EVENT-BOUNDED. Guidance is invalidated by the company changing it, not "
        "by time passing -- it can die the minute an earnings call starts and is "
        "otherwise as good as the day it was issued",
    ),
    "earnings": Staleness(
        None, timedelta(days=21),
        "EVENT-BOUNDED. A reported quarter does not decay; it is superseded by "
        "the next report",
    ),
    "channel": Staleness(
        timedelta(days=30), timedelta(days=7),
        "an observation about demand describes the window it was taken in. This "
        "is the least defensible entry in the table, because no channel source "
        "is wired and nothing has measured how fast one would decay",
    ),
}


def staleness_for(kind: str | None) -> Staleness:
    """The policy for one kind, or the universal default.

    An unknown kind gets the DEFAULT rather than an error, deliberately: a
    caller naming a kind nobody has written a policy for should get the
    strictest thing available, not an exception in a decision path.
    """
    if kind and kind in POLICY:
        return POLICY[kind]
    return Staleness(
        DEFAULT_MAX_AGE, None, "no policy for this kind; the universal default applies"
    )


@dataclass(frozen=True)
class Input:
    """One fact the decision rests on, and whether it is still true."""

    name: str
    source: str
    value: Any = None
    at: datetime | None = None
    version: str | None = None
    #: Set when two or more sources disagree about this value. It is recorded
    #: on the input rather than resolved here: choosing a winner is the
    #: judgement L75 section 6 forbids making silently.
    conflicted: bool = False
    #: Which staleness policy applies. `None` uses the universal default, so
    #: every caller written before POLICY existed behaves exactly as it did.
    kind: str | None = None

    def freshness(
        self,
        *,
        now: datetime,
        max_age: timedelta | None = None,
        aging_after: timedelta | None = None,
    ) -> Freshness:
        """`MISSING` beats `INVALID` beats `CONFLICTED` beats `STALE` beats
        `FRESH`, and none of them is `ALLOW`.

        A value of None is MISSING even when a timestamp is present: "we asked
        at 12:00 and got nothing" is an absent fact, not a fresh one.

        The order is by how little the input can be relied on, and `CONFLICTED`
        sits above `STALE` on purpose: a stale figure is one nobody has
        refreshed, and a contested one is a figure two sources disagree about.
        The second cannot be fixed by asking again.

        **Which threshold applies** is the L81 section 6 rule: an explicit
        argument beats this input's `kind` policy, which beats the universal
        default. An input with no `kind` gets `DEFAULT_MAX_AGE` and behaves
        exactly as every caller written before `POLICY` existed.

        A kind whose policy has `max_age=None` is EVENT-BOUNDED -- guidance,
        a reported quarter -- and age alone never makes it `STALE`. It can still
        become `AGING`, which says nobody has checked whether the event that
        would supersede it has happened.
        """
        if self.value is None:
            return Freshness.MISSING
        if self.at is None:
            return Freshness.INVALID
        at = self.at if self.at.tzinfo else self.at.replace(tzinfo=UTC)
        if at > now + timedelta(seconds=1):
            # A timestamp in the future is not fresh data, it is a clock
            # problem or a fabricated reading. Neither is evidence.
            return Freshness.INVALID
        if self.conflicted:
            return Freshness.CONFLICTED

        # An explicit argument beats the policy, which beats the default. The
        # caller who knows this particular reading's shelf life outranks a table
        # written for its kind in general.
        policy = staleness_for(self.kind)
        limit = max_age if max_age is not None else policy.max_age
        soft = aging_after if aging_after is not None else policy.aging_after

        age = now - at
        # `limit is None` is the event-bounded case: age cannot make it stale.
        if limit is not None and age > limit:
            return Freshness.STALE
        if soft is not None and age > soft:
            return Freshness.AGING
        return Freshness.FRESH


@dataclass(frozen=True)
class Finding:
    """One layer's verdict, and why."""

    layer: Layer
    verdict: Verdict
    reason: str
    inputs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.name,
            "verdict": str(self.verdict),
            "reason": self.reason,
            "inputs": list(self.inputs),
        }


@dataclass(frozen=True)
class Decision:
    """The recommendation, the layer that produced it, and everything else.

    `contributing` keeps every finding, including the ones that lost. A
    decision record that showed only the winner would hide that risk and the
    optimizer disagreed -- which is the fact somebody needs after an incident.
    """

    verdict: Verdict
    layer: Layer
    reason: str
    at: datetime
    findings: tuple[Finding, ...]
    degraded_inputs: tuple[tuple[str, Freshness], ...]

    @property
    def allows_new_risk(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.verdict),
            "decided_by": self.layer.name,
            "reason": self.reason,
            "at": self.at.isoformat(),
            "allows_new_risk": self.allows_new_risk,
            "findings": [f.as_dict() for f in self.findings],
            "degraded_inputs": [
                {"input": name, "freshness": str(state)} for name, state in self.degraded_inputs
            ],
            "authority": (
                "A recommendation. The RiskEngine remains the final veto, the OMS owns "
                "order state, and nothing here reaches either."
            ),
        }


@dataclass
class DecisionContext:
    """The inputs and findings a decision is built from.

    Deliberately a collector rather than a calculator: it does not compute
    exposure, health, regime or risk. Those have owners, and a second
    derivation of one fact eventually disagrees with the first.
    """

    now: datetime
    inputs: list[Input] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    max_age: timedelta = DEFAULT_MAX_AGE

    def add_input(self, item: Input) -> None:
        self.inputs.append(item)

    def add_finding(self, finding: Finding) -> None:
        self.findings.append(finding)

    def degraded(self) -> list[tuple[str, Freshness]]:
        """Every input that is not FRESH, with what is wrong with it."""
        out: list[tuple[str, Freshness]] = []
        for item in self.inputs:
            state = item.freshness(now=self.now, max_age=self.max_age)
            if state is not Freshness.FRESH:
                out.append((item.name, state))
        return out

    def require_fresh(self, *names: str, verdict: Verdict = Verdict.DEFER) -> None:
        """Name the inputs a decision cannot safely be made without.

        Anything named here that is not FRESH produces a HARD_SAFETY finding.
        **Not a warning** -- L60 step 4 is explicit that missing critical
        information makes the decision more conservative, and a warning that
        does not change the verdict is not that.
        """
        by_name = {i.name: i for i in self.inputs}
        for name in names:
            item = by_name.get(name)
            state = (
                Freshness.MISSING
                if item is None
                else item.freshness(now=self.now, max_age=self.max_age)
            )
            if state is not Freshness.FRESH:
                self.add_finding(
                    Finding(
                        layer=Layer.HARD_SAFETY,
                        verdict=verdict,
                        reason=(
                            f"{name} is {state} and the decision requires it. Missing "
                            "information is never read as permission"
                        ),
                        inputs=(name,),
                    )
                )


def decide(context: DecisionContext) -> Decision:
    """The most restrictive verdict, and the highest layer that produced it.

    **A lower layer cannot relax a higher one**, and that is enforced by
    construction rather than by a rule somebody has to remember: the winner is
    the most severe verdict present, so an `ALLOW` from OPTIMIZATION cannot
    displace a `RESTRICT` from RISK. Ties break to the higher layer, so the
    reason names the authority a reader should go to.

    With no findings at all the answer is `ALLOW` from PREFERENCE — the
    platform has been given no reason to restrict. That is only safe because
    `require_fresh` turns absent evidence into a finding rather than into
    silence.
    """
    if not context.findings:
        return Decision(
            verdict=Verdict.ALLOW,
            layer=Layer.PREFERENCE,
            reason="no layer reported a reason to restrict",
            at=context.now,
            findings=(),
            degraded_inputs=tuple(context.degraded()),
        )

    winner = max(
        context.findings,
        key=lambda f: (SEVERITY[f.verdict], int(f.layer)),
    )
    return Decision(
        verdict=winner.verdict,
        layer=winner.layer,
        reason=winner.reason,
        at=context.now,
        findings=tuple(context.findings),
        degraded_inputs=tuple(context.degraded()),
    )


__all__ = [
    "DEFAULT_MAX_AGE",
    "SEVERITY",
    "Decision",
    "DecisionContext",
    "Finding",
    "Freshness",
    "Input",
    "Layer",
    "Verdict",
    "decide",
]
