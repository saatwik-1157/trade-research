"""Who writes the narrative. Sections 35, 36 and 45.

**There is no LLM in this platform, and this module does not add one.** The
audit found no `openai`, no `anthropic`, no API key setting, no prompt template
and no provider abstraction anywhere in the repository. That is the same finding
L24 recorded about models -- *"No AI model existed anywhere before it"* -- and it
has the same consequence: nothing is preserved, nothing is replaced, and the
honest thing to build is the seat rather than an occupant.

So this module defines:

  * `ReviewProvider` -- the Protocol a narrative generator satisfies. One
    method, because §35 says not to invent abstraction beyond what is needed.
  * `DeterministicProvider` -- the DEFAULT, and not an LLM. It writes the
    summary from the sections `checks.py` already produced.

**The seat is not filled with a stub that pretends.** L27 made this exact choice
about AI_DISABLED: the seat is `None` rather than a filter that accepts
everything, because a permissive stub and a real component that agrees look
identical in the record. Here the equivalent trap would be a provider that
returns plausible prose; instead the default provider returns prose that is
*derived*, says so, and is labelled `deterministic` in `review_model`.

**Nothing leaves this process.** §45 asks that credentials never be sent to an
external provider. Today nothing is sent anywhere at all, and when a real
provider is added the payload it receives is `ReviewInput.as_dict()` -- which is
prices, quantities, identifiers and recorded decisions, and has never held a
secret. A test asserts that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.review.context import ReviewInput
from app.review.contract import (
    PROMPT_VERSION,
    Compliance,
    Evidence,
    Rating,
    TradeReview,
    hypothesis,
    interpreted,
)


class ProviderError(Exception):
    """The narrative generator failed. The trade is unaffected. Section 57."""


class ProviderTimeout(ProviderError):
    """It did not answer in time. Distinct because it is worth retrying."""


@runtime_checkable
class ReviewProvider(Protocol):
    """One method. Section 35: no abstraction beyond what is needed.

    An implementation receives the assembled facts and the deterministic
    sections, and returns the narrative parts ONLY -- a summary, key factors,
    lessons and follow-up questions. It is never asked for a rating, a price or
    a P&L, because §36 assigns those to the deterministic side and §7 says not
    to ask a model to reconstruct facts already in the database.
    """

    name: str
    version: str

    def narrate(self, review: TradeReview, source: ReviewInput) -> Narration: ...


class Narration:
    """What a provider is allowed to return.

    Deliberately narrow. A provider cannot return a rating, an outcome, a price
    or a model version -- there is nowhere to put one, so a hallucinated figure
    has no route into the stored review even before `validation.py` looks at it.
    """

    def __init__(
        self,
        summary: str,
        key_factors: list[Evidence] | None = None,
        lessons: list[Evidence] | None = None,
        follow_up_questions: list[str] | None = None,
    ) -> None:
        self.summary = summary
        self.key_factors = key_factors or []
        self.lessons = lessons or []
        self.follow_up_questions = follow_up_questions or []


class DeterministicProvider:
    """The default. Derives the narrative from the sections; invents nothing.

    Every sentence it produces is assembled from a value the deterministic
    assessors already established, which is why its output passes
    `validation.py` by construction rather than by luck. It exists so the level
    is usable with no external dependency, and so the validator has something
    real to validate.
    """

    name = "deterministic"
    version = "1.0"

    def narrate(self, review: TradeReview, source: ReviewInput) -> Narration:
        decision, outcome = source.decision, source.outcome
        parts: list[str] = [
            f"{decision.symbol or 'the instrument'} {decision.side or ''}".strip()
            + f", {str(review.outcome).lower()}"
            + (
                f" at {outcome.net_profit} {outcome.currency or ''}".rstrip()
                if outcome.net_profit is not None
                else ""
            )
            + "."
        ]
        if outcome.r_multiple is not None:
            parts.append(f"R = {outcome.r_multiple}.")
        if outcome.exit_reason:
            parts.append(f"Closed on {outcome.exit_reason}.")

        rated = {
            name: section.rating
            for name, section in review.sections().items()
            if section.rating is not Rating.unknown
        }
        if rated:
            good = [n for n, r in rated.items() if r is Rating.good]
            poor = [n for n, r in rated.items() if r is Rating.poor]
            if good:
                parts.append(f"Assessed GOOD on {', '.join(sorted(good))}.")
            if poor:
                parts.append(f"Assessed POOR on {', '.join(sorted(poor))}.")
        unrated_names = [
            name for name, section in review.sections().items() if section.rating is Rating.unknown
        ]
        if unrated_names:
            parts.append(f"Not assessable from the record: {', '.join(sorted(unrated_names))}.")

        key_factors: list[Evidence] = []
        for name, section in review.sections().items():
            if section.rating is Rating.poor:
                key_factors.append(
                    interpreted(f"{name.replace('_', ' ')} rated POOR.", f"review.{name}")
                )
            for warning in section.warnings:
                key_factors.append(interpreted(warning, f"review.{name}.warnings"))

        lessons: list[Evidence] = []
        questions: list[str] = []

        # §25. A lesson must connect to project data, so each one names the
        # figure behind it. Generic advice is deliberately absent.
        if outcome.slippage_points:
            worst = max(abs(v) for v in outcome.slippage_points)
            baseline = source.baselines.get("median_slippage_points")
            if baseline is not None:
                lessons.append(
                    interpreted(
                        f"worst fill slippage was {worst} points against a recent median "
                        f"of {baseline}.",
                        "executions.slippage_points and /v1/analytics/execution",
                    )
                )
            else:
                questions.append(
                    "How does this trade's fill slippage compare with the recent median "
                    "for this symbol? Analytics can answer it; this review has no "
                    "baseline to compare against."
                )

        if review.compliance is Compliance.unknown and decision.strategy_version_id is None:
            questions.append(
                "Which strategy produced this trade? Nothing is linked, so compliance "
                "could not be checked."
            )
        if decision.ai_decision is not None and review.ai_context.get("outcome_agreed") is False:
            questions.append(
                "Across how many trades has this model version leaned this way and been "
                "wrong? A single disagreement says nothing; L29 monitoring aggregates it."
            )
        if outcome.mae is None:
            questions.append(
                "How far did price move against this position before it recovered? "
                "MAE needs an intratrade price series this deployment does not store."
            )

        if source.completeness.fraction < 0.5:
            lessons.append(
                hypothesis(
                    f"only {source.completeness.fraction:.0%} of the reviewable fields "
                    "were recorded for this trade, so most of this review is 'not "
                    "assessable' rather than a judgement."
                )
            )

        return Narration(
            summary=" ".join(parts),
            key_factors=key_factors,
            lessons=lessons,
            follow_up_questions=questions,
        )


def apply(review: TradeReview, narration: Narration) -> TradeReview:
    """Fold a narration into a review. The provider touches nothing else."""
    review.summary = narration.summary
    review.key_factors = list(narration.key_factors)
    review.lessons = list(narration.lessons)
    review.follow_up_questions = list(narration.follow_up_questions)
    return review


def describe(provider: ReviewProvider) -> dict[str, Any]:
    return {
        "name": provider.name,
        "version": provider.version,
        "prompt_version": PROMPT_VERSION,
        "external": provider.name != "deterministic",
        "note": (
            "the default provider is DETERMINISTIC and is not a language model. No LLM "
            "provider exists in this platform: the audit found no client, no API key "
            "and no prompt template, and this level added the seat rather than an "
            "occupant. Nothing is sent anywhere."
        ),
    }


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


#: Convenience for a section that a provider must never be able to set.
PROVIDER_MAY_NOT_SET: tuple[str, ...] = (
    "outcome",
    "compliance",
    "confidence",
    "strategy_alignment",
    "entry_quality",
    "exit_quality",
    "risk_quality",
    "execution_quality",
    "market_context",
    "ai_context",
    "review_model",
    "review_model_version",
    "prediction_model",
    "prediction_model_version",
)


__all__ = [
    "PROVIDER_MAY_NOT_SET",
    "DeterministicProvider",
    "Narration",
    "ProviderError",
    "ProviderTimeout",
    "ReviewProvider",
    "apply",
    "describe",
    "utcnow",
]
