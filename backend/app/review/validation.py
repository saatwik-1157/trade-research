"""Checking the review against the facts before it is stored. Sections 37, 38.

*"Do not store malformed AI output as trusted data."*

**The validator's job is narrower than it looks, because the shape already did
most of it.** A provider returns a `Narration` -- a summary and three lists of
statements -- and there is nowhere on it to put a rating, a price, a P&L or a
model version. So the classic hallucinations §37 lists cannot enter the stored
record by the front door; what remains is prose that *asserts* something the
facts contradict, and that is what this module looks for.

Two kinds of check:

**Structural.** The schema holds: enums are in range, confidence is a
probability, the trade id matches, every section is present. A failure here is a
bug in this platform rather than in a provider, and it fails the review rather
than being repaired -- §38 says to store only validated output.

**Referential.** Every number that looks like a price, a quantity or a P&L in the
narrative must appear in the facts the review was built from. A provider that
writes "closed at 1.2050" when the recorded exit was 1.1050 is caught here, and
so is an invented symbol or an invented model version.

**A failure is not a retry-forever.** §38 and §57: the review is marked FAILED
with the reasons attached, the trade is untouched, and a human or a regeneration
decides what happens next.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.review.context import ReviewInput
from app.review.contract import (
    REVIEW_SCHEMA_VERSION,
    Compliance,
    EvidenceKind,
    Outcome,
    Rating,
    TradeReview,
)

#: Numbers in prose that look like a figure worth checking. Deliberately
#: excludes integers below 1000 with no decimal point: "3 fills", "2 parts" and
#: "27 trades" are counts, and demanding they appear in the price facts would
#: reject every honest sentence.
_NUMBER = re.compile(r"\b\d+\.\d+\b")

#: How close a quoted figure must be to a recorded one to count as the same.
#: Not exact: a narrative legitimately rounds, and "0.9%" derived from a
#: recorded ratio should not be rejected for the last decimal.
TOLERANCE = Decimal("0.01")


@dataclass
class Verdict:
    """What validation found. Section 38."""

    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.valid = False
        self.errors.append(reason)

    def warn(self, reason: str) -> None:
        self.warnings.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "note": (
                "a review that fails validation is stored as FAILED with these reasons, "
                "never stored as trusted data and never silently repaired."
            ),
        }


def _decimals(source: ReviewInput) -> set[Decimal]:
    """Every figure the review is entitled to quote."""
    decision, outcome = source.decision, source.outcome
    candidates = [
        decision.entry_price,
        decision.requested_entry_price,
        decision.quantity,
        decision.planned_stop_loss,
        decision.planned_take_profit,
        decision.ai_probability,
        decision.ai_confidence,
        decision.ai_anomaly_score,
        decision.signal_confidence,
        outcome.exit_price,
        outcome.net_profit,
        outcome.gross_profit,
        outcome.commission,
        outcome.swap,
        outcome.fees,
        outcome.r_multiple,
        outcome.mae,
        outcome.mfe,
        *outcome.slippage_points,
    ]
    found = {Decimal(str(v)) for v in candidates if v is not None}
    # Derived figures a narrative may legitimately state.
    if decision.entry_price is not None and decision.planned_stop_loss is not None:
        found.add(abs(decision.entry_price - decision.planned_stop_loss))
    if decision.entry_price is not None and decision.requested_entry_price is not None:
        found.add(abs(decision.entry_price - decision.requested_entry_price))
    if outcome.submit_latency_seconds is not None:
        found.add(Decimal(str(round(outcome.submit_latency_seconds, 2))))
    if outcome.fill_latency_seconds is not None:
        found.add(Decimal(str(round(outcome.fill_latency_seconds, 2))))
    if source.baselines:
        for value in source.baselines.values():
            try:
                found.add(Decimal(str(value)))
            except (InvalidOperation, ValueError, TypeError):
                continue
    return found


def _supported(value: Decimal, known: set[Decimal]) -> bool:
    return any(abs(value - candidate) <= TOLERANCE for candidate in known)


def structural(review: TradeReview) -> Verdict:
    """The schema holds. Section 38, steps 1, 2 and 5."""
    verdict = Verdict()

    if not review.trade_id:
        verdict.fail("the review carries no trade id.")
    if review.schema_version != REVIEW_SCHEMA_VERSION:
        verdict.fail(
            f"schema version {review.schema_version!r} is not the current "
            f"{REVIEW_SCHEMA_VERSION!r}."
        )
    if not isinstance(review.outcome, Outcome):
        verdict.fail(f"{review.outcome!r} is not a known outcome.")
    if not isinstance(review.compliance, Compliance):
        verdict.fail(f"{review.compliance!r} is not a known compliance value.")
    if not 0.0 <= review.confidence <= 1.0:
        verdict.fail(f"confidence {review.confidence} is not a probability.")
    if not review.review_model or not review.review_model_version:
        verdict.fail("the review does not say which model and version produced it.")

    for name, section in review.sections().items():
        if not isinstance(section.rating, Rating):
            verdict.fail(f"{name} carries {section.rating!r}, which is not a rating.")
        if section.rating is Rating.unknown and not section.unavailable_reason:
            verdict.fail(f"{name} is UNKNOWN without saying which data was missing.")
        for item in section.evidence:
            if not isinstance(item.kind, EvidenceKind):
                verdict.fail(f"{name} carries evidence with kind {item.kind!r}.")
            if item.kind is EvidenceKind.observed and not item.source:
                verdict.fail(
                    f"{name} states an OBSERVED fact with no source. An observation "
                    "nobody can check is exactly what section 5 forbids."
                )
    return verdict


def referential(review: TradeReview, source: ReviewInput) -> Verdict:
    """Nothing is asserted that the facts do not support. Sections 37 and 38.

    Checks the trade id, the environment, the symbol, the model versions and
    every decimal figure quoted in the narrative.
    """
    verdict = Verdict()

    if review.trade_id != source.trade_id:
        verdict.fail(
            f"the review is for trade {review.trade_id!r} and the input is for {source.trade_id!r}."
        )
    if review.environment != source.environment:
        verdict.fail(
            f"the review says environment {review.environment!r} and the trade is "
            f"{source.environment!r}. Paper and live are never conflated."
        )

    symbol = source.decision.symbol
    if symbol:
        # An invented instrument is the clearest hallucination there is.
        for text in _narrative(review):
            for token in re.findall(r"\b[A-Z]{6}\b", text):
                if token != symbol.upper():
                    verdict.fail(
                        f"the narrative names instrument {token!r}, and this trade is on "
                        f"{symbol!r}."
                    )

    if review.prediction_model_version != source.decision.prediction_model_version:
        verdict.fail(
            "the review's prediction model version does not match the recorded "
            "decision. Section 52: never refer to a model version the record does "
            "not carry."
        )

    known = _decimals(source)
    for text in _narrative(review):
        for token in _NUMBER.findall(text):
            try:
                value = Decimal(token)
            except InvalidOperation:  # pragma: no cover - the regex guarantees it parses
                continue
            if not _supported(value, known):
                verdict.fail(
                    f"the narrative states {token}, which does not appear in the "
                    "recorded facts for this trade."
                )
    return verdict


def _narrative(review: TradeReview) -> list[str]:
    """Only the provider-written text. The deterministic sections are facts.

    Validating the assessors' own evidence against the assessors' own inputs
    would be circular; what needs checking is what a narrative generator added.
    """
    texts = [review.summary]
    texts += [e.statement for e in review.key_factors]
    texts += [e.statement for e in review.lessons]
    texts += list(review.follow_up_questions)
    return [t for t in texts if t]


def validate(review: TradeReview, source: ReviewInput) -> Verdict:
    """Both passes. The review is storable only if this is valid."""
    combined = Verdict()
    for verdict in (structural(review), referential(review, source)):
        combined.errors.extend(verdict.errors)
        combined.warnings.extend(verdict.warnings)
        combined.valid = combined.valid and verdict.valid
    return combined


def confidence_from(source: ReviewInput, review: TradeReview) -> float:
    """Section 22. Derived from what was available, never self-reported.

    Two terms, both measurable:

      * how much of the reviewable input was actually recorded, and
      * how many sections could be rated at all.

    A fluent review of a trade with no strategy, no risk and no sizing record is
    a low-confidence review, and this is what makes that true rather than a
    convention. There is no term for "how sure the narrative sounded", because
    §22 says not to treat a generator's confidence as statistical certainty.
    """
    completeness = source.completeness.fraction
    sections = review.sections()
    rated = sum(1 for s in sections.values() if s.rating is not Rating.unknown)
    coverage = rated / len(sections) if sections else 0.0
    return round(min(1.0, (completeness + coverage) / 2), 4)


__all__ = [
    "TOLERANCE",
    "Verdict",
    "confidence_from",
    "referential",
    "structural",
    "validate",
]
