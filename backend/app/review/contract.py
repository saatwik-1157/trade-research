"""What a review IS: the vocabulary, the shape, and the three kinds of claim.

**Every statement carries what kind of claim it is.** Section 41, and it is the
decision that shapes the module:

    OBSERVED      a fact read from a recorded row. "Entry was 1.1030."
    INTERPRETED   a reading OF those facts. "The stop was 100 points away,
                  which is 0.9% of price."
    HYPOTHESIS    a possible explanation, offered as one. "Elevated spread may
                  explain part of the adverse initial movement."

A review that mixed them would be read entirely as the first kind, which is the
failure §23 describes: *"The market wanted to go higher"* presented beside
*"Entry was 1.1030"* borrows the second's credibility. So `Evidence` cannot be
constructed without a kind, and the API and the dashboard both render it.

**UNKNOWN is a rating, and it is the honest default.** Section 12 is explicit:
do not label a trade non-compliant when the required strategy data was not
recorded. A missing record is not a fault in the trade, and the two must not
look alike -- the same distinction L26 drew between BLOCKED and FAIL, and L29
between INSUFFICIENT_DATA and HEALTHY.

**A rating is never inferred from the outcome.** Section 13: a losing trade can
have a high-quality entry and a winning trade can have poor execution. The
assessors in `checks.py` that produce entry and strategy ratings are handed a
context that structurally cannot contain the result -- see `context.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

#: The structured shape's version. Bumped when a section is added, removed or
#: given a different meaning -- §33 and §38. A stored review carries the version
#: it was written under, so a reader never has to guess which shape it is.
REVIEW_SCHEMA_VERSION = "1.0"

#: §34. The narrative template's version, stored beside the review. Changing the
#: wording without changing this would leave two differently-worded reviews
#: claiming to be the same thing.
PROMPT_VERSION = "trade_review_prompt_v1"


class ReviewStatus(StrEnum):
    """Section 10. Only states this pipeline can actually reach.

    L28's lifecycle rule, applied again: a state nothing can set is a state that
    reads as a promise. `cancelled` IS reachable -- a queued review whose trade
    was superseded by a regeneration request is cancelled rather than run.
    """

    pending = "PENDING"
    processing = "PROCESSING"
    completed = "COMPLETED"
    failed = "FAILED"
    retrying = "RETRYING"
    cancelled = "CANCELLED"


#: Statuses from which no further work happens. A review in one of these is
#: never picked up by the worker again without an explicit regeneration.
TERMINAL: frozenset[ReviewStatus] = frozenset(
    {ReviewStatus.completed, ReviewStatus.failed, ReviewStatus.cancelled}
)


class Rating(StrEnum):
    """A section's assessment.

    Four values, and `unknown` is not a fifth-best: it says the evidence needed
    to rate this was not recorded, which is a statement about the platform
    rather than about the trade.
    """

    good = "GOOD"
    fair = "FAIR"
    poor = "POOR"
    unknown = "UNKNOWN"


class Compliance(StrEnum):
    """Section 12. Did the trade match the strategy it claims?"""

    compliant = "COMPLIANT"
    partially_compliant = "PARTIALLY_COMPLIANT"
    non_compliant = "NON_COMPLIANT"
    #: The strategy definition, or the fields needed to check it, were not
    #: recorded. NOT `non_compliant`: §12 forbids that conflation explicitly.
    unknown = "UNKNOWN"


class Outcome(StrEnum):
    """What happened, from the recorded P&L. Deterministic, never inferred."""

    win = "WIN"
    loss = "LOSS"
    breakeven = "BREAKEVEN"
    unknown = "UNKNOWN"


class EvidenceKind(StrEnum):
    """Section 41. What kind of claim a line is making."""

    observed = "OBSERVED"
    interpreted = "INTERPRETED"
    hypothesis = "HYPOTHESIS"


@dataclass(frozen=True)
class Evidence:
    """One statement, its kind, and where it came from.

    `source` names the recorded field or system behind an OBSERVED line, so a
    reader can go and check it. An INTERPRETED line names what it was derived
    from; a HYPOTHESIS names nothing, because there is nothing to check -- and
    that absence is itself informative.
    """

    kind: EvidenceKind
    statement: str
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "statement": self.statement, "source": self.source}


def observed(statement: str, source: str) -> Evidence:
    """A fact from a recorded row. A source is REQUIRED.

    Deliberately: an OBSERVED line with nowhere to check it is exactly the
    fabrication §5 forbids, and making the argument mandatory means the type
    system asks the question rather than a reviewer noticing later.
    """
    return Evidence(EvidenceKind.observed, statement, source)


def interpreted(statement: str, source: str | None = None) -> Evidence:
    return Evidence(EvidenceKind.interpreted, statement, source)


def hypothesis(statement: str) -> Evidence:
    """A possible explanation, and it is labelled as one."""
    return Evidence(EvidenceKind.hypothesis, statement, None)


@dataclass
class Section:
    """One rated aspect of the trade, with the evidence behind the rating."""

    rating: Rating = Rating.unknown
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Why the rating is UNKNOWN, when it is. Present only in that case, so an
    #: unrated section says which data was missing rather than being blank.
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rating": str(self.rating),
            "evidence": [e.as_dict() for e in self.evidence],
            "warnings": self.warnings,
            "unavailable_reason": self.unavailable_reason,
        }


def unrated(reason: str) -> Section:
    """A section that could not be assessed, and says which data was missing."""
    return Section(rating=Rating.unknown, unavailable_reason=reason)


@dataclass
class Completeness:
    """How much of the input was actually available. Section 22.

    Review confidence is computed from THIS, not reported by a narrative
    generator. §22 says confidence should reflect data completeness and evidence
    quality, and a self-reported number reflects neither -- it is the model's
    opinion of its own prose.
    """

    available: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        total = len(self.available) + len(self.missing)
        return (len(self.available) / total) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": sorted(self.available),
            "missing": sorted(self.missing),
            "fraction": round(self.fraction, 4),
            "note": (
                "review confidence is derived from this, not asserted by the "
                "narrative generator. A review of a trade whose strategy, risk and "
                "sizing records are all absent is a low-confidence review however "
                "fluent it reads."
            ),
        }


@dataclass
class TradeReview:
    """The structured output. Section 6.

    **Structured first, narrative second.** §6 says not to rely only on
    free-form text, and the ordering here is the reason: every rating and every
    piece of evidence is produced deterministically from recorded rows, and the
    `summary` is written from them. A provider that returned prose contradicting
    a section is rejected by `validation.py` rather than stored.
    """

    trade_id: str
    environment: str
    outcome: Outcome
    summary: str = ""

    strategy_alignment: Section = field(default_factory=lambda: unrated("not assessed"))
    entry_quality: Section = field(default_factory=lambda: unrated("not assessed"))
    exit_quality: Section = field(default_factory=lambda: unrated("not assessed"))
    risk_quality: Section = field(default_factory=lambda: unrated("not assessed"))
    execution_quality: Section = field(default_factory=lambda: unrated("not assessed"))

    compliance: Compliance = Compliance.unknown
    market_context: dict[str, Any] = field(default_factory=dict)
    ai_context: dict[str, Any] = field(default_factory=dict)

    key_factors: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lessons: list[Evidence] = field(default_factory=list)
    follow_up_questions: list[str] = field(default_factory=list)

    completeness: Completeness = field(default_factory=Completeness)
    confidence: float = 0.0

    #: §21. TWO model identities, never one. The model that produced the
    #: original trade prediction and the model that produced this review are
    #: different questions, and a single `model` field would answer neither.
    review_model: str = ""
    review_model_version: str = ""
    prediction_model: str | None = None
    prediction_model_version: str | None = None

    schema_version: str = REVIEW_SCHEMA_VERSION
    prompt_version: str = PROMPT_VERSION
    generated_at: datetime | None = None

    def sections(self) -> dict[str, Section]:
        return {
            "strategy_alignment": self.strategy_alignment,
            "entry_quality": self.entry_quality,
            "exit_quality": self.exit_quality,
            "risk_quality": self.risk_quality,
            "execution_quality": self.execution_quality,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "environment": self.environment,
            "outcome": str(self.outcome),
            "summary": self.summary,
            **{name: section.as_dict() for name, section in self.sections().items()},
            "compliance": str(self.compliance),
            "market_context": self.market_context,
            "ai_context": self.ai_context,
            "key_factors": [e.as_dict() for e in self.key_factors],
            "warnings": self.warnings,
            "lessons": [e.as_dict() for e in self.lessons],
            "follow_up_questions": self.follow_up_questions,
            "completeness": self.completeness.as_dict(),
            "confidence": round(self.confidence, 4),
            "attribution": {
                "review_model": self.review_model,
                "review_model_version": self.review_model_version,
                "prediction_model": self.prediction_model,
                "prediction_model_version": self.prediction_model_version,
                "note": (
                    "the model that REVIEWED this trade and the model that PREDICTED "
                    "it are different, and are recorded separately. Section 21."
                ),
            },
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "reading_note": (
                "every statement carries its KIND. OBSERVED is a fact from a recorded "
                "row and names it; INTERPRETED is a reading of those facts; HYPOTHESIS "
                "is a possible explanation offered as one. They are not the same claim "
                "and are not presented as though they were."
            ),
        }


def outcome_of(net_profit: Any) -> Outcome:
    """WIN, LOSS or BREAKEVEN, from the recorded figure. Never from a narrative."""
    if net_profit is None:
        return Outcome.unknown
    value = float(net_profit)
    if value > 0:
        return Outcome.win
    if value < 0:
        return Outcome.loss
    return Outcome.breakeven


__all__ = [
    "PROMPT_VERSION",
    "REVIEW_SCHEMA_VERSION",
    "TERMINAL",
    "Completeness",
    "Compliance",
    "Evidence",
    "EvidenceKind",
    "Outcome",
    "Rating",
    "ReviewStatus",
    "Section",
    "TradeReview",
    "hypothesis",
    "interpreted",
    "observed",
    "outcome_of",
    "unrated",
]
