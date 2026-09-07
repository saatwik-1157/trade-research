"""What the AI layer is asked, what it may answer, and under which mode.

The L27 vocabulary, in a neutral module for the reason L22 learned the hard
way: `AiPolicy` was defined in `app/execution/ai.py` and is now needed by
`app/ai/integration.py`, and having `app.ai` import `app.execution` would
reverse the one dependency direction this codebase is careful about. So the
shared words move here and `app/execution/ai.py` re-exports them — the same
extraction L22 did when `AiVerdict` came out of the paper engine, and every
existing caller is unchanged.

**`AiVerdict` deliberately stays in `app/execution/ai.py`.** It is the seat's
return type, and the seat is an execution concept. `AiDecision` — this module's
type — is the AI layer's *own* answer, richer and with no authority: it carries
a probability, a regime, an anomaly score and a latency, and the seat converts
it into the two-valued thing the pipeline acts on. Keeping them separate is
what stops a field being added to `AiDecision` and quietly becoming executable.

**Nothing here can approve, size or place anything.** `AiDecision` has no
quantity, no risk percentage, no account and no order. The strongest value it
can hold is `Decision.accept`, which means "the AI does not object" — and an
unobjecting AI is not an approval, because approval is the risk engine's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class AiIntegrationError(Exception):
    """An AI configuration that cannot be honoured. Refused, never defaulted."""


class AiMode(StrEnum):
    """How much the AI layer is allowed to affect a signal. Section 6.

    Four values and no fifth. There is deliberately no mode in which the AI
    layer *originates* a trade: every mode here takes a strategy signal that
    already exists and does something to it, and the strongest of those things
    is to decline.
    """

    # No inference runs at all. Section 7: the strategy behaves exactly as the
    # deterministic strategy would. This is the baseline every comparison
    # needs, and the fallback when the AI layer is having a bad day.
    disabled = "AI_DISABLED"
    # Inference runs and is recorded. The signal is unchanged. Section 8.
    advisory = "AI_ADVISORY"
    # Inference runs and may REJECT the signal. Section 9.
    filter = "AI_FILTER"
    # Inference runs and produces a score combined with the strategy's own by a
    # named formula. Section 10. Still only able to subtract: the combined
    # score is compared against a threshold and the outcome is accept or
    # reject, never a size.
    scoring = "AI_SCORING"

    @property
    def runs_inference(self) -> bool:
        return self is not AiMode.disabled

    @property
    def can_reject(self) -> bool:
        """Whether a signal can be stopped by the AI layer in this mode."""
        return self in (AiMode.filter, AiMode.scoring)


class AiPolicy(StrEnum):
    """What happens when the AI layer cannot answer. Sections 11 and 28.

    Moved here from `app/execution/ai.py`, which re-exports it. Section 28 is
    emphatic that fallback must never be implicit, so this is a value a caller
    states rather than a default a caller inherits. There is no third option:
    "carry on if it feels safe" is what an explicit policy exists to replace.
    """

    # The AI must answer. No answer means no trade.
    required = "AI_REQUIRED"
    # The AI is advice. No answer means the signal proceeds to the risk engine,
    # which is unchanged and still authoritative.
    optional = "AI_OPTIONAL"


class Decision(StrEnum):
    """What the AI layer concluded about one signal. Section 18.

    `neutral` and `error` are separate on purpose, on the same reasoning
    `SignalType` keeps HOLD apart from NO_SIGNAL: "the AI looked and has no
    objection" and "the AI could not look" are different facts, and a caller
    counting how often the layer answered needs both.
    """

    accept = "ACCEPT"
    reject = "REJECT"
    # Ran, produced a prediction, and this mode does not act on it. What
    # ADVISORY always returns.
    neutral = "NEUTRAL"
    # Could not produce a usable prediction. The failure policy decides what
    # happens next; this value never decides it by itself.
    error = "ERROR"


class ScoringMethod(StrEnum):
    """How a strategy score and an AI score combine. Section 10.

    Named formulas rather than an expression a caller supplies, because §41
    forbids accepting arbitrary code and an arbitrary formula is arbitrary code
    with extra steps. Each is written out in `combine()` and each is monotone in
    both inputs, so a lower AI score can never raise a combined one.
    """

    # The pessimistic one, and the default. A combined score is no better than
    # its weakest component -- which is the only combination rule that cannot
    # let a confident AI rescue a weak strategy signal.
    minimum = "minimum"
    # Both must be present and both matter, weighted by `ai_weight`.
    weighted = "weighted"
    # Probabilistic reading: two independent-ish opinions multiplied. Always
    # lower than either, so it is the strictest of the three.
    product = "product"


def combine(
    method: ScoringMethod, strategy_score: float, ai_score: float, *, ai_weight: float
) -> float:
    """The combined score, by a formula that is written down.

    Every branch is monotone non-decreasing in both arguments, so raising the
    AI score can never lower the combination and lowering it can never raise
    it. That property is what makes a threshold on the result meaningful.
    """
    if not 0.0 <= strategy_score <= 1.0:
        raise AiIntegrationError(f"a strategy score must be in [0, 1], not {strategy_score}")
    if not 0.0 <= ai_score <= 1.0:
        raise AiIntegrationError(f"an AI score must be in [0, 1], not {ai_score}")
    if not 0.0 <= ai_weight <= 1.0:
        raise AiIntegrationError(f"the AI weight must be in [0, 1], not {ai_weight}")

    if method is ScoringMethod.minimum:
        return min(strategy_score, ai_score)
    if method is ScoringMethod.weighted:
        return (1.0 - ai_weight) * strategy_score + ai_weight * ai_score
    return strategy_score * ai_score


@dataclass(frozen=True)
class AiThresholds:
    """The bars a prediction must clear. Section 39, and every one settable.

    None means "not applied", which is different from a permissive value: a
    `maximum_anomaly_score` of 1.0 says "any anomaly is fine" and None says
    "this deployment does not have an anomaly model", and the report should be
    able to tell them apart.
    """

    minimum_probability: float = 0.5
    maximum_anomaly_score: float | None = None
    allowed_regimes: tuple[str, ...] = ()
    minimum_expected_return: float | None = None
    # Section 26. Exceeding it is a failure of the AI layer, handled by the
    # policy -- never a reason to skip the risk engine.
    maximum_latency_ms: float = 2000.0
    # Section 10, for AI_SCORING only.
    scoring_method: ScoringMethod = ScoringMethod.minimum
    ai_weight: float = 0.5
    minimum_combined_score: float = 0.5

    def __post_init__(self) -> None:
        for name in ("minimum_probability", "ai_weight", "minimum_combined_score"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise AiIntegrationError(f"{name} must be between 0 and 1, not {value}")
        if self.maximum_anomaly_score is not None and not 0.0 <= self.maximum_anomaly_score <= 1.0:
            raise AiIntegrationError(
                f"maximum_anomaly_score must be between 0 and 1, not {self.maximum_anomaly_score}"
            )
        if self.maximum_latency_ms <= 0:
            raise AiIntegrationError(
                f"a latency budget must be positive, not {self.maximum_latency_ms}. A budget "
                "of zero would reject every signal, which is a configuration mistake "
                "rather than a policy."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "minimum_probability": self.minimum_probability,
            "maximum_anomaly_score": self.maximum_anomaly_score,
            "allowed_regimes": list(self.allowed_regimes),
            "minimum_expected_return": self.minimum_expected_return,
            "maximum_latency_ms": self.maximum_latency_ms,
            "scoring_method": str(self.scoring_method),
            "ai_weight": self.ai_weight,
            "minimum_combined_score": self.minimum_combined_score,
            "note": (
                "None means the check is not applied, which is not the same as a "
                "permissive value. A deployment with no anomaly model and one that "
                "tolerates any anomaly are different facts."
            ),
        }


@dataclass(frozen=True)
class ModelRequirement:
    """One model a strategy depends on, named exactly. Sections 12 and 23.

    `optional` is the difference between "this strategy cannot run without a
    regime model" and "a regime reading improves the record if one is
    available". §23 asks that each strategy declare its own dependencies rather
    than every strategy being handed every model.

    There is no `latest`: §12 forbids arbitrary model selection, and a strategy
    whose behaviour changes when somebody registers a new version is a strategy
    nobody can reproduce.
    """

    key: str
    version: str
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.key or not self.version:
            raise AiIntegrationError(
                "a model requirement names a key AND a version. There is no 'latest' "
                "option: a strategy whose behaviour changes when someone registers a "
                "new version is a strategy nobody can reproduce."
            )

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "version": self.version, "optional": self.optional}


@dataclass(frozen=True)
class AiStrategyConfig:
    """One strategy's AI configuration. Section 38, explicit throughout.

    The default is `AiMode.disabled` with `AiPolicy.optional`, and that pairing
    is deliberate: a strategy that has not been configured behaves exactly as
    the deterministic strategy does, which is §7's baseline.
    """

    strategy_key: str
    mode: AiMode = AiMode.disabled
    policy: AiPolicy = AiPolicy.optional
    models: tuple[ModelRequirement, ...] = ()
    thresholds: AiThresholds = field(default_factory=AiThresholds)
    feature_version: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_key:
            raise AiIntegrationError("an AI configuration must name the strategy it applies to")
        if self.mode.runs_inference and not self.required():
            raise AiIntegrationError(
                f"mode {self.mode} runs inference but this configuration names no required "
                "model. An AI mode with nothing to ask is a configuration mistake, not a "
                "quiet AI_DISABLED -- if the intent is no AI, say AI_DISABLED."
            )
        seen: set[tuple[str, str]] = set()
        for requirement in self.models:
            identity = (requirement.key, requirement.version)
            if identity in seen:
                raise AiIntegrationError(f"model {requirement.key} v{requirement.version} twice")
            seen.add(identity)

    def required(self) -> tuple[ModelRequirement, ...]:
        return tuple(m for m in self.models if not m.optional)

    def optional_models(self) -> tuple[ModelRequirement, ...]:
        return tuple(m for m in self.models if m.optional)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_key": self.strategy_key,
            "mode": str(self.mode),
            "policy": str(self.policy),
            "required_models": [m.as_dict() for m in self.required()],
            "optional_models": [m.as_dict() for m in self.optional_models()],
            "thresholds": self.thresholds.as_dict(),
            "feature_version": self.feature_version,
            "notes": self.notes,
            "authority": (
                "advisory. No mode here lets the AI layer place, size or approve an "
                "order, raise a risk limit or enable live trading. The strongest thing "
                "it can do is decline a signal the strategy produced."
            ),
        }


@dataclass(frozen=True)
class SignalContext:
    """What the AI layer is allowed to see about a signal. Section 17.

    Deliberately a copy rather than the signal object: the AI layer sees what
    it is *permitted* to use, and a type that carried the whole signal would let
    a future model reach for a field nobody meant it to have.

    **It carries no labels and no future.** §16: at signal time the AI may use
    information available at or before that timestamp. `bars` are the closed
    bars up to and including `bar_time`, which is what the strategy itself saw.
    """

    strategy_key: str
    symbol: str
    timeframe: str
    bar_time: datetime
    side: str
    strategy_version: int | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    strategy_score: float | None = None
    # Closed bars only, oldest first, ending at `bar_time`. Typed loosely so
    # this module stays import-light; the integration service checks the shape.
    bars: tuple[Any, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_key": self.strategy_key,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_time": self.bar_time.isoformat(),
            "side": self.side,
            "entry_price": str(self.entry_price) if self.entry_price is not None else None,
            "stop_loss": str(self.stop_loss) if self.stop_loss is not None else None,
            "take_profit": str(self.take_profit) if self.take_profit is not None else None,
            "strategy_score": self.strategy_score,
            "bars": len(self.bars),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class AiDecision:
    """The AI layer's own answer. Section 18.

    Richer than `AiVerdict` and with no more authority: there is no quantity,
    no risk percentage, no account and no order on this type, and the seat that
    consumes it can only turn `Decision.accept` into "proceed to the risk
    engine".

    **`decision` and `status` are separate.** `status` says whether inference
    ran and produced something usable; `decision` says what the configured mode
    concluded from it. An ERROR status under AI_OPTIONAL still yields an
    `accept` decision, and the report shows both — which is what makes a
    fallback visible rather than indistinguishable from agreement.
    """

    decision: Decision
    mode: AiMode
    policy: AiPolicy
    reason: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = "OK"
    model_key: str | None = None
    model_version: str | None = None
    model_version_id: str | None = None
    feature_version: str | None = None
    probability: float | None = None
    predicted_class: str | None = None
    regime: str | None = None
    anomaly_score: float | None = None
    expected_return: float | None = None
    confidence: float | None = None
    strategy_score: float | None = None
    combined_score: float | None = None
    # Section 26, measured rather than assumed.
    feature_latency_ms: float | None = None
    inference_latency_ms: float | None = None
    total_latency_ms: float | None = None
    # One entry per model consulted, including the ones that refused.
    predictions: tuple[dict[str, Any], ...] = ()
    explanation: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        # Section 25, enforced on the way in rather than trusted. A probability
        # outside [0, 1] is not a confident model, it is a broken one.
        if self.probability is not None and not 0.0 <= self.probability <= 1.0:
            raise AiIntegrationError(
                f"a probability of {self.probability} is not a probability. An out-of-range "
                "output is an inference ERROR, never a very confident prediction."
            )
        if self.anomaly_score is not None and not 0.0 <= self.anomaly_score <= 1.0:
            raise AiIntegrationError(f"an anomaly score of {self.anomaly_score} is out of range")
        if self.decision is Decision.error and self.probability is not None:
            raise AiIntegrationError(
                "an ERROR decision carries no probability. A failed inference that reports "
                "a number is the shape a fabricated result takes."
            )

    @property
    def ran(self) -> bool:
        return self.mode.runs_inference and self.status == "OK"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "mode": str(self.mode),
            "policy": str(self.policy),
            "status": self.status,
            "reason": self.reason,
            "at": self.at.isoformat(),
            "model": (f"{self.model_key} v{self.model_version}" if self.model_key else None),
            "model_key": self.model_key,
            "model_version": self.model_version,
            "model_version_id": self.model_version_id,
            "feature_version": self.feature_version,
            "probability": self.probability,
            "predicted_class": self.predicted_class,
            "regime": self.regime,
            "anomaly_score": self.anomaly_score,
            "expected_return": self.expected_return,
            "confidence": self.confidence,
            "strategy_score": self.strategy_score,
            "combined_score": self.combined_score,
            "latency_ms": {
                "features": self.feature_latency_ms,
                "inference": self.inference_latency_ms,
                "total": self.total_latency_ms,
            },
            "predictions": list(self.predictions),
            "explanation": list(self.explanation),
            "authority": (
                "advisory. This decision cannot place, size or approve an order, raise a "
                "risk limit, or enable live trading. ACCEPT means the AI layer does not "
                "object; the risk engine still decides."
            ),
        }


def disabled_decision(reason: str = "") -> AiDecision:
    """What AI_DISABLED returns, without running anything. Section 7.

    A real decision rather than `None`, so a caller cannot tell "the AI was off"
    apart from "the AI agreed" by accident — the mode is on the record either
    way, and the journal has a row for both.
    """
    return AiDecision(
        decision=Decision.accept,
        mode=AiMode.disabled,
        policy=AiPolicy.optional,
        status="DISABLED",
        reason=reason
        or (
            "AI_DISABLED: no inference ran. The strategy behaves exactly as the "
            "deterministic strategy would, which is the baseline every comparison needs."
        ),
    )
