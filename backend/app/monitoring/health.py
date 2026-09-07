"""From findings to one health state, by rule rather than by score.

Section 21: *"Health must be based on measurable conditions. Do not use
arbitrary random health scores."*

So there is no score. `derive()` maps each finding's severity to a health state
and takes the worst by precedence — the same shape L26's verdict uses, and for
the same reason: any weighted composite can be tuned until it hides the check
that mattered.

**Two states are not about the model at all**, and they are the ones most often
got wrong:

  * `OFFLINE` — nothing is deployed for this scope. A statement about the
    registry, and it outranks everything else because a model that is not
    serving cannot be healthy *or* degraded; every other reading would be about
    a model nobody is using.
  * `INSUFFICIENT_DATA` — checks ran and the samples could not support a
    conclusion. It sits ABOVE `HEALTHY` in the precedence, because §43 is
    explicit that too little data must never become a false healthy state.

**`DEGRADED` is reserved for the model.** A warning on a feature distribution
says the market moved; a warning on calibration or prediction drift says the
model's own behaviour changed. §42 asks that a distribution change not be read
as a failure, and this is where that distinction becomes a state an operator
reacts to differently.
"""

from __future__ import annotations

from typing import Any

from app.monitoring.config import HealthState, worst_health
from app.monitoring.findings import Check, Finding, Severity

#: Checks whose reading implicates the MODEL rather than the market. A serious
#: finding on one of these degrades the model's health; the same severity on a
#: feature distribution does not, because the inputs moving is the world moving.
#:
#: Same set `escalation.MODEL_CHECKS` uses, extended with the two L29 added that
#: are also about the model's own behaviour.
MODEL_IMPLICATING: frozenset[Check] = frozenset(
    {
        Check.prediction_drift,
        Check.prediction_distribution,
        Check.confidence_drift,
        Check.calibration,
        Check.performance,
        Check.possible_concept_drift,
    }
)

#: Checks about the SERVICE. A failure here means the model is not answering,
#: which is an availability problem rather than a quality one -- and under
#: AI_REQUIRED it already means no trade, which L27 enforces.
SERVICE_CHECKS: frozenset[Check] = frozenset({Check.latency, Check.availability})


def state_for(finding: Finding) -> HealthState:
    """One finding as a health state.

    A `serious` reading on a model-implicating check is DEGRADED; the same
    severity on a market-facing one is WARNING. That is §42 made operational:
    "the feature distribution changed" and "the model's own behaviour changed"
    are different events and should not produce the same state.
    """
    if finding.severity is Severity.insufficient_data:
        return HealthState.insufficient_data
    if finding.severity is Severity.ok:
        return HealthState.healthy
    if finding.severity is Severity.critical:
        return HealthState.critical
    if finding.severity is Severity.serious:
        return (
            HealthState.degraded
            if finding.check in MODEL_IMPLICATING or finding.check in SERVICE_CHECKS
            else HealthState.warning
        )
    return HealthState.warning


def derive(findings: list[Finding], *, deployed: bool = True) -> HealthState:
    """The overall state. Section 21.

    `deployed=False` short-circuits to OFFLINE: with nothing serving, every
    other reading would be about a model nobody is using, and reporting HEALTHY
    would be the false clean bill §43 warns about wearing a different hat.
    """
    if not deployed:
        return HealthState.offline
    if not findings:
        return HealthState.insufficient_data
    return worst_health([state_for(f) for f in findings])


def explain(findings: list[Finding], *, deployed: bool = True) -> dict[str, Any]:
    """The state, and the findings that produced it. Section 21 and §37."""
    state = derive(findings, deployed=deployed)
    by_state: dict[str, list[str]] = {}
    for finding in findings:
        by_state.setdefault(str(state_for(finding)), []).append(
            f"{finding.check}:{finding.subject}"
        )
    driving = [
        {
            "check": str(f.check),
            "subject": f.subject,
            "severity": str(f.severity),
            "summary": f.summary,
        }
        for f in findings
        if state_for(f) is state and state is not HealthState.healthy
    ]
    return {
        "state": str(state),
        "driving": driving,
        "by_state": {k: sorted(v) for k, v in sorted(by_state.items())},
        "checks_run": len(findings),
        "unmeasured": sum(1 for f in findings if f.severity is Severity.insufficient_data),
        "method": {
            "no_score": (
                "there is no health score. The state is the worst of the per-check "
                "states by precedence, because a weighted composite can be tuned until "
                "it hides the check that mattered."
            ),
            "insufficient_data": (
                "outranks HEALTHY. A run in which checks could not be evaluated is not a "
                "healthy run, and too little data must never become a false healthy "
                "state."
            ),
            "degraded": (
                "reserved for readings that implicate the MODEL -- prediction drift, "
                "confidence, calibration, performance -- or its SERVICE. A feature "
                "distribution moving is the market moving, and it warns rather than "
                "degrades."
            ),
        },
    }
