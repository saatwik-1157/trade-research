"""The AI seat: what a model may say, and the shape of the thing that says it.

Moved here from `app/paper/engine.py` at L22, which re-exports both names so
every existing caller and test is unchanged.

**Why it moved.** It was defined in the paper engine and imported by
`app/execution/pipeline.py`, which the paper engine imports from -- a cycle
that only fired when `app.paper.service` happened to be the first module
imported, so the test suite's import order hid it. The seat is not a paper
concept: the orchestrator uses it, a demo bot would use it, and a module that
two pipelines depend on cannot live inside one of them.

**The seat can only subtract.** `AiVerdict` has no field by which a model
could raise a limit, approve an order, size a position or disengage a kill
switch, because those are not things it is allowed to do. That is the design,
not a limitation of the current stub: a richer model later still returns this
type, and this type cannot express an approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

# Moved to `app/ai/decision.py` at L27 and re-exported here, so every existing
# caller and test is unchanged. The move was forced by direction: L27's
# integration service lives in `app.ai` and needs the policy, and having
# `app.ai` import `app.execution` would reverse the one dependency rule this
# codebase keeps. The same extraction L22 did for `AiVerdict`.
from app.ai.decision import AiDecision as AiDecision
from app.ai.decision import AiMode as AiMode
from app.ai.decision import AiPolicy as AiPolicy
from app.ai.decision import AiStrategyConfig as AiStrategyConfig
from app.ai.decision import Decision as Decision
from app.ai.decision import SignalContext as SignalContext

log = logging.getLogger("app.execution.ai")


@dataclass(frozen=True)
class AiVerdict:
    """What an AI filter may say. It may lower confidence or refuse.

    There is no field by which it could raise a limit, approve an order or
    disengage a kill switch, because those are not things it is allowed to do.
    """

    accept: bool
    confidence: Decimal | None = None
    reason: str = ""
    model: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "accept": self.accept,
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "reason": self.reason,
            "model": self.model,
        }


class AiFilter(Protocol):
    """The seat the AI layer will occupy at L24/L27.

    It sees a signal and returns a verdict. It does not see the risk engine,
    the kill switches, the account or the OMS, so the strongest thing it can
    do is decline -- which is the design, not a limitation of the stub.

    The signal is typed loosely on purpose: a strategy signal and an
    externally-arriving one are different shapes, and both pipelines consult
    the same seat.
    """

    def score(self, signal: object) -> AiVerdict: ...


# ============================================ the policy, and the model seat
#
# Added at L24. These live here rather than in `app/ai/` on purpose: the seat
# is an execution concept, and having `app.execution` depend on `app.ai` -- and
# never the reverse -- keeps the dependency one-way. The reverse is exactly the
# cycle L22 had to break.


@dataclass(frozen=True)
class AiGate:
    """The policy plus the bar a probability must clear.

    `minimum_probability` is a filter, not a size. Section 29: the AI says
    0.76 and the sizing engine -- which this type cannot reach -- decides the
    quantity. Nothing here scales anything.
    """

    policy: AiPolicy = AiPolicy.optional
    minimum_probability: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_probability <= 1.0:
            raise ValueError(
                f"minimum_probability must be between 0 and 1, not {self.minimum_probability}"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": str(self.policy),
            "minimum_probability": self.minimum_probability,
            "on_no_answer": (
                "no trade" if self.policy is AiPolicy.required else "proceed to the risk engine"
            ),
        }


class ModelBackedFilter:
    """An `AiFilter` that consults a probability model.

    It occupies the seat and inherits the seat's limits: `AiVerdict` has no
    field by which it could approve, size or raise anything, so the strongest
    outcome here is `accept=False`.

    **A model that cannot answer is not a model that said yes.** Under
    `AI_REQUIRED` an unavailable, mismatched, stale or input-starved model
    declines, and the reason travels with the verdict. Under `AI_OPTIONAL` the
    signal proceeds -- to the risk engine, which is unchanged and still the
    thing that can refuse it.
    """

    def __init__(
        self,
        model,  # noqa: ANN001 - app.ai.BaseModel; typed loosely to keep this module import-light
        gate: AiGate,
        *,
        feature_version: str,
        features_for,  # noqa: ANN001 - callable(signal) -> (features, at, symbol, timeframe, features_at)
    ) -> None:
        self._model = model
        self._gate = gate
        self._feature_version = feature_version
        self._features_for = features_for

    def score(self, signal: object) -> AiVerdict:
        name = getattr(self._model, "identity", None)
        model_name = f"{name.key} v{name.version}" if name is not None else "unknown"

        try:
            features, at, symbol, timeframe, features_at = self._features_for(signal)
        except Exception as exc:  # noqa: BLE001 - a feature build that raises is "no answer"
            return self._no_answer(model_name, f"features could not be built: {exc}")

        prediction = self._model.predict(
            features,
            at=at,
            symbol=symbol,
            timeframe=timeframe,
            feature_version=self._feature_version,
            features_at=features_at,
        )

        if not prediction.ok:
            return self._no_answer(model_name, f"{prediction.status}: {prediction.detail}")

        probability = prediction.probability
        if probability is None:
            return self._no_answer(model_name, "the model answered without a probability")

        if probability < self._gate.minimum_probability:
            return AiVerdict(
                accept=False,
                confidence=Decimal(str(prediction.confidence or 0.0)),
                reason=(
                    f"probability {probability:.4f} is below the configured minimum "
                    f"{self._gate.minimum_probability:.4f}. This is a filter, not a size: "
                    "the risk engine and the sizing engine are unchanged."
                ),
                model=model_name,
            )

        return AiVerdict(
            accept=True,
            confidence=Decimal(str(prediction.confidence or 0.0)),
            reason=(
                f"probability {probability:.4f} clears the minimum "
                f"{self._gate.minimum_probability:.4f}. Advisory only; the risk engine "
                "still decides."
            ),
            model=model_name,
        )

    def _no_answer(self, model_name: str, why: str) -> AiVerdict:
        """The one place the policy is applied, so it cannot be applied twice."""
        if self._gate.policy is AiPolicy.required:
            return AiVerdict(
                accept=False,
                reason=(
                    f"AI_REQUIRED and the model gave no answer ({why}). No trade. A model "
                    "that could not answer is not a model that agreed."
                ),
                model=model_name,
            )
        return AiVerdict(
            accept=True,
            reason=(
                f"AI_OPTIONAL and the model gave no answer ({why}). The signal proceeds "
                "to the risk engine, which is unchanged and still authoritative."
            ),
            model=model_name,
        )


# ================================================ the L27 seat, mode-aware
#
# `ModelBackedFilter` above is L24's: one model, one probability, one gate. It
# is KEPT because it works and several tests describe it. `IntegrationFilter`
# is the richer seat L27 needs — several models, four modes, a latency budget
# and a journal — and it occupies the SAME seat and inherits the same limits:
# `AiVerdict` still has no field by which anything here could approve, size or
# raise anything.


class IntegrationFilter:
    """An `AiFilter` backed by the L27 integration service.

    The one adapter between `AiDecision` (what the AI layer concluded, rich)
    and `AiVerdict` (what the pipeline acts on, two-valued). Keeping the
    conversion in one function is what stops a field being added to
    `AiDecision` and quietly becoming executable: this method reads exactly one
    of its fields to decide, and everything else travels as narrative.

    **The mode is honoured here and nowhere else.** ADVISORY returns
    `accept=True` whatever the probability was, which is §8; FILTER and SCORING
    return what the service concluded; DISABLED never reaches the service at
    all. A caller cannot get a different answer by reading the decision itself,
    because the decision does not have an `accept` field.

    `context_for` is supplied by whoever owns the bars — the paper engine, the
    orchestrator, the backtest runner. This class does not fetch market data,
    which is why it cannot accidentally fetch a bar from the future.
    """

    def __init__(
        self,
        service: object,  # noqa: ANN001 - app.ai.integration.AiIntegrationService
        config: AiStrategyConfig,
        *,
        context_for,  # noqa: ANN001 - callable(signal) -> SignalContext
        journal=None,  # noqa: ANN001 - callable(AiDecision, SignalContext) -> None
    ) -> None:
        self._service = service
        self._config = config
        self._context_for = context_for
        self._journal = journal
        #: Every decision this filter made with the context it was made on,
        #: newest last. ONE list holding both, rather than two that a caller
        #: would have to keep aligned -- the pairing is what a journal row
        #: needs, and two lists is one opportunity too many to mismatch them.
        #: Drained by whoever owns the run: the paper service writes them to
        #: `ai_decisions`, a backtest counts them.
        self.decisions: list[tuple[AiDecision, SignalContext | None]] = []

    @property
    def config(self) -> AiStrategyConfig:
        return self._config

    def score(self, signal: object) -> AiVerdict:
        model_name = f"{self._config.strategy_key}:{self._config.mode}"

        try:
            # A caller that already built the normalized context -- the paper
            # engine and the orchestrator both do -- passes it straight through.
            # `context_for` exists for callers holding a raw signal, and there
            # is deliberately no path that builds one from market data here:
            # a seat that could fetch a bar could fetch tomorrow's.
            context = signal if isinstance(signal, SignalContext) else self._context_for(signal)
        except Exception as exc:  # noqa: BLE001 - a context that cannot be built is "no answer"
            # Deliberately routed through the service's own failure policy
            # rather than decided here, so AI_REQUIRED and AI_OPTIONAL behave
            # the same for every failure mode.
            decision = AiDecision(
                decision=(
                    Decision.reject if self._config.policy is AiPolicy.required else Decision.accept
                ),
                mode=self._config.mode,
                policy=self._config.policy,
                status="CONTEXT_ERROR",
                reason=(
                    f"the signal context could not be built ({type(exc).__name__}: {exc}). "
                    + (
                        "AI_REQUIRED: no trade."
                        if self._config.policy is AiPolicy.required
                        else "AI_OPTIONAL: the signal proceeds to the risk engine."
                    )
                ),
            )
            return self._verdict(decision, None, model_name)

        decision = self._service.evaluate(context, self._config)  # type: ignore[attr-defined]
        return self._verdict(decision, context, model_name)

    def _verdict(
        self, decision: AiDecision, context: SignalContext | None, model_name: str
    ) -> AiVerdict:
        self.decisions.append((decision, context))
        if self._journal is not None:
            try:
                self._journal(decision, context)
            except Exception:  # noqa: BLE001 - a trade does not fail because a log did
                log.warning(
                    "an AI decision could not be journalled",
                    extra={"event": "ai_journal_failed", "strategy": self._config.strategy_key},
                )

        # NEUTRAL is an acceptance at the seat and is NOT recorded as one: §8
        # says advisory mode leaves the signal unchanged, and the decision on
        # the record still says NEUTRAL so nobody later counts it as agreement.
        accept = decision.decision is not Decision.reject
        confidence = Decimal(str(decision.confidence)) if decision.confidence is not None else None
        return AiVerdict(
            accept=accept,
            confidence=confidence,
            reason=decision.reason,
            model=(
                f"{decision.model_key} v{decision.model_version}"
                if decision.model_key
                else model_name
            ),
        )
