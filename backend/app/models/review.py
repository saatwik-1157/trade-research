"""trade_reviews: one reviewed version of one completed trade (L33)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import IdMixin, JSONType, TimestampMixin

#: Section 10. Only states this pipeline can reach; the enum in
#: `app.review.contract` asserts against this tuple on import, so a state the
#: code can produce and the schema rejects cannot exist.
REVIEW_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "RETRYING", "CANCELLED")
REVIEW_STATUS_CHECK = "status IN ('" + "','".join(REVIEW_STATUSES) + "')"

RATINGS = ("GOOD", "FAIR", "POOR", "UNKNOWN")
COMPLIANCE = ("COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "UNKNOWN")
OUTCOMES = ("WIN", "LOSS", "BREAKEVEN", "UNKNOWN")


class TradeReviewRow(IdMixin, TimestampMixin, Base):
    """One review of one trade, at one version. Sections 31, 33 and 44.

    **Versioned, never overwritten.** §33 and §44: a regeneration writes
    version 2 and version 1 remains readable. The unique constraint is on
    `(trade_id, review_version)`, so the same version cannot be written twice --
    which is §11's idempotency as a database guarantee rather than a check the
    worker has to remember.

    **The input snapshot is stored beside the output.** §32: a future reader must
    be able to answer "what data did the AI see?". It carries prices, quantities,
    identifiers and recorded decisions and has never held a credential, so there
    is nothing in it to redact.
    """

    __tablename__ = "trade_reviews"
    __table_args__ = (
        CheckConstraint(REVIEW_STATUS_CHECK, name="status"),
        CheckConstraint("review_version >= 1", name="version_is_positive"),
        # §22. A stored confidence outside [0,1] is not a low confidence, it is
        # a bug, and the schema is where that stops being representable.
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_is_a_probability",
        ),
        # §38. A COMPLETED review must carry the model that produced it;
        # anything else is untraceable output stored as trusted data.
        CheckConstraint(
            "status <> 'COMPLETED' OR (review_model IS NOT NULL "
            "AND review_model_version IS NOT NULL)",
            name="a_completed_review_names_its_model",
        ),
        # §11 and §33. One row per (trade, version).
        UniqueConstraint("trade_id", "review_version", name="one_review_per_version"),
        Index("ix_trade_reviews_trade_status", "trade_id", "status"),
    )

    trade_id: Mapped[str] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: §55. Carried on the review as well as the trade: a review is served and
    #: filtered on its own, and an environment reachable only through a join is
    #: one a query can forget.
    environment: Mapped[str] = mapped_column(String(8), nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")

    #: §11. The request that produced this row, so a redelivered event resolves
    #: to the same review rather than a second one.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    compliance: Mapped[str | None] = mapped_column(String(24), nullable=True)

    #: One JSON block per section, each `{rating, evidence, warnings,
    #: unavailable_reason}`. A block that was not assessed is NULL rather than
    #: an empty object -- the L29 rule: an empty object reads as "measured, and
    #: nothing there".
    strategy_alignment: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    entry_quality: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    exit_quality: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    risk_quality: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    execution_quality: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    market_context: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    ai_context: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    key_factors: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    lessons: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    follow_up_questions: Mapped[list | None] = mapped_column(JSONType, nullable=True)

    #: §22. Derived from data completeness and section coverage, never
    #: self-reported by a narrative generator.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    completeness: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    #: §21. TWO identities. The model that REVIEWED and the model that
    #: PREDICTED are different questions and a single column would answer
    #: neither.
    review_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_model_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prediction_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prediction_model_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: §32. Exactly what the reviewer saw.
    input_snapshot: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    #: §38. What validation said, stored whether it passed or failed.
    validation: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: §58. Why it failed, if it did. Never a credential -- the provider layer
    #: raises typed errors and this stores their message, and no provider in
    #: this platform holds a secret to leak.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trade_id": self.trade_id,
            "environment": self.environment,
            "review_version": self.review_version,
            "status": self.status,
            "summary": self.summary,
            "outcome": self.outcome,
            "compliance": self.compliance,
            "strategy_alignment": self.strategy_alignment,
            "entry_quality": self.entry_quality,
            "exit_quality": self.exit_quality,
            "risk_quality": self.risk_quality,
            "execution_quality": self.execution_quality,
            "market_context": self.market_context,
            "ai_context": self.ai_context,
            "key_factors": self.key_factors or [],
            "warnings": self.warnings or [],
            "lessons": self.lessons or [],
            "follow_up_questions": self.follow_up_questions or [],
            "confidence": self.confidence,
            "completeness": self.completeness,
            "attribution": {
                "review_model": self.review_model,
                "review_model_version": self.review_model_version,
                "prediction_model": self.prediction_model,
                "prediction_model_version": self.prediction_model_version,
            },
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "validation": self.validation,
            "attempts": self.attempts,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


__all__ = ["COMPLIANCE", "OUTCOMES", "RATINGS", "REVIEW_STATUSES", "TradeReviewRow"]
