"""models, model_versions, training_runs, model_predictions.

The tables have existed since L05 and held nothing until L24, which added the
provenance a prediction needs to be explainable: which feature set, label set
and dataset a version was fitted against, the fitted parameters themselves, and
the three chronological periods.

**A promoted version must say what it was fitted on.** The check constraint is
the point: an unprovenanced draft is fine, and an unprovenanced *production*
model cannot be compared with the one it replaced, which is what section 32's
"no silent replacement" needs to be checkable.

**A prediction is idempotent on `prediction_key`.** Inference is deterministic,
so re-running the same model over the same inputs yields the same id -- the same
mechanism `orders.intent_id` uses, and for the same reason: without it a retry
looks like a second event and every distribution over the table is wrong.

**A refusal carries no prediction**, in the database as well as in
`Prediction.__post_init__`. A row that reaches the table by another path is
still a row somebody will read.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import (
    CreatedMixin,
    IdMixin,
    JSONType,
    NullableJSONType,
    Ratio,
    TimestampMixin,
)


class AIModel(IdMixin, TimestampMixin, Base):
    __tablename__ = "models"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('identity','classifier','regressor','llm_agent','other')", name="kind"
        ),
    )

    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# The lifecycle vocabulary, as the database sees it. `app/ai/lifecycle.py` owns
# the transitions; this is the set of values the column may hold, and the two
# are asserted equal by a test so neither can gain a value the other lacks.
#
# The first four have been here since L05. L28 adds `registered`, `paper`,
# `rejected` and `rolled_back` -- and deliberately does NOT add `validating`,
# `failed` or `active`; `app/ai/lifecycle.DECLINED` records why for each.
VERSION_STATUSES = (
    "draft",
    "validated",
    "registered",
    "paper",
    "promoted",
    "rejected",
    "rolled_back",
    "retired",
)


class ModelVersion(IdMixin, CreatedMixin, Base):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("model_id", "version", name="model_version"),
        CheckConstraint("status IN ('" + "','".join(VERSION_STATUSES) + "')", name="status"),
        CheckConstraint(
            "status <> 'promoted' OR (feature_version IS NOT NULL AND params IS NOT NULL)",
            name="promoted_states_its_provenance",
        ),
        # L28 §7. A version that has been accepted by the registry must carry
        # the digest that makes its artifact checkable. Without one, "verified"
        # and "never verified" are indistinguishable, and the second would be
        # read as the first.
        CheckConstraint(
            "status IN ('draft','validated','rejected') OR artifact_sha256 IS NOT NULL",
            name="a_registered_version_carries_its_digest",
        ),
        # L28 §8. Registration is gated on validation, and the run that gated it
        # is named. A `registered` version whose decisive run cannot be
        # identified is a claim nobody can re-check.
        CheckConstraint(
            "status IN ('draft','validated','rejected') OR validation_run_id IS NOT NULL",
            name="a_registered_version_names_its_validation",
        ),
    )

    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(String(260), nullable=True)
    features: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    labels: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    validation_report_ref: Mapped[str | None] = mapped_column(String(260), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    # Provenance (L24). Nullable because a draft may not have been fitted yet;
    # the check constraint requires them once a version is promoted.
    feature_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    label_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dataset_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preprocessing_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    code_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fitted parameters and the training configuration, together. Small,
    # structured and queryable -- and holding no secret, which a test asserts.
    params: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    calibration: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    training_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    training_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validation_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validation_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    test_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    test_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- L28: artifact integrity (§7) ------------------------------------
    #
    # The artifact is the JSON in `params["artifact"]`; these describe it. The
    # digest is taken over the canonical (sorted-key, no-whitespace) encoding of
    # that sub-object ALONE, not the whole row: metrics, status and timestamps
    # change legitimately over a version's life and the fitted parameters do
    # not, so hashing the row would make the check fire on every ordinary write
    # and be switched off within a week.
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # `logistic` | `quantile_cuts` | `robust_baseline`. Denormalised from the
    # artifact so a list view can show it without parsing every row.
    artifact_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    framework: Mapped[str | None] = mapped_column(String(32), nullable=True)
    framework_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- L28: lineage (§8) -----------------------------------------------
    #
    # `training_runs.model_version_id` and `validation_runs.model_version_id`
    # already point the other way, so these are not a second copy of the same
    # edge: they name the run this registration RELIED ON. A version can be
    # validated several times, and "which run gated this" is a distinct fact
    # from "which runs touched it".
    training_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    validation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # --- L28: scope (§15) -------------------------------------------------
    #
    # What this version was fitted FOR. NULL means unrestricted, which is not
    # the same as "any symbol is fine" -- it means nobody recorded a restriction,
    # and the resolver says so rather than assuming.
    symbol_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timeframe_scope: Mapped[str | None] = mapped_column(String(4), nullable=True)

    # --- L28: lifecycle timestamps (§5) -----------------------------------
    registered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


TRAINING_STATUSES = (
    "queued",
    "running",
    # Asked to stop, not yet stopped. The same distinction L19 draws between
    # `cancel_requested` and `cancelled`: one says we asked, the other says it
    # happened, and a cooperative cancellation needs both.
    "cancelling",
    "cancelled",
    "failed",
    "finished",
    # Where a SUCCESSFUL run ends. Section 25: "candidate model successfully
    # trained" is not "model approved for trading", so there is deliberately no
    # `completed` that could be read as the second.
    "validation_pending",
)


class TrainingRun(IdMixin, CreatedMixin, Base):
    """One training job: what it was asked to fit, and how far it got."""

    __tablename__ = "training_runs"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(TRAINING_STATUSES) + "')", name="status"),
        # A job claiming a candidate exists must name it. Without this a
        # `validation_pending` row with no model version is a claim nobody can
        # check -- the same shape as `ready_is_reproducible` on `datasets`.
        CheckConstraint(
            "status <> 'validation_pending' OR model_version_id IS NOT NULL",
            name="pending_validation_names_its_candidate",
        ),
        CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)",
            name="progress_is_a_fraction",
        ),
    )

    # NULLABLE since L25: a job is created when it is QUEUED, and the candidate
    # does not exist until the fit finishes. The old NOT NULL made a queued row
    # impossible to write without inventing a model version first.
    model_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    dataset_ref: Mapped[str | None] = mapped_column(String(260), nullable=True)
    params: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # String(32): `validation_pending` is 18 characters and this column was
    # String(16). Same class of defect as `positions.status`, same cause.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    log_ref: Mapped[str | None] = mapped_column(String(260), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # L25. Which family and candidate version, what data was LOCKED, and how
    # far the run got -- so a job can be described without loading its params.
    model_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Indexed, not unique: section 38 wants a duplicate RUNNING job refused,
    # which the service does by querying active rows. A unique index would also
    # forbid re-running a recipe after it finished, which is legitimate.
    config_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Derived from the stage rather than reported separately: two numbers that
    # can disagree is one number too many.
    progress: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    random_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


PREDICTION_STATUSES = (
    "OK",
    "INSUFFICIENT_DATA",
    "MODEL_INPUT_ERROR",
    "FEATURE_VERSION_MISMATCH",
    "STALE_FEATURES",
    "MODEL_UNAVAILABLE",
)


class ModelPrediction(IdMixin, Base):
    __tablename__ = "model_predictions"
    __table_args__ = (
        CheckConstraint("status IN ('" + "','".join(PREDICTION_STATUSES) + "')", name="status"),
        CheckConstraint("status = 'OK' OR prediction IS NULL", name="refusal_carries_no_value"),
        Index(
            "uq_model_predictions_prediction_key",
            "prediction_key",
            unique=True,
            postgresql_where=text("prediction_key IS NOT NULL"),
            sqlite_where=text("prediction_key IS NOT NULL"),
        ),
    )

    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol_id: Mapped[str | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    predicted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    horizon: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # NullableJSONType, not JSONType: `refusal_carries_no_value` tests this
    # column with `IS NULL`, and the default JSON type would store a refusal's
    # None as the JSON literal `null` -- which is not SQL NULL and passes the
    # check for the wrong reason, or fails it for a confusing one.
    prediction: Mapped[dict | None] = mapped_column(NullableJSONType, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    realized: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    realized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # L24. `prediction_key` is `Prediction.prediction_id()`: deterministic, so
    # recording the same inference twice is one row rather than two events.
    prediction_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feature_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dataset_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # A refusal is recorded as a refusal. A missing row and a MODEL_UNAVAILABLE
    # look identical from a count, and only one of them means the model was asked.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OK")
    features_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    prediction_metadata: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
