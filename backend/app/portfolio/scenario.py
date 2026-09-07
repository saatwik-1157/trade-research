"""What could happen, and what that is allowed to change. **L66.**

**One rule carries this module: a prediction may tighten and may never loosen.**

The asymmetry is not caution for its own sake, it is the cost function. A
scenario that wrongly predicts danger costs a missed opportunity. A scenario
that wrongly predicts safety costs the portfolio. Those are not the same
mistake, so they must not have the same authority -- and the way that gets lost
is a gate that returns a verdict computed symmetrically from a forecast.

So `gate()` takes the verdict the platform already holds and can only return
something **at least as restrictive**. A scenario predicting calm cannot
upgrade a RESTRICT to a PASS; it can only decline to add anything. This is
L61's "autonomy may take permissions away and never grant them" applied to
forecasting, and it makes section 47's rule -- prediction must never become
direct execution -- a property of the return type rather than a policy somebody
enforces.

**It is not a RiskEngine.** Section 32 says so explicitly and it is worth
repeating here: the gate decides whether a proposed adaptation is worth putting
in front of the real safety chain, not whether it is safe. The RiskEngine
remains the final veto, the OMS owns order state, and nothing here reaches
either -- asserted by a test.

**On conflict, the conservative side wins.** Section 16: where a model and a
deterministic baseline disagree, `conservative_of` returns the more restrictive
of the two. A model is never consulted to relax what a baseline already said.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any


class ScenarioKind(StrEnum):
    """L66 section 4."""

    HISTORICAL = "HISTORICAL_SCENARIO"
    HYPOTHETICAL = "HYPOTHETICAL_SCENARIO"
    STRESS = "STRESS_SCENARIO"
    COUNTERFACTUAL = "COUNTERFACTUAL_SCENARIO"
    REGIME = "REGIME_SCENARIO"
    LIQUIDITY = "LIQUIDITY_SCENARIO"
    CORRELATION = "CORRELATION_SCENARIO"
    VOLATILITY = "VOLATILITY_SCENARIO"
    EXECUTION = "EXECUTION_SCENARIO"
    CONCENTRATION = "PORTFOLIO_CONCENTRATION_SCENARIO"
    STRATEGY_FAILURE = "STRATEGY_FAILURE_SCENARIO"
    MODEL_FAILURE = "MODEL_FAILURE_SCENARIO"
    BROKER_FAILURE = "BROKER_FAILURE_SCENARIO"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE_SCENARIO"


class Severity(IntEnum):
    """L66 section 19. How bad the scenario is, not how likely.

    `EXTREME` is a stress test, not a forecast -- section 19 says so, and the
    distinction matters because an extreme scenario appearing in a ranking
    beside a likely one invites reading it as a prediction.
    """

    EXPECTED = 0
    ADVERSE = 1
    SEVERE = 2
    EXTREME = 3


class Likelihood(IntEnum):
    """Deliberately coarse. A finer scale would imply a precision that a
    platform with no calibration history cannot support."""

    UNLIKELY = 0
    POSSIBLE = 1
    LIKELY = 2


class RiskClass(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


#: The risk matrix. Section 21.
#:
#: Read the UNLIKELY column. An unlikely EXTREME scenario is HIGH, not LOW --
#: section 20 says low-likelihood/high-severity events must remain visible, and
#: multiplying likelihood by severity is exactly what makes them disappear. The
#: matrix is written out rather than computed for that reason: a product would
#: have been shorter and would have buried the row this exists to protect.
MATRIX: dict[tuple[Likelihood, Severity], RiskClass] = {
    (Likelihood.UNLIKELY, Severity.EXPECTED): RiskClass.LOW,
    (Likelihood.UNLIKELY, Severity.ADVERSE): RiskClass.LOW,
    (Likelihood.UNLIKELY, Severity.SEVERE): RiskClass.MEDIUM,
    (Likelihood.UNLIKELY, Severity.EXTREME): RiskClass.HIGH,
    (Likelihood.POSSIBLE, Severity.EXPECTED): RiskClass.LOW,
    (Likelihood.POSSIBLE, Severity.ADVERSE): RiskClass.MEDIUM,
    (Likelihood.POSSIBLE, Severity.SEVERE): RiskClass.HIGH,
    (Likelihood.POSSIBLE, Severity.EXTREME): RiskClass.CRITICAL,
    (Likelihood.LIKELY, Severity.EXPECTED): RiskClass.MEDIUM,
    (Likelihood.LIKELY, Severity.ADVERSE): RiskClass.HIGH,
    (Likelihood.LIKELY, Severity.SEVERE): RiskClass.CRITICAL,
    (Likelihood.LIKELY, Severity.EXTREME): RiskClass.CRITICAL,
}


def classify(likelihood: Likelihood, severity: Severity) -> RiskClass:
    """The matrix, with no fallback. An unmapped pair is a bug, not a LOW."""
    return MATRIX[(likelihood, severity)]


class GateVerdict(IntEnum):
    """L66 section 32. Ordered so `max()` is "the more restrictive"."""

    PASS = 0
    WARN = 1
    RESTRICT = 2
    DEFER = 3
    REJECT = 4


#: How far a hypothetical input may be moved. Section 6: no arbitrary unsafe
#: values.
#:
#: Configuration decisions, not measurements. Nothing has ever been stressed on
#: this platform, so there is no observed distribution these were fitted to.
#: Recorded in `SCENARIO_DEFINITION_POLICY.md` as requiring approval.
BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "volatility_pct": (Decimal("-50"), Decimal("500")),
    "correlation_pct": (Decimal("-100"), Decimal("100")),
    "spread_pct": (Decimal("0"), Decimal("1000")),
    "liquidity_pct": (Decimal("-95"), Decimal("100")),
    "drawdown_pct": (Decimal("0"), Decimal("100")),
    "margin_pct": (Decimal("-95"), Decimal("100")),
    "slippage_pct": (Decimal("0"), Decimal("1000")),
    "latency_ms": (Decimal("0"), Decimal("60000")),
}


class Freshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ScenarioDefinition:
    """L66 section 7. What is being asked, and against what."""

    scenario_id: str
    name: str
    kind: ScenarioKind
    severity: Severity
    likelihood: Likelihood
    account_id: str
    environment: str = "paper"
    inputs: dict[str, Decimal] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()
    horizon: timedelta = timedelta(days=1)
    data_version: str = ""
    model_version: str = ""
    policy_version: str = ""
    seed: int = 0

    def risk_class(self) -> RiskClass:
        return classify(self.likelihood, self.severity)

    def fingerprint(self) -> str:
        """Section 38. The same question asked twice is one scenario.

        Excludes the id and the name -- two definitions differing only in what
        they are called are not two scenarios. Includes every version, because
        the same inputs against different data, models or policy have not
        actually been run.
        """
        body = "\x1f".join(f"{k}={self.inputs[k]}" for k in sorted(self.inputs))
        material = "\x1e".join(
            (
                str(self.kind),
                self.account_id,
                self.environment,
                body,
                self.data_version,
                self.model_version,
                self.policy_version,
                str(self.seed),
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Validation:
    ok: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": list(self.violations)}


def validate(definition: ScenarioDefinition, *, seen: dict[str, str] | None = None) -> Validation:
    """Section 8. Every check runs; a scenario is refused for every reason.

    The environment check is the one that would be easy to leave out: a
    scenario is a simulation, so it looks harmless to run one against `live`.
    It is refused, because a simulation naming a live account is a simulation
    somebody will eventually read as being about live behaviour.
    """
    violations: list[str] = []

    if definition.environment == "live":
        violations.append(
            "a scenario is never defined against a live environment. Simulation is "
            "isolated from live by construction, and a scenario naming a live account "
            "is one somebody will eventually read as a statement about live behaviour"
        )

    for name, value in sorted(definition.inputs.items()):
        bound = BOUNDS.get(name)
        if bound is None:
            violations.append(
                f"{name} is not a recognised scenario input. Unrecognised inputs are "
                "refused rather than passed through: an input nobody bounded is an "
                "input nobody thought about"
            )
            continue
        low, high = bound
        if not (low <= value <= high):
            violations.append(f"{name}: {value} is outside the bound [{low}, {high}]")

    if not definition.inputs and definition.kind is ScenarioKind.HYPOTHETICAL:
        violations.append("a hypothetical scenario that changes nothing is not a scenario")

    if definition.kind is ScenarioKind.HISTORICAL and not definition.data_version:
        violations.append(
            "a historical scenario must name the data version it replays. Without one "
            "the run cannot be reproduced and cannot be checked for leakage"
        )

    if seen is not None:
        print_hash = definition.fingerprint()
        if print_hash in seen:
            violations.append(
                f"identical to scenario {seen[print_hash]} already run under the same "
                "data, model and policy versions"
            )

    return Validation(not violations, tuple(violations))


@dataclass(frozen=True)
class Confidence:
    """Section 13. Separate components, and absent is absent.

    Not a single score. A caller that wants one number would have to decide how
    to combine them, and the honest combination is "the lowest", which is what
    `weakest()` returns.
    """

    data: Decimal | None = None
    model: Decimal | None = None
    scenario: Decimal | None = None
    portfolio: Decimal | None = None
    execution: Decimal | None = None

    def known(self) -> dict[str, Decimal]:
        return {
            k: v
            for k, v in (
                ("data", self.data),
                ("model", self.model),
                ("scenario", self.scenario),
                ("portfolio", self.portfolio),
                ("execution", self.execution),
            )
            if v is not None
        }

    def weakest(self) -> Decimal | None:
        """The lowest stated component, or None if nothing was stated.

        **None, not zero and not one.** A caller must be able to tell "nothing
        was measured" from "everything was measured and it was bad", because
        those warrant different responses.
        """
        known = self.known()
        return min(known.values()) if known else None

    def is_low(self, *, floor: Decimal = Decimal("0.5")) -> bool:
        """**Unknown counts as low.** Section 13: UNKNOWN must not be read as
        HIGH, and the reading that does the damage is the one that treats an
        unmeasured confidence as good enough to act on."""
        weakest = self.weakest()
        return weakest is None or weakest < floor


@dataclass(frozen=True)
class Outlook:
    """A predictive statement, with everything needed to judge it. Section 12."""

    indicator: str
    value: Decimal
    methodology: str
    at: datetime
    validity: timedelta
    confidence: Confidence = field(default_factory=Confidence)
    data_version: str = ""
    model_version: str = ""

    def expired(self, *, now: datetime) -> bool:
        return now >= self.at + self.validity

    def as_dict(self) -> dict[str, Any]:
        return {
            "indicator": self.indicator,
            "value": str(self.value),
            "methodology": self.methodology,
            "at": self.at.isoformat(),
            "valid_until": (self.at + self.validity).isoformat(),
            "confidence": {k: str(v) for k, v in self.confidence.known().items()},
            "data_version": self.data_version,
            "model_version": self.model_version,
            "kind": "PREDICTION",
            "authority": (
                "A prediction, not an observation and not an instruction. It may "
                "tighten a decision and can never loosen one."
            ),
        }


def conservative_of(baseline: Outlook, model: Outlook | None) -> tuple[Outlook, str]:
    """Section 16. Where a model and a baseline disagree, take the safer.

    **A model is never consulted to relax what a baseline already said.** It is
    consulted only to make the answer more conservative, so an absent or
    unreliable model degrades the result to the baseline rather than to
    nothing -- which is section 46's TEST 10, and it needs no fallback logic
    because the baseline was always the floor.

    "Safer" means the larger stress figure, so this assumes the indicator is
    oriented such that more is worse. Named in the returned reason so a caller
    cannot use it on an indicator where that is untrue.
    """
    if model is None:
        return baseline, (
            "no model outlook was supplied, so the deterministic baseline stands. A "
            "missing model removes a tightening input; it never removes a floor"
        )
    if model.confidence.is_low():
        return baseline, (
            "the model outlook carries low or unstated confidence, so the deterministic "
            "baseline stands. An unmeasured confidence is treated as low, never as high"
        )
    if model.value > baseline.value:
        return model, (
            f"the model projects {model.value} against the baseline's {baseline.value}; "
            "the more conservative figure is taken (higher is worse for this indicator)"
        )
    return baseline, (
        f"the model projects {model.value}, below the baseline's {baseline.value}. The "
        "baseline stands: a model may raise a stress estimate and may never lower one"
    )


@dataclass(frozen=True)
class GateResult:
    verdict: GateVerdict
    reason: str
    risk_class: RiskClass
    warnings: tuple[str, ...] = ()

    @property
    def may_proceed(self) -> bool:
        """Whether this reaches the real safety chain at all.

        PASS and WARN proceed -- **to verification, not to execution.** The
        chain after this is certification, policy verification, safety
        validation, the risk orchestrator, the RiskEngine, the sizer and the
        OMS, and this gate replaces none of them.
        """
        return self.verdict in (GateVerdict.PASS, GateVerdict.WARN)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.name,
            "reason": self.reason,
            "risk_class": self.risk_class.name,
            "warnings": list(self.warnings),
            "may_proceed": self.may_proceed,
            "authority": (
                "A scenario gate, not a risk check. It decides whether a proposal is "
                "worth putting in front of the safety chain. The RiskEngine remains "
                "the final veto and the OMS owns order state."
            ),
        }


def gate(
    *,
    definition: ScenarioDefinition,
    confidence: Confidence,
    breaches_hard_constraint: bool,
    current: GateVerdict = GateVerdict.PASS,
    safe_mode: bool = False,
    certification_permits: bool = True,
    unreconciled_order: bool = False,
    data: Freshness = Freshness.FRESH,
) -> GateResult:
    """L66 section 32. Whether a scenario-driven proposal proceeds.

    **The return can only ever be at least as restrictive as `current`.** That
    is the asymmetry the module exists for, and it is enforced at the end by a
    single `max()` rather than by each branch remembering to respect it -- a
    rule every branch has to observe is a rule one branch eventually will not.
    """
    verdicts: list[GateVerdict] = [current]
    warnings: list[str] = []
    reasons: list[str] = []

    def raise_to(verdict: GateVerdict, why: str) -> None:
        verdicts.append(verdict)
        reasons.append(why)

    if safe_mode:
        raise_to(
            GateVerdict.REJECT,
            "safe mode is engaged. A scenario cannot argue a portfolio out of a latched "
            "refusal, whatever it projects",
        )
    if unreconciled_order:
        raise_to(
            GateVerdict.REJECT,
            "an order's venue state was never established. It is settled by asking the "
            "venue, never by acting on a forecast about it",
        )
    if not certification_permits:
        raise_to(
            GateVerdict.REJECT,
            "certification does not permit autonomous adaptation, so a scenario "
            "recommendation cannot be applied without a person",
        )
    if breaches_hard_constraint:
        raise_to(
            GateVerdict.REJECT,
            "the proposal breaches a hard constraint under this scenario. A hard "
            "constraint is not a scenario input",
        )
    if data is not Freshness.FRESH:
        raise_to(
            GateVerdict.DEFER,
            f"the data behind this scenario is {data}. A projection from evidence that "
            "is not current is not evidence about what happens next",
        )

    risk = definition.risk_class()
    if risk is RiskClass.CRITICAL:
        raise_to(
            GateVerdict.RESTRICT,
            f"{definition.name} classifies as CRITICAL "
            f"({definition.likelihood.name} x {definition.severity.name})",
        )
    elif risk is RiskClass.HIGH:
        raise_to(
            GateVerdict.WARN,
            f"{definition.name} classifies as HIGH "
            f"({definition.likelihood.name} x {definition.severity.name})",
        )

    if confidence.is_low():
        # Section 46's TEST 4. Low confidence does not license a bigger move --
        # it licenses a smaller one. A forecast nobody can vouch for is a
        # reason to do less, never a reason to do more.
        raise_to(
            GateVerdict.WARN,
            "the scenario's confidence is low or unstated, so it is treated "
            "conservatively. An unmeasured confidence is low, never high",
        )
        warnings.append("low or unstated confidence")

    verdict = max(verdicts)
    if verdict is current and not reasons:
        reasons.append(
            "no scenario condition raised the verdict. The scenario adds nothing, "
            "which is not the same as the scenario permitting anything"
        )

    return GateResult(verdict, " | ".join(reasons), risk, tuple(warnings))


class Recommendation(StrEnum):
    """L66 section 33. What a scenario may suggest. None of these executes."""

    REDUCE_CONCENTRATION = "REDUCE_CONCENTRATION"
    REDUCE_ALLOCATION = "REDUCE_ALLOCATION"
    DEFER_INCREASE = "DEFER_INCREASE"
    DIVERSIFY = "DIVERSIFY"
    REVIEW_STRATEGY = "REVIEW_STRATEGY"
    REVIEW_POLICY = "REVIEW_POLICY"
    INVESTIGATE_EXECUTION = "INVESTIGATE_EXECUTION"
    MONITOR_LIQUIDITY = "MONITOR_LIQUIDITY"
    NO_CHANGE = "NO_CHANGE"


#: Which way each recommendation moves permissions.
#:
#: Classified by EFFECT rather than by name, the same way L61 classifies
#: actions -- and the first version of the test for this matched names, which
#: flagged `DEFER_INCREASE` as loosening because the string contains
#: "INCREASE". It withholds an increase; it is a restriction. A blocklist on
#: substrings gets both false positives and false negatives, and the false
#: negative is the one that matters.
#:
#: **Every member is TIGHTENS or NEUTRAL. None is LOOSENS.** Section 33's list
#: contains none either, and that is not an oversight in the brief -- it is the
#: asymmetry again. A scenario engine that could recommend increasing something
#: would be a forecast with an upside, and a forecast with an upside is one
#: somebody will eventually act on for its upside.
RECOMMENDATION_EFFECT: dict[Recommendation, str] = {
    Recommendation.REDUCE_CONCENTRATION: "TIGHTENS",
    Recommendation.REDUCE_ALLOCATION: "TIGHTENS",
    Recommendation.DEFER_INCREASE: "TIGHTENS",
    Recommendation.DIVERSIFY: "TIGHTENS",
    Recommendation.REVIEW_STRATEGY: "NEUTRAL",
    Recommendation.REVIEW_POLICY: "NEUTRAL",
    Recommendation.INVESTIGATE_EXECUTION: "NEUTRAL",
    Recommendation.MONITOR_LIQUIDITY: "NEUTRAL",
    Recommendation.NO_CHANGE: "NEUTRAL",
}

#: Empty, and it is a function of the mapping above rather than a second list
#: that could disagree with it.
LOOSENING_RECOMMENDATIONS: frozenset[Recommendation] = frozenset(
    r for r, effect in RECOMMENDATION_EFFECT.items() if effect == "LOOSENS"
)


@dataclass(frozen=True)
class Warning_:
    """L66 section 30. A predictive warning, presented as a prediction."""

    scenario_id: str
    message: str
    risk_class: RiskClass
    confidence: Confidence
    at: datetime
    validity: timedelta
    affected: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "message": self.message,
            "risk_class": self.risk_class.name,
            "confidence": {k: str(v) for k, v in self.confidence.known().items()},
            "at": self.at.isoformat(),
            "valid_until": (self.at + self.validity).isoformat(),
            "affected": list(self.affected),
            "kind": "PREDICTION",
            "authority": (
                "A projection under a stated scenario, not a forecast of what will "
                "happen and not an observation of what did."
            ),
        }


__all__ = [
    "BOUNDS",
    "LOOSENING_RECOMMENDATIONS",
    "RECOMMENDATION_EFFECT",
    "MATRIX",
    "Confidence",
    "Freshness",
    "GateResult",
    "GateVerdict",
    "Likelihood",
    "Outlook",
    "Recommendation",
    "RiskClass",
    "ScenarioDefinition",
    "ScenarioKind",
    "Severity",
    "Validation",
    "Warning_",
    "classify",
    "conservative_of",
    "gate",
    "validate",
]
