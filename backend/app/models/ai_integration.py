"""ai_strategy_configs and ai_decisions: how a strategy uses AI, and what it said.

Two tables, and §36 is explicit that existing model, training and registry
tables must not be duplicated — so neither of these stores a model, a version,
a metric or an artifact. They reference them.

**`ai_decisions` is the journal §35 asks for**, and it is the audit trail every
later level reads: the trade journal, the analytics, the AI trade review and
L29's monitoring all need to know what the AI said and what happened next.

**A decision row references its prediction rather than restating it.** L24's
`model_predictions` already records every inference including the refusals, is
idempotent on `prediction_key`, and carries the input digest. Copying the
probability into a second table would create two numbers that can disagree; the
one denormalised copy here (`probability`) exists so a dashboard can aggregate
without a join, and a CHECK keeps it in range.

**Nothing here can execute.** There is no quantity, no price, no account and no
order id on `ai_decisions` — only the id of the order that the *pipeline*
subsequently created, if it created one, which is written after the fact by the
caller that owns it.
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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import CreatedMixin, IdMixin, JSONType, Ratio, TimestampMixin

# Section 6. Four modes and no fifth; there is deliberately no mode in which
# the AI layer originates a trade.
AI_MODES = ("AI_DISABLED", "AI_ADVISORY", "AI_FILTER", "AI_SCORING")

# Section 11. Two, and the absence of a third is the point: "carry on if it
# feels safe" is what an explicit policy exists to replace.
AI_POLICIES = ("AI_REQUIRED", "AI_OPTIONAL")

# Section 18. ACCEPT / REJECT / NEUTRAL / ERROR. NEUTRAL is what AI_ADVISORY
# always records: the layer looked and this mode does not act on what it saw,
# which must not be counted later as agreement.
AI_DECISIONS = ("ACCEPT", "REJECT", "NEUTRAL", "ERROR")

# Section 10. Named formulas, because §41 forbids arbitrary code and an
# arbitrary formula is arbitrary code with extra steps.
SCORING_METHODS = ("minimum", "weighted", "product")


class AiStrategyConfiguration(IdMixin, TimestampMixin, Base):
    """One strategy's AI configuration. Section 38, explicit throughout.

    Keyed by `(strategy_key, account_id)` so one strategy can run advisory on a
    demo account and disabled on another without two strategy rows. A NULL
    account is the default for that strategy.
    """

    __tablename__ = "ai_strategy_configs"
    __table_args__ = (
        UniqueConstraint("strategy_key", "account_id", name="one_config_per_scope"),
        CheckConstraint("mode IN ('" + "','".join(AI_MODES) + "')", name="mode"),
        CheckConstraint("policy IN ('" + "','".join(AI_POLICIES) + "')", name="policy"),
        CheckConstraint(
            "scoring_method IN ('" + "','".join(SCORING_METHODS) + "')", name="scoring_method"
        ),
        # Section 25 and 39, in the schema rather than only in the dataclass.
        CheckConstraint(
            "minimum_probability >= 0 AND minimum_probability <= 1",
            name="minimum_probability_is_a_probability",
        ),
        CheckConstraint(
            "maximum_anomaly_score IS NULL OR "
            "(maximum_anomaly_score >= 0 AND maximum_anomaly_score <= 1)",
            name="maximum_anomaly_score_is_a_score",
        ),
        CheckConstraint("ai_weight >= 0 AND ai_weight <= 1", name="ai_weight_is_a_fraction"),
        CheckConstraint(
            "minimum_combined_score >= 0 AND minimum_combined_score <= 1",
            name="minimum_combined_score_is_a_fraction",
        ),
        # A budget of zero would reject every signal, which is a configuration
        # mistake wearing a policy's clothes.
        CheckConstraint("max_latency_ms > 0", name="latency_budget_is_positive"),
        # Section 7 and 12: a mode that runs inference must name a model. An
        # AI_FILTER with no model would silently behave as AI_DISABLED under
        # AI_OPTIONAL, which is the implicit fallback §28 forbids.
        CheckConstraint(
            "mode = 'AI_DISABLED' OR required_models IS NOT NULL",
            name="an_active_mode_names_a_model",
        ),
    )

    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # NULL means "the default for this strategy". Not a magic account id: an id
    # that means "all of them" is the shape a config gets applied to the wrong
    # account.
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="AI_DISABLED")
    policy: Mapped[str] = mapped_column(String(16), nullable=False, default="AI_OPTIONAL")

    # `[{"key": ..., "version": ...}]`. Versions are exact: §12 forbids
    # `latest`, because a strategy whose behaviour changes when somebody
    # registers a new version is a strategy nobody can reproduce.
    required_models: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    optional_models: Mapped[list | None] = mapped_column(JSONType, nullable=True)

    feature_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    minimum_probability: Mapped[Decimal] = mapped_column(
        Ratio, nullable=False, default=Decimal("0.5")
    )
    maximum_anomaly_score: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    # `["TRENDING_UP", ...]`. Empty or NULL means the gate is not applied.
    allowed_regimes: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    minimum_expected_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    max_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    scoring_method: Mapped[str] = mapped_column(String(16), nullable=False, default="minimum")
    ai_weight: Mapped[Decimal] = mapped_column(Ratio, nullable=False, default=Decimal("0.5"))
    minimum_combined_score: Mapped[Decimal] = mapped_column(
        Ratio, nullable=False, default=Decimal("0.5")
    )

    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AiDecisionRecord(IdMixin, CreatedMixin, Base):
    """One AI decision about one signal, and what happened to that signal.

    Named `AiDecisionRecord` rather than `AiDecision` because
    `app.ai.decision.AiDecision` already owns that name for the computed thing.
    Two `AiDecision` classes in one codebase is an import away from a bug nobody
    reads twice — the same reasoning `DatasetRecord` was named under at L23.
    """

    __tablename__ = "ai_decisions"
    __table_args__ = (
        CheckConstraint("mode IN ('" + "','".join(AI_MODES) + "')", name="mode"),
        CheckConstraint("policy IN ('" + "','".join(AI_POLICIES) + "')", name="policy"),
        CheckConstraint("decision IN ('" + "','".join(AI_DECISIONS) + "')", name="decision"),
        CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="probability_is_a_probability",
        ),
        CheckConstraint(
            "anomaly_score IS NULL OR (anomaly_score >= 0 AND anomaly_score <= 1)",
            name="anomaly_score_is_a_score",
        ),
        # Section 47, in the schema. An ERROR decision that carries a
        # probability is the exact shape a fabricated result takes: the layer
        # failed and a number appeared anyway.
        CheckConstraint(
            "decision <> 'ERROR' OR probability IS NULL",
            name="an_error_carries_no_probability",
        ),
        # A DISABLED run computed nothing, so it cannot have produced a reading.
        CheckConstraint(
            "mode <> 'AI_DISABLED' OR (probability IS NULL AND model_version_id IS NULL)",
            name="a_disabled_run_produced_no_reading",
        ),
        Index("ix_ai_decisions_strategy_at", "strategy_key", "created_at"),
        Index("ix_ai_decisions_decision_at", "decision", "created_at"),
    )

    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    timeframe: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # The close time of the bar the STRATEGY decided on, never "now": two runs
    # over the same data must produce the same row.
    bar_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)

    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The inference itself lives in `model_predictions` (L24). Referenced, not
    # copied: two records of one probability is one too many.
    prediction_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_predictions.id", ondelete="SET NULL"), nullable=True
    )
    model_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    feature_version: Mapped[str | None] = mapped_column(String(16), nullable=True)

    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    policy: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # What the AI layer's own pipeline reported: OK, MODEL_UNAVAILABLE,
    # LATENCY_EXCEEDED, INVALID_OUTPUT and so on. Separate from `decision`,
    # because an ERROR under AI_OPTIONAL still yields ACCEPT and conflating the
    # two is how a fallback becomes indistinguishable from agreement.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OK")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    probability: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    predicted_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    anomaly_score: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    strategy_score: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    combined_score: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)

    # Section 26, measured rather than assumed.
    feature_latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    inference_latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    total_latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)

    # What happened AFTER the AI layer. Written by the caller that owns the
    # pipeline, so "the AI accepted and risk vetoed" is one row rather than a
    # correlation somebody has to reconstruct. Section 35.
    final_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    risk_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    order_id: Mapped[str | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    bot_id: Mapped[str | None] = mapped_column(
        ForeignKey("bots.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # paper | demo | live, so decisions from the simulator and a real account
    # are never pooled by accident. The same rule every execution-side table has.
    mode_of_trading: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Every model consulted, including the ones that refused. Section 45: a
    # failure that leaves no record is indistinguishable from an absence.
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
