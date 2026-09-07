"""Models get the metadata that makes a prediction explainable (L24).

Additive only. No column is dropped, no type narrowed, no row rewritten, and no
existing model is touched — there are none, which the L24 audit confirmed:
`models`, `model_versions`, `training_runs` and `model_predictions` have existed
since L05 and hold nothing.

**`model_versions` gains the provenance section 15 and section 33 ask for.**
The table already had `artifact_ref`, `features`, `labels`, `metrics` and
`validation_report_ref`. What was missing is everything needed to answer "what
was this fitted on" without a join: the feature, label, dataset and
preprocessing versions, the dataset's own fingerprint, the code version, the
fitted parameters, and the three chronological periods. A version that cannot
answer that question is a version nobody can reproduce, and reproducing it is
the only way to check a result.

`params` holds the fitted parameters and the training configuration together —
coefficients for a logistic model, quantile cuts for the regime model, medians
and MADs for the anomaly model. In this column rather than a file because they
are small, structured and queryable, and because an artifact on disk is a second
place for a model to live. **No secret goes in it**: a test asserts the JSON
carries no credential-shaped key, and section 46 is the reason.

**`model_predictions` gains `prediction_key`, and it is UNIQUE.** Section 24
requires deterministic inference, so re-running the same model over the same
inputs produces the same prediction id — which makes recording idempotent by
the same mechanism `orders.intent_id` uses. Without it, a retried inference
would look like two events and every distribution built over the table would be
wrong in a way nothing reports.

The other prediction columns are the ones section 11 names and the table did
not carry: the feature and dataset versions the prediction was made under, its
status (so a refusal is recorded as a refusal rather than as a missing row),
the timeframe, the input timestamp, and free metadata.

Revision ID: 0014_model_metadata
Revises: 0013_datasets
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_model_metadata"
down_revision: str | Sequence[str] | None = "0013_datasets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONCOL = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

PREDICTION_STATUSES = (
    "status IN ('OK','INSUFFICIENT_DATA','MODEL_INPUT_ERROR',"
    "'FEATURE_VERSION_MISMATCH','STALE_FEATURES','MODEL_UNAVAILABLE')"
)


def upgrade() -> None:
    # ---- model_versions: what this was fitted on, and with what
    for column in (
        sa.Column("feature_version", sa.String(length=16), nullable=True),
        sa.Column("label_version", sa.String(length=16), nullable=True),
        sa.Column("dataset_version", sa.String(length=16), nullable=True),
        sa.Column("dataset_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("preprocessing_version", sa.String(length=16), nullable=True),
        sa.Column("code_version", sa.String(length=64), nullable=True),
        sa.Column("params", JSONCOL, nullable=True),
        sa.Column("calibration", JSONCOL, nullable=True),
        sa.Column("training_start", sa.DateTime(), nullable=True),
        sa.Column("training_end", sa.DateTime(), nullable=True),
        sa.Column("validation_start", sa.DateTime(), nullable=True),
        sa.Column("validation_end", sa.DateTime(), nullable=True),
        sa.Column("test_start", sa.DateTime(), nullable=True),
        sa.Column("test_end", sa.DateTime(), nullable=True),
    ):
        op.add_column("model_versions", column)

    # A model may only be promoted once it says what it was fitted on. Section
    # 32 forbids silently changing production behaviour, and a promoted version
    # whose provenance is null cannot be compared with the one it replaced.
    op.create_check_constraint(
        "promoted_states_its_provenance",
        "model_versions",
        "status <> 'promoted' OR (feature_version IS NOT NULL AND params IS NOT NULL)",
    )

    # ---- model_predictions: one shape, and idempotent on a deterministic id
    op.add_column(
        "model_predictions",
        sa.Column("prediction_key", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "model_predictions", sa.Column("feature_version", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "model_predictions", sa.Column("dataset_version", sa.String(length=16), nullable=True)
    )
    op.add_column("model_predictions", sa.Column("timeframe", sa.String(length=4), nullable=True))
    op.add_column(
        "model_predictions",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="OK"),
    )
    op.alter_column("model_predictions", "status", server_default=None)
    op.add_column("model_predictions", sa.Column("features_at", sa.DateTime(), nullable=True))
    op.add_column("model_predictions", sa.Column("detail", sa.Text(), nullable=True))
    op.add_column("model_predictions", sa.Column("prediction_metadata", JSONCOL, nullable=True))

    op.create_check_constraint("status", "model_predictions", PREDICTION_STATUSES)
    # A refusal carries no prediction. The same rule `Prediction.__post_init__`
    # enforces in code, enforced again here because a row that reaches the
    # table by another path is still a row somebody will read.
    op.create_check_constraint(
        "refusal_carries_no_value",
        "model_predictions",
        "status = 'OK' OR prediction IS NULL",
    )
    op.create_index(
        "uq_model_predictions_prediction_key",
        "model_predictions",
        ["prediction_key"],
        unique=True,
        postgresql_where=sa.text("prediction_key IS NOT NULL"),
        sqlite_where=sa.text("prediction_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_model_predictions_prediction_key", table_name="model_predictions")
    # The BARE name: the naming convention adds the ck_<table>_ prefix, so a
    # pre-prefixed name becomes ck_model_predictions_ck_model_predictions_...
    # Every migration from 0007 onward passes the bare name for this reason.
    op.drop_constraint("refusal_carries_no_value", "model_predictions", type_="check")
    op.drop_constraint("status", "model_predictions", type_="check")
    for column in (
        "prediction_metadata",
        "detail",
        "features_at",
        "status",
        "timeframe",
        "dataset_version",
        "feature_version",
        "prediction_key",
    ):
        op.drop_column("model_predictions", column)

    op.drop_constraint("promoted_states_its_provenance", "model_versions", type_="check")
    for column in (
        "test_end",
        "test_start",
        "validation_end",
        "validation_start",
        "training_end",
        "training_start",
        "calibration",
        "params",
        "code_version",
        "preprocessing_version",
        "dataset_fingerprint",
        "dataset_version",
        "label_version",
        "feature_version",
    ):
        op.drop_column("model_versions", column)
