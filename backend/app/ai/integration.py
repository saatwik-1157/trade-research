"""Strategy signal in, AI decision out. The one place the AI layer is asked.

Section 24's pipeline, in order:

    1. receive a strategy signal      (a `SignalContext`, never a raw signal)
    2. load the AI configuration      (per strategy, explicit, §38)
    3. resolve model versions         (named exactly; no `latest`, §12)
    4. validate compatibility         (feature version, features, symbol, §13)
    5. prepare features               (L23's engine; there is no second one)
    6. run inference                  (L24's `predict()`; no second contract)
    7. validate the prediction        (§25: a probability outside [0,1] is an
                                       ERROR, never a confident model)
    8. apply the AI policy            (§11: REQUIRED or OPTIONAL, stated)
    9. produce an `AiDecision`
   10. return it

**Never send an order.** Section 24's last line, and it is structural here:
this module imports no order manager, no broker adapter, no risk engine and no
position sizer, and `AiDecision` has no field that could carry a quantity. A
test parses every module in `app/ai/` to keep it that way.

**One feature pipeline.** `app.datasets.features.compute_features` — the same
function that built the training data — is called with the same feature names
the model declares. Section 15 asks that training and inference use compatible
features; the cheapest way to guarantee that is to have one implementation and
no second path into it.

**No future.** The bars handed to this service are closed bars ending at the
signal's own bar time, and `compute_features` is causal by construction (L23
proves it by recomputation with a negative control). Section 16 is satisfied
upstream and re-checked here: a context whose last bar is after `bar_time` is
refused rather than trimmed.

**Failure is recorded, never hidden.** Section 27. Every failure mode — model
unavailable, artifact unreadable, feature mismatch, an out-of-range output, a
latency budget exceeded — produces an `AiDecision` with `status` saying what
happened and `decision` saying what the configured policy made of it. There is
no path that returns `None` and lets a caller decide it meant yes.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from app.ai.base import BaseModel
from app.ai.contract import ModelError, PredictionStatus
from app.ai.decision import (
    AiDecision,
    AiIntegrationError,
    AiMode,
    AiPolicy,
    AiStrategyConfig,
    AiThresholds,
    Decision,
    ModelRequirement,
    ScoringMethod,
    SignalContext,
    combine,
    disabled_decision,
)
from app.ai.registry import ModelRegistry, RegistryError
from app.datasets import features as feature_engine

log = logging.getLogger("app.ai.integration")

# What a model key is taken to mean. Not a registry: a mapping from the three
# families L24 built to the role each plays in a decision, so a deployment that
# registers a fourth family gets a clear refusal rather than a silent misread.
PROBABILITY_KEYS = ("trade_probability",)
REGIME_KEYS = ("regime",)
ANOMALY_KEYS = ("anomaly",)


class Resolver(Protocol):
    """Anything that can turn a `ModelRequirement` into a loaded model.

    A protocol rather than a base class, so the two implementations below share
    a shape without sharing an inheritance chain — and so a test can supply a
    third without importing either. What they must agree on is the refusal:
    every one raises `AiIntegrationError`, which is the only thing the pipeline
    catches, so a resolver that raised something else would escape the failure
    policy rather than be handled by it.
    """

    def resolve(self, requirement: ModelRequirement) -> BaseModel: ...


class ModelResolver:
    """Turns a `ModelRequirement` into a loaded model, or refuses.

    A thin protocol-shaped object rather than a service, so the pipeline can be
    driven from a worker, a backtest or a test without a database session. This
    implementation reads the in-process `ModelRegistry` L24 built, which holds
    models somebody registered deliberately at startup.

    **For a running bot, use `RegistryResolver` instead.** L28 §28 requires the
    AI layer to obtain models through the model registry, so that every model
    serving a trading decision has passed validation, artifact verification and
    a compatibility check. This resolver cannot make those checks -- the
    in-process registry holds objects, not lineage -- so `PaperService` builds
    the registry-backed one and this remains what a backtest or a test uses
    when it supplies its own models.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    def resolve(self, requirement: ModelRequirement) -> BaseModel:
        try:
            return self._registry.get(requirement.key, requirement.version)
        except RegistryError as exc:
            raise AiIntegrationError(str(exc)) from exc


class RegistryResolver:
    """Resolves through the model registry. L28 §28 and §29.

    Every model it returns has been through `resolution.resolve`, which checks
    that the version exists, that its status serves inference, that it names the
    validation run that gated it, that its artifact digest matches, and that its
    feature set and scope are compatible. There is no path here that loads a
    model file, and none that accepts a path.

    **Pre-resolved, deliberately.** The AI pipeline is synchronous and must not
    open a database session per signal (§45), so the caller resolves once when a
    bot starts and hands the resolved models in. That also makes the version a
    bot ran on a FIXED fact for the life of the run rather than something that
    could change under it mid-pass -- which is what §37 and §39 need in order to
    say which model produced a decision.
    """

    def __init__(self, resolved: dict[tuple[str, str], BaseModel], detail: dict[str, Any]) -> None:
        self._resolved = resolved
        #: The resolution behind each entry, so a caller can journal WHICH
        #: version answered rather than only that one did.
        self.detail = detail

    @classmethod
    async def build(
        cls,
        db: Any,
        config: AiStrategyConfig,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        environment: str = "paper",
    ) -> RegistryResolver:
        """Resolve every model a configuration names, once, up front."""
        from app.ai import resolution as model_resolution

        resolved: dict[tuple[str, str], BaseModel] = {}
        detail: dict[str, Any] = {}
        cache = model_resolution.ModelCache()

        for requirement in config.models:
            request = model_resolution.Request(
                model_key=requirement.key,
                strategy_key=config.strategy_key,
                symbol=symbol,
                timeframe=timeframe,
                environment=environment,
            )
            try:
                model, answer = await model_resolution.resolve_and_load(db, request, cache)
            except model_resolution.ResolutionError as exc:
                if requirement.optional:
                    detail[f"{requirement.key}:{requirement.version}"] = {"refused": str(exc)}
                    continue
                raise AiIntegrationError(
                    f"{requirement.key} v{requirement.version}: {exc}"
                ) from exc
            if answer.model_version != requirement.version:
                # The strategy named a version and the registry resolves a
                # different one. Refused rather than substituted: §37 needs the
                # decision to name the model that made it, and quietly serving
                # another version would make every such record wrong.
                raise AiIntegrationError(
                    f"this strategy names {requirement.key} v{requirement.version} and the "
                    f"registry resolves v{answer.model_version} for this scope. Refused "
                    "rather than substituted: a decision must name the model that made it."
                )
            resolved[(requirement.key, requirement.version)] = model
            detail[f"{requirement.key}:{requirement.version}"] = answer.as_dict()

        return cls(resolved, detail)

    def resolve(self, requirement: ModelRequirement) -> BaseModel:
        model = self._resolved.get((requirement.key, requirement.version))
        if model is None:
            raise AiIntegrationError(
                f"{requirement.key} v{requirement.version} was not resolved by the model "
                "registry for this scope. There is no fallback that loads it another "
                "way: §28 requires every model reaching a trading decision to come "
                "through the registry."
            )
        return model


class AiIntegrationService:
    """The deterministic AI pipeline. Produces a decision and nothing else."""

    def __init__(
        self,
        resolver: Resolver,
        *,
        now: Any = None,
    ) -> None:
        self._resolver = resolver
        self._now = now or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------ the entry

    def evaluate(self, context: SignalContext, config: AiStrategyConfig) -> AiDecision:
        """One signal, one decision. Never raises, never places anything."""
        started = time.perf_counter()

        if config.mode is AiMode.disabled:
            # Section 7. Nothing is loaded, nothing is computed, and the
            # strategy behaves exactly as the deterministic strategy would.
            return disabled_decision()

        try:
            return self._evaluate(context, config, started)
        except Exception as exc:  # noqa: BLE001 - recorded as an error, never as agreement
            log.warning(
                "the AI integration pipeline raised",
                extra={
                    "event": "ai_integration_error",
                    "strategy": config.strategy_key,
                    "error": type(exc).__name__,
                },
            )
            return self._failed(
                config,
                context,
                status="PIPELINE_ERROR",
                why=f"{type(exc).__name__}: {exc}"[:300],
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

    # -------------------------------------------------------------- stages

    def _evaluate(
        self, context: SignalContext, config: AiStrategyConfig, started: float
    ) -> AiDecision:
        thresholds = config.thresholds

        # --- 16. no future. Checked here as well as upstream, because "the
        # caller passed the right bars" is an assumption and this is the place
        # it becomes checkable.
        problem = _bars_are_causal(context)
        if problem is not None:
            return self._failed(
                config,
                context,
                status="LOOKAHEAD_REFUSED",
                why=problem,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        # --- 3 & 4. resolve and check compatibility BEFORE computing anything.
        resolved: list[tuple[ModelRequirement, BaseModel]] = []
        for requirement in config.models:
            try:
                model = self._resolver.resolve(requirement)
            except AiIntegrationError as exc:
                if requirement.optional:
                    continue
                return self._failed(
                    config,
                    context,
                    status="MODEL_UNAVAILABLE",
                    why=str(exc),
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )
            incompatible = _compatibility(model, config, context)
            if incompatible is not None:
                if requirement.optional:
                    continue
                return self._failed(
                    config,
                    context,
                    status="INCOMPATIBLE",
                    why=incompatible,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )
            resolved.append((requirement, model))

        if not resolved:
            return self._failed(
                config,
                context,
                status="MODEL_UNAVAILABLE",
                why="no model this strategy declares could be resolved",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        # --- 5. features, once, from the union of what the models declare.
        wanted = tuple(sorted({name for _, model in resolved for name in model.contract.features}))
        feature_started = time.perf_counter()
        try:
            values = _features_at_signal(context, wanted)
        except AiIntegrationError as exc:
            return self._failed(
                config,
                context,
                status="FEATURES_UNAVAILABLE",
                why=str(exc),
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        feature_ms = (time.perf_counter() - feature_started) * 1000.0

        # --- 6 & 7. inference, and the prediction validated on the way out.
        inference_started = time.perf_counter()
        readings = _Readings()
        records: list[dict[str, Any]] = []
        explanation: list[dict[str, Any]] = []

        for requirement, model in resolved:
            try:
                prediction = model.predict(
                    values,
                    at=context.bar_time,
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    feature_version=config.feature_version or feature_engine.FEATURE_SET_VERSION,
                )
            except ModelError as exc:
                # L24's `Prediction` refuses an out-of-range probability in its
                # own constructor, so a broken model usually fails HERE rather
                # than at the check below. Caught by type so the report says
                # INVALID_OUTPUT rather than the vaguer PIPELINE_ERROR: §25
                # asks for the invalid output to be marked, and marking it
                # "something raised" loses which model and which field.
                return self._failed(
                    config,
                    context,
                    status="INVALID_OUTPUT",
                    why=f"{model.identity.key} v{model.identity.version}: {exc}",
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    predictions=records,
                )
            records.append(prediction.as_dict())

            if not prediction.ok:
                if requirement.optional:
                    continue
                return self._failed(
                    config,
                    context,
                    status=str(prediction.status),
                    why=f"{model.identity.key} v{model.identity.version}: {prediction.detail}",
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    predictions=records,
                )

            invalid = _prediction_is_usable(prediction)
            if invalid is not None:
                # Section 25. An out-of-range output is an inference ERROR, and
                # it is never used -- not clamped, not rounded into range.
                return self._failed(
                    config,
                    context,
                    status="INVALID_OUTPUT",
                    why=invalid,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    predictions=records,
                )

            readings.absorb(model, prediction)
            if model.identity.key in PROBABILITY_KEYS:
                explanation = model.explanation(values)

        inference_ms = (time.perf_counter() - inference_started) * 1000.0
        total_ms = (time.perf_counter() - started) * 1000.0

        # --- 26. the latency budget, applied through the failure policy. Never
        # a reason to skip a gate: exceeding it produces a refusal or a
        # fallback, and the risk engine still runs on whatever proceeds.
        if total_ms > thresholds.maximum_latency_ms:
            return self._failed(
                config,
                context,
                status="LATENCY_EXCEEDED",
                why=(
                    f"AI inference took {total_ms:.1f}ms against a budget of "
                    f"{thresholds.maximum_latency_ms:.1f}ms"
                ),
                elapsed_ms=total_ms,
                predictions=records,
            )

        if readings.probability is None and config.mode.can_reject:
            # A mode that can reject needs something to reject on. Reaching
            # here means every resolved model was a regime or anomaly one.
            return self._failed(
                config,
                context,
                status="NO_PROBABILITY",
                why=(
                    f"mode {config.mode} decides on a probability and no resolved model "
                    "produced one. A regime label is not a probability and is not "
                    "substituted for one."
                ),
                elapsed_ms=total_ms,
                predictions=records,
            )

        # --- 8 & 9. the mode decides; the policy has nothing left to decide.
        decision, reason, combined = _apply_mode(config, context, readings, thresholds)

        return AiDecision(
            decision=decision,
            mode=config.mode,
            policy=config.policy,
            reason=reason,
            at=self._now(),
            status="OK",
            model_key=readings.model_key,
            model_version=readings.model_version,
            feature_version=config.feature_version or feature_engine.FEATURE_SET_VERSION,
            probability=readings.probability,
            predicted_class=readings.predicted_class,
            regime=readings.regime,
            anomaly_score=readings.anomaly_score,
            confidence=readings.confidence,
            strategy_score=context.strategy_score,
            combined_score=combined,
            feature_latency_ms=round(feature_ms, 3),
            inference_latency_ms=round(inference_ms, 3),
            total_latency_ms=round(total_ms, 3),
            predictions=tuple(records),
            explanation=tuple(explanation),
        )

    # -------------------------------------------------------------- failure

    def _failed(
        self,
        config: AiStrategyConfig,
        context: SignalContext,
        *,
        status: str,
        why: str,
        elapsed_ms: float,
        predictions: list[dict[str, Any]] | None = None,
    ) -> AiDecision:
        """Section 27 and 28: the failure policy, applied in ONE place.

        Applied here rather than at each call site so it cannot be applied
        twice, and so a new failure mode inherits the configured behaviour
        instead of picking its own.
        """
        if config.policy is AiPolicy.required:
            decision = Decision.reject
            reason = (
                f"AI_REQUIRED and the AI layer could not answer ({why}). No trade. "
                "A model that could not answer is not a model that agreed."
            )
        else:
            decision = Decision.accept
            reason = (
                f"AI_OPTIONAL and the AI layer could not answer ({why}). The signal "
                "proceeds to the risk engine, which is unchanged and still authoritative."
            )
        return AiDecision(
            decision=decision,
            mode=config.mode,
            policy=config.policy,
            reason=reason,
            at=self._now(),
            status=status,
            feature_version=config.feature_version or feature_engine.FEATURE_SET_VERSION,
            strategy_score=context.strategy_score,
            total_latency_ms=round(elapsed_ms, 3),
            predictions=tuple(predictions or ()),
        )


# ================================================================== helpers


class _Readings:
    """What the resolved models said, one field per role."""

    def __init__(self) -> None:
        self.probability: float | None = None
        self.predicted_class: str | None = None
        self.regime: str | None = None
        self.anomaly_score: float | None = None
        self.confidence: float | None = None
        self.model_key: str | None = None
        self.model_version: str | None = None

    def absorb(self, model: BaseModel, prediction: Any) -> None:
        key = model.identity.key
        if key in PROBABILITY_KEYS:
            self.probability = prediction.probability
            self.predicted_class = prediction.value
            self.confidence = prediction.confidence
            # The probability model is the one a decision is attributed to;
            # a regime reading is context, not the answer.
            self.model_key = key
            self.model_version = model.identity.version
        elif key in REGIME_KEYS:
            self.regime = prediction.value
        elif key in ANOMALY_KEYS:
            self.anomaly_score = prediction.confidence
        if self.model_key is None:
            self.model_key = key
            self.model_version = model.identity.version


def _bars_are_causal(context: SignalContext) -> str | None:
    """Section 16. A context reaching into the future is refused, not trimmed.

    Trimming would be the repair §16 exists to prevent: it would turn a caller's
    bug into a silently different evaluation, and the resulting model would look
    fine.
    """
    if not context.bars:
        return "no bars were supplied, so no feature can be computed. Nothing is substituted."
    last = getattr(context.bars[-1], "bar_time", None)
    if last is None:
        return "the supplied bars carry no bar_time, so causality cannot be checked"
    if last > context.bar_time:
        return (
            f"the last supplied bar closes at {last.isoformat()}, after the signal's own "
            f"bar time {context.bar_time.isoformat()}. Refused rather than trimmed: a "
            "silently trimmed window is a look-ahead bug that leaves no trace."
        )
    return None


def _compatibility(
    model: BaseModel, config: AiStrategyConfig, context: SignalContext
) -> str | None:
    """Section 13, checked BEFORE inference. Returns why not, or None."""
    if not model.fitted:
        return (
            f"{model.identity.key} v{model.identity.version} has no fitted parameters. "
            "An unfitted model answers MODEL_UNAVAILABLE rather than a default."
        )
    wanted = config.feature_version or feature_engine.FEATURE_SET_VERSION
    if not model.contract.accepts_version(wanted):
        return (
            f"{model.identity.key} v{model.identity.version} was fitted against feature "
            f"set {model.contract.feature_version} and this strategy supplies {wanted}. "
            "Section 15: a model run on features it was not fitted on is being asked "
            "about a different quantity."
        )
    unknown = [name for name in model.contract.features if name not in feature_engine.CATALOGUE]
    if unknown:
        return (
            f"{model.identity.key} requires {', '.join(unknown)}, which this platform's "
            "feature engine does not compute. Refused rather than filled with nulls."
        )
    return None


def _features_at_signal(context: SignalContext, names: tuple[str, ...]) -> dict[str, float | None]:
    """The feature row for the signal's own bar. One engine, no second path.

    `compute_features` returns one row per bar in the same order, so the row for
    the signal is the last one — and it is the last one because
    `_bars_are_causal` already established that the window ends at `bar_time`.
    """
    rows = feature_engine.compute_features(list(context.bars), names)
    if not rows:
        raise AiIntegrationError("the feature engine produced no rows for this window")
    row = rows[-1]
    missing = sorted(name for name in names if row.get(name) is None)
    if missing:
        warmup = feature_engine.warmup_for(names)
        raise AiIntegrationError(
            f"{', '.join(missing)} could not be computed from {len(context.bars)} bars "
            f"(this feature set needs a {warmup}-bar warm-up). Nothing is substituted: a "
            "feature filled with a default is a number the model will treat as a reading."
        )
    return row


def _prediction_is_usable(prediction: Any) -> str | None:
    """Section 25. Validate what came back before anything acts on it."""
    probability = prediction.probability
    if probability is not None:
        if probability != probability:  # NaN
            return "the model returned NaN as a probability"
        if not 0.0 <= probability <= 1.0:
            return (
                f"the model returned {probability} as a probability. Out of range is an "
                "inference ERROR, never a very confident prediction, and it is not "
                "clamped into range."
            )
    confidence = prediction.confidence
    if confidence is not None and (confidence != confidence or not 0.0 <= confidence <= 1.0):
        return f"the model returned {confidence} as a confidence, which is out of range"
    ok = prediction.status is PredictionStatus.ok
    if ok and prediction.value is None and probability is None:
        return "the model reported OK and returned neither a value nor a probability"
    return None


def _apply_mode(
    config: AiStrategyConfig,
    context: SignalContext,
    readings: _Readings,
    thresholds: AiThresholds,
) -> tuple[Decision, str, float | None]:
    """What the configured mode makes of the readings. Sections 8, 9 and 10."""
    if config.mode is AiMode.advisory:
        # Section 8. The information is recorded and the signal is unchanged.
        return (
            Decision.neutral,
            _describe(readings)
            + " AI_ADVISORY: recorded, and the signal is unchanged. The strategy's own "
            "decision stands.",
            None,
        )

    # Both remaining modes may reject, and both apply the context gates first:
    # a forbidden regime or an anomalous bar stops the signal whatever the
    # probability says, because those describe the market the number came from.
    gate = _context_gates(readings, thresholds)
    if gate is not None:
        return Decision.reject, _describe(readings) + gate, None

    probability = readings.probability
    assert probability is not None  # `_evaluate` refused the alternative

    if config.mode is AiMode.filter:
        if probability < thresholds.minimum_probability:
            return (
                Decision.reject,
                _describe(readings)
                + f"AI_FILTER: probability {probability:.4f} is below the configured "
                f"minimum {thresholds.minimum_probability:.4f}. This is a filter, not a "
                "size -- the risk engine and the sizing engine are unchanged.",
                None,
            )
        return (
            Decision.accept,
            _describe(readings) + f"AI_FILTER: probability {probability:.4f} clears the minimum "
            f"{thresholds.minimum_probability:.4f}. Advisory only; the risk engine still "
            "decides.",
            None,
        )

    # AI_SCORING. Section 10: a named formula, never an invented one.
    strategy_score = context.strategy_score
    if strategy_score is None:
        # A strategy with no score of its own cannot be scored against. Falling
        # back to "use the AI score alone" would silently turn AI_SCORING into
        # a stricter AI_FILTER with a different threshold.
        return (
            Decision.reject,
            _describe(readings)
            + "AI_SCORING: this strategy supplied no score of its own, so there is "
            "nothing to combine. Refused rather than scored on the AI alone, which "
            "would be a different mode wearing this one's name.",
            None,
        )
    combined = combine(
        thresholds.scoring_method,
        strategy_score,
        probability,
        ai_weight=thresholds.ai_weight,
    )
    method = _formula(thresholds.scoring_method, thresholds.ai_weight)
    if combined < thresholds.minimum_combined_score:
        return (
            Decision.reject,
            _describe(readings)
            + f"AI_SCORING: {method} gives {combined:.4f} from strategy {strategy_score:.4f} "
            f"and AI {probability:.4f}, below the configured minimum "
            f"{thresholds.minimum_combined_score:.4f}.",
            combined,
        )
    return (
        Decision.accept,
        _describe(readings) + f"AI_SCORING: {method} gives {combined:.4f}, clearing "
        f"{thresholds.minimum_combined_score:.4f}. The combined score is a filter and "
        "not a size: nothing downstream reads it as risk.",
        combined,
    )


def _context_gates(readings: _Readings, thresholds: AiThresholds) -> str | None:
    """The regime and anomaly gates. Applied before the probability."""
    if thresholds.allowed_regimes and readings.regime is not None:
        if readings.regime not in thresholds.allowed_regimes:
            return (
                f"the regime model reports {readings.regime}, which is not in the "
                f"configured {', '.join(thresholds.allowed_regimes)}. A probability "
                "estimated in one regime is not evidence about another."
            )
    if thresholds.maximum_anomaly_score is not None and readings.anomaly_score is not None:
        if readings.anomaly_score > thresholds.maximum_anomaly_score:
            return (
                f"the anomaly score is {readings.anomaly_score:.4f}, above the configured "
                f"{thresholds.maximum_anomaly_score:.4f}. A rare bar is not a bad bar; "
                "this gate says the model's training window does not describe it."
            )
    return None


def _describe(readings: _Readings) -> str:
    parts: list[str] = []
    if readings.probability is not None:
        parts.append(f"probability {readings.probability:.4f}")
    if readings.regime is not None:
        parts.append(f"regime {readings.regime}")
    if readings.anomaly_score is not None:
        parts.append(f"anomaly {readings.anomaly_score:.4f}")
    return (", ".join(parts) + ". ") if parts else ""


def _formula(method: ScoringMethod, weight: float) -> str:
    if method is ScoringMethod.minimum:
        return "min(strategy, ai)"
    if method is ScoringMethod.weighted:
        return f"{1 - weight:.2f}*strategy + {weight:.2f}*ai"
    return "strategy * ai"
