"""validation_runs: one validation of one candidate, and what it concluded.

A new table rather than more columns on `training_runs`, for a reason section 5
names: a candidate can be validated more than once — new data, changed
thresholds, a re-check after a disputed result — and a column can hold one
answer. Keeping the runs separate is what makes "validate again with a stricter
profit-factor floor and compare" a query rather than an argument.

**A finished run must carry a verdict, and a verdict must carry its report.**
Two CHECK constraints, on the reasoning `ready_is_reproducible` uses on
`datasets`: a row claiming a conclusion with nothing behind it is a claim
nobody can check, and it will be read as a result anyway.

**A PASS here is not a promotion.** There is no foreign key from
`model_versions.status` to this table and nothing in the validation package
writes that column. Section 34: passing validation makes a candidate eligible
for CONSIDERATION by the registry, which is level 28's decision to take, by a
human.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import CreatedMixin, IdMixin, JSONType

# How far a run got. `cancelling` and `cancelled` are both present for the
# reason L25 gives: one says we asked, the other says it happened, and a
# cooperative cancellation needs both to be distinguishable.
VALIDATION_STATUSES = (
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "failed",
    # A run that produced a report. The verdict inside it says what the report
    # concluded; deliberately there is no `passed` status, because a status of
    # `passed` on a *run* is one keystroke from being read as a passed *model*.
    "completed",
)

# What a completed run concluded. BLOCKED is a first-class outcome rather than
# an error: "we could not tell" is a real answer and the one most often lost.
VALIDATION_VERDICTS = ("PASS", "FAIL", "CONDITIONAL", "BLOCKED")


class ValidationRun(IdMixin, CreatedMixin, Base):
    """One validation job: what it was asked to check, and what it found."""

    __tablename__ = "validation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(VALIDATION_STATUSES) + "')", name="status"),
        CheckConstraint(
            "verdict IS NULL OR verdict IN ('" + "','".join(VALIDATION_VERDICTS) + "')",
            name="verdict",
        ),
        # A completed run states a verdict, and a verdict carries the report it
        # came from. Either half alone is a conclusion nobody can audit.
        CheckConstraint(
            "status <> 'completed' OR (verdict IS NOT NULL AND report IS NOT NULL)",
            name="completed_states_its_verdict",
        ),
        CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)",
            name="progress_is_a_fraction",
        ),
        Index("ix_validation_runs_model_version", "model_version_id", "created_at"),
    )

    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which training job produced the candidate, when one did. Nullable: a
    # version registered outside the training service is still validatable, and
    # refusing it would make validation reachable only through one door.
    training_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("training_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dataset_id: Mapped[str | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The data actually validated against, recorded even when the dataset row
    # is later deleted -- a report whose data cannot be named is not re-checkable.
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The thresholds and versions used, and the full report. Both stored:
    # section 25 says a threshold is a choice, and a report read a year later
    # against today's defaults would be read against the wrong bar.
    config: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    report: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # Denormalised counts so a list view does not have to open every report.
    checks_passed: Mapped[int | None] = mapped_column(Numeric(5, 0), nullable=True)
    checks_failed: Mapped[int | None] = mapped_column(Numeric(5, 0), nullable=True)
    checks_warning: Mapped[int | None] = mapped_column(Numeric(5, 0), nullable=True)
    checks_blocked: Mapped[int | None] = mapped_column(Numeric(5, 0), nullable=True)

    validation_engine_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
