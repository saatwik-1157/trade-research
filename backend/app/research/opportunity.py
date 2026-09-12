"""Autonomous opportunity research. **L83.**

Answers "what deserves research attention now, and why" — and is built so that
the answer is allowed to be *nothing*.

**This module has no execution authority and cannot acquire one.** It imports
nothing from `app.oms`, `app.brokers`, `app.execution` or `app.positions`, and
`test_opportunity.py` asserts that by parsing the import graph. An
`Opportunity` is a research task, never an order; the strongest thing it can
produce is a `ResearchAction`, which a human or the existing pipeline may act
on through the RiskEngine like any other intent.

## Why this is shaped defensively

An opportunity score is structurally the same object as the composite score in
`tools/score.py`, and that one **has been backtested and has no forward-return
edge** — mean IC 0.00-0.03, t < 1, negative quintile spreads. `CLAUDE.md`
records six further searches across five universes that all failed to clear
their own permutation nulls. So this module is written on the assumption that
its own output is probably not predictive, and everything that could hide that
is refused:

* `EdgeStrength.no_edge` is the **default**, not a failure branch. Edge is
  granted only by `app.research.selection`, which asks how many candidates a
  coin flip clears over the same search — the arithmetic that made every one
  of those searches readable.
* A quantity that cannot be computed is `None` and is named in `data_gaps`.
  It is never defaulted, and never treated as zero. A dropped component lowers
  `coverage`; it does not silently lower the score.
* `priority_score` is returned beside `confidence`, `uncertainty`,
  `reason_codes` and `blocking_factors`, and a caller that reads only the
  score is reading the least reliable field in the object.

## What cannot be computed here, and is not faked

The brief asks for expectation gaps, channel intelligence, guidance
credibility and historical base rates. **This repository has no data source for
any of them** — no consensus estimates, no channel panel, no guidance history,
and no point-in-time fundamental panel (`PROJECT_STATE.json`: 705 market bars
across 2 symbols, 1 dataset). Those assessments therefore return
`Verdict.insufficient_data` with the missing input named.

That is the honest result and it is deliberately not a stub returning a
neutral 50. A base rate computed from a panel that does not exist is the
metals-points error wearing a new hat: an arithmetic operation on quantities
that are not what they claim to be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

__all__ = [
    "Assessment",
    "Direction",
    "EdgeStrength",
    "Evidence",
    "FalseOpportunityRisk",
    "Freshness",
    "Novelty",
    "Opportunity",
    "OpportunityKind",
    "OpportunityState",
    "ResearchValue",
    "Verdict",
    "assess",
    "classify_divergence",
    "freshness_of",
    "novelty_of",
]


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class Verdict(StrEnum):
    """The outcome of any assessment that can decline to answer.

    `insufficient_data` is not a failure. It is the correct answer whenever the
    input a judgement needs was never collected, and it must never be
    collapsed into a neutral score -- a neutral score is a claim.
    """

    insufficient_data = "insufficient_data"
    no = "no"
    weak = "weak"
    yes = "yes"


class OpportunityKind(StrEnum):
    """What kind of change was detected. Only kinds this repo can compute."""

    fundamental_acceleration = "fundamental_acceleration"
    fundamental_deceleration = "fundamental_deceleration"
    margin_inflection = "margin_inflection"
    fcf_inflection = "fcf_inflection"
    fundamental_price_divergence = "fundamental_price_divergence"
    thesis_break = "thesis_break"
    thesis_confirmation = "thesis_confirmation"
    edge_decay = "edge_decay"
    information_conflict = "information_conflict"
    #: Detected, but this repository has no data source for it. Recorded so
    #: the catalogue is honest about its own coverage rather than silently
    #: short. Never produced by `assess`.
    requires_absent_data = "requires_absent_data"


class OpportunityState(StrEnum):
    """Lifecycle. Mirrors the brief; nothing skips a state."""

    discovered = "discovered"
    screening = "screening"
    research_required = "research_required"
    researching = "researching"
    evidence_collection = "evidence_collection"
    verifying = "verifying"
    thesis_forming = "thesis_forming"
    thesis_validating = "thesis_validating"
    watchlist = "watchlist"
    waiting = "waiting"
    decision_pending = "decision_pending"
    decision_ready = "decision_ready"
    blocked = "blocked"
    stale = "stale"
    thesis_weakening = "thesis_weakening"
    thesis_invalidated = "thesis_invalidated"
    thesis_confirmed = "thesis_confirmed"
    closed = "closed"


class Novelty(StrEnum):
    new = "new"
    known = "known"
    recurring = "recurring"
    rediscovered = "rediscovered"
    already_researched = "already_researched"
    duplicate = "duplicate"
    stale = "stale"
    thesis_changed = "thesis_changed"


class Freshness(StrEnum):
    fresh = "fresh"
    aging = "aging"
    stale = "stale"
    expired = "expired"
    requires_revalidation = "requires_revalidation"


class EdgeStrength(StrEnum):
    """Default is `no_edge`. Nothing here promotes without `selection`."""

    no_edge = "no_edge"
    weak_edge = "weak_edge"
    potential_edge = "potential_edge"
    supported_edge = "supported_edge"
    strong_edge = "strong_edge"
    uncertain_edge = "uncertain_edge"


class ResearchValue(StrEnum):
    not_worth_researching = "not_worth_researching"
    low_value = "low_value"
    medium_value = "medium_value"
    high_value = "high_value"


class Direction(StrEnum):
    up = "up"
    down = "down"
    flat = "flat"
    unknown = "unknown"


class FalseOpportunityRisk(StrEnum):
    unknown = "unknown"
    low = "low"
    elevated = "elevated"
    high = "high"


#: Reason codes. A blocking factor is a reason the opportunity must not be
#: promoted; a reason code merely explains the score.
BLOCKING = {
    "INSUFFICIENT_DATA",
    "NO_PROVENANCE",
    "PERIOD_MISALIGNED",
    "DUPLICATE",
    "EXPIRED",
    "FALSE_OPPORTUNITY_HIGH",
    "THESIS_INVALIDATED",
}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One sourced observation. Unsourced evidence is refused at construction.

    `source` is the provenance the whole project rests on -- an EDGAR
    accession number, a snapshot path, a venue read. A figure with no source
    is exactly what `tools/verify.py --strict` exists to catch, and admitting
    one here would route it around that gate.
    """

    key: str
    value: float | None
    source: str
    observed_at: datetime
    unit: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError(f"evidence {self.key!r} has no source")
        if self.observed_at.tzinfo is None:
            # Naive timestamps are how look-ahead gets in unnoticed: two
            # naive stamps from different zones compare as though they were
            # the same clock.
            raise ValueError(f"evidence {self.key!r} has a naive observed_at")


@dataclass(frozen=True)
class Assessment:
    """What the engine concluded, and everything needed to disbelieve it."""

    kind: OpportunityKind
    state: OpportunityState
    novelty: Novelty
    freshness: Freshness
    edge: EdgeStrength
    research_value: ResearchValue
    false_opportunity: FalseOpportunityRisk
    materiality: Verdict
    expectation_gap: Verdict
    base_rate: Verdict
    priority_score: float | None
    confidence: float | None
    uncertainty: str
    coverage: float
    reason_codes: tuple[str, ...] = ()
    blocking_factors: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()
    counter_thesis: tuple[str, ...] = ()

    @property
    def promotable(self) -> bool:
        """Whether this may leave `screening`. Blocking factors are absolute."""
        return not self.blocking_factors


@dataclass(frozen=True)
class Opportunity:
    """A research task. Never an order."""

    symbol: str
    kind: OpportunityKind
    discovered_at: datetime
    evidence: tuple[Evidence, ...] = ()
    thesis: str = ""
    dedupe_key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------


def classify_divergence(fundamentals: Direction, price: Direction) -> tuple[str, Verdict]:
    """§8. Fundamental direction against price direction.

    Returns the label and whether it is potentially actionable. Agreement is
    explicitly NOT an opportunity: both rising is the ordinary state of a
    working business and reporting it as a finding is how a screen fills up
    with noise.
    """
    if Direction.unknown in (fundamentals, price):
        return ("unknown", Verdict.insufficient_data)
    if fundamentals == price:
        return (f"fundamentals_{fundamentals}_price_{price}", Verdict.no)
    if fundamentals is Direction.flat or price is Direction.flat:
        return (f"fundamentals_{fundamentals}_price_{price}", Verdict.weak)
    return (f"fundamentals_{fundamentals}_price_{price}", Verdict.yes)


def novelty_of(candidate: Opportunity, seen: dict[str, Opportunity], *, now: datetime) -> Novelty:
    """§5. Whether this is genuinely new.

    Keyed on `dedupe_key`, which the caller derives from the underlying EVENT
    rather than from the detection run -- so the same filing detected twice is
    one opportunity, and the brief's rule holds: never two records for one
    event unless the economic thesis is materially different.
    """
    key = candidate.dedupe_key or f"{candidate.symbol}:{candidate.kind}"
    prior = seen.get(key)
    if prior is None:
        return Novelty.new
    if prior.thesis and candidate.thesis and prior.thesis != candidate.thesis:
        # Same event, different economic reading. That is a new opportunity.
        return Novelty.thesis_changed
    age = now - prior.discovered_at
    if age > timedelta(days=365):
        return Novelty.rediscovered
    if age > timedelta(days=30):
        return Novelty.recurring
    return Novelty.duplicate


def freshness_of(
    opportunity: Opportunity,
    *,
    now: datetime,
    aging_days: float = 7.0,
    stale_days: float = 30.0,
    expired_days: float = 90.0,
) -> Freshness:
    """§18. Time decay, measured from the OLDEST evidence, not the newest.

    The oldest, because a thesis is only as current as its weakest input. A
    refreshed price beside a two-year-old filing is not a fresh opportunity;
    reading the newest stamp would report it as one.
    """
    stamps = [e.observed_at for e in opportunity.evidence] or [opportunity.discovered_at]
    age = now - min(stamps)
    if age >= timedelta(days=expired_days):
        return Freshness.expired
    if age >= timedelta(days=stale_days):
        return Freshness.stale
    if age >= timedelta(days=aging_days):
        return Freshness.aging
    return Freshness.fresh


def _false_opportunity(
    values: dict[str, float | None],
) -> tuple[FalseOpportunityRisk, tuple[str, ...]]:
    """§23. The checks this repository can actually compute.

    Only four of the brief's thirteen are computable from EDGAR XBRL without
    data the project does not have. The rest are absent rather than passing --
    a check that cannot run must not contribute a clean bill of health.
    """
    flags: list[str] = []
    fcf = values.get("fcf")
    net_income = values.get("net_income")
    if fcf is not None and net_income is not None and net_income > 0:
        if fcf < net_income * 0.5:
            # Earnings the business is not converting to cash.
            flags.append("WEAK_CASH_CONVERSION")
    growth = values.get("revenue_growth")
    margin_delta = values.get("margin_delta")
    if growth is not None and growth < 0 and margin_delta is not None:
        if margin_delta > 0:
            # Margins improving while the top line shrinks is often cost
            # cutting reaching its floor, not an inflection.
            flags.append("MARGIN_UP_ON_FALLING_REVENUE")
    one_off = values.get("one_off_share_of_income")
    if one_off is not None and one_off > 0.25:
        flags.append("ONE_OFF_DRIVEN")
    cheapness = values.get("valuation_percentile")
    quality_delta = values.get("quality_delta")
    if cheapness is not None and cheapness < 0.2 and quality_delta is not None:
        if quality_delta < 0:
            flags.append("CHEAP_AND_DETERIORATING")

    if not flags:
        # No flag is not the same as no risk when the inputs were absent.
        computable = any(
            values.get(k) is not None for k in ("fcf", "revenue_growth", "one_off_share_of_income")
        )
        return (
            (FalseOpportunityRisk.low if computable else FalseOpportunityRisk.unknown),
            (),
        )
    if len(flags) >= 2:
        return (FalseOpportunityRisk.high, tuple(flags))
    return (FalseOpportunityRisk.elevated, tuple(flags))


def _counter_thesis(kind: OpportunityKind, flags: tuple[str, ...]) -> tuple[str, ...]:
    """§14. What would make this wrong. Never empty for a real opportunity."""
    out = [
        "The market may already price this; a change that is visible in a "
        "filing is visible to everyone reading the same filing.",
    ]
    if kind in (
        OpportunityKind.fundamental_acceleration,
        OpportunityKind.margin_inflection,
    ):
        out.append("The improvement may be one or two quarters of cycle, not a trend.")
    if kind is OpportunityKind.fundamental_price_divergence:
        out.append(
            "Price may be discounting information the filing does not contain; "
            "an unexplained divergence is more often the market being early "
            "than the market being wrong."
        )
    out.extend(f"Flagged: {f}" for f in flags)
    return tuple(out)


def assess(
    opportunity: Opportunity,
    *,
    seen: dict[str, Opportunity],
    now: datetime | None = None,
    values: dict[str, float | None] | None = None,
    fundamentals: Direction = Direction.unknown,
    price: Direction = Direction.unknown,
    selection_clears: int | None = None,
    selection_candidates: int | None = None,
) -> Assessment:
    """Score one opportunity, and refuse to score it when the inputs are absent.

    `selection_clears` / `selection_candidates` are the only route to an edge
    above `no_edge`, and they come from `app.research.selection` -- how many of
    a search's candidates cleared against how many a coin flip would clear.
    Absent them the answer is `no_edge`, which is also the answer when the
    search was run and did not beat its null.
    """
    now = now or datetime.now(UTC)
    values = values or {}

    reasons: list[str] = []
    blocking: list[str] = []
    gaps: list[str] = []

    # --- provenance ------------------------------------------------------
    if not opportunity.evidence:
        blocking.append("NO_PROVENANCE")
        gaps.append("no evidence attached")

    # --- novelty and freshness ------------------------------------------
    novelty = novelty_of(opportunity, seen, now=now)
    if novelty is Novelty.duplicate:
        blocking.append("DUPLICATE")
    reasons.append(f"novelty:{novelty}")

    fresh = freshness_of(opportunity, now=now)
    if fresh is Freshness.expired:
        blocking.append("EXPIRED")
    reasons.append(f"freshness:{fresh}")

    # --- divergence ------------------------------------------------------
    label, divergence = classify_divergence(fundamentals, price)
    reasons.append(f"divergence:{label}")
    if divergence is Verdict.insufficient_data:
        gaps.append("fundamental or price direction unknown")

    # --- materiality -----------------------------------------------------
    impact = values.get("earnings_impact_pct")
    if impact is None:
        materiality = Verdict.insufficient_data
        gaps.append("earnings_impact_pct absent")
    elif abs(impact) < 0.02:
        materiality = Verdict.no
        reasons.append("materiality: under 2% earnings impact")
    elif abs(impact) < 0.10:
        materiality = Verdict.weak
    else:
        materiality = Verdict.yes

    # --- the three the repository has no data for ------------------------
    # Named individually so a reader can see WHICH input is missing rather
    # than a single "not implemented".
    expectation_gap = Verdict.insufficient_data
    gaps.append("expectation gap needs consensus estimates; no source configured")
    base_rate = Verdict.insufficient_data
    gaps.append("base rate needs a point-in-time fundamental panel; none exists")

    # --- false opportunity ----------------------------------------------
    false_risk, flags = _false_opportunity(values)
    reasons.extend(flags)
    if false_risk is FalseOpportunityRisk.high:
        blocking.append("FALSE_OPPORTUNITY_HIGH")

    # --- edge ------------------------------------------------------------
    edge = EdgeStrength.no_edge
    if selection_clears is not None and selection_candidates is not None:
        if selection_candidates <= 0:
            edge = EdgeStrength.uncertain_edge
        elif selection_clears > selection_candidates:
            # More cleared than chance would produce over the same grid.
            edge = EdgeStrength.potential_edge
            reasons.append(
                f"selection: {selection_clears} cleared vs "
                f"{selection_candidates} expected by chance"
            )
        else:
            reasons.append(
                f"selection: {selection_clears} cleared vs "
                f"{selection_candidates} expected by chance -- at or below chance"
            )
    else:
        gaps.append("no selection result; edge cannot rise above no_edge")

    # --- coverage and score ---------------------------------------------
    components = {
        "materiality": materiality is not Verdict.insufficient_data,
        "divergence": divergence is not Verdict.insufficient_data,
        "expectation_gap": expectation_gap is not Verdict.insufficient_data,
        "base_rate": base_rate is not Verdict.insufficient_data,
        "false_opportunity": false_risk is not FalseOpportunityRisk.unknown,
    }
    coverage = sum(components.values()) / len(components)

    if coverage < 0.5:
        # Below half the components, a composite is a number with a shape
        # rather than a measurement. `tools/score.py` reports a dropped
        # component through `coverage` and never defaults it to 50; the same
        # rule applies here, and at this coverage the honest output is none.
        blocking.append("INSUFFICIENT_DATA")
        score: float | None = None
        confidence: float | None = None
    else:
        base = 0.0
        base += {Verdict.yes: 0.4, Verdict.weak: 0.2}.get(materiality, 0.0)
        base += {Verdict.yes: 0.3, Verdict.weak: 0.15}.get(divergence, 0.0)
        base += {Novelty.new: 0.2, Novelty.thesis_changed: 0.15}.get(novelty, 0.0)
        base += {Freshness.fresh: 0.1, Freshness.aging: 0.05}.get(fresh, 0.0)
        base -= {
            FalseOpportunityRisk.high: 0.4,
            FalseOpportunityRisk.elevated: 0.2,
        }.get(false_risk, 0.0)
        score = round(max(0.0, min(1.0, base)), 4)
        confidence = round(coverage, 4)

    research_value = ResearchValue.not_worth_researching
    if score is not None and not blocking:
        if score >= 0.6:
            research_value = ResearchValue.high_value
        elif score >= 0.35:
            research_value = ResearchValue.medium_value
        elif score > 0.0:
            research_value = ResearchValue.low_value

    state = OpportunityState.blocked if blocking else OpportunityState.screening

    return Assessment(
        kind=opportunity.kind,
        state=state,
        novelty=novelty,
        freshness=fresh,
        edge=edge,
        research_value=research_value,
        false_opportunity=false_risk,
        materiality=materiality,
        expectation_gap=expectation_gap,
        base_rate=base_rate,
        priority_score=score,
        confidence=confidence,
        uncertainty=(
            "score is absent below 50% component coverage; edge is no_edge "
            "unless a selection result says otherwise"
        ),
        coverage=round(coverage, 4),
        reason_codes=tuple(reasons),
        blocking_factors=tuple(dict.fromkeys(blocking)),
        data_gaps=tuple(dict.fromkeys(gaps)),
        counter_thesis=_counter_thesis(opportunity.kind, flags),
    )
