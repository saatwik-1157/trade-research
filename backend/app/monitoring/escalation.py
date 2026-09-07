"""Findings to recommendations.

    WARNING -> PAUSE/REDUCE STRATEGY -> REVIEW -> RETRAIN -> VALIDATE

Each rung is a recommendation for a person or a supervisor to act on. The top
rung is "validate", not "deploy": a monitor may conclude that a model needs
retraining and revalidating, and it may never conclude that a replacement
should go live. That decision belongs to the model registry and to whoever
signs it off.
"""

from __future__ import annotations

from datetime import datetime

from app.monitoring.findings import (
    Action,
    Alert,
    Check,
    Finding,
    Severity,
    worst,
)

# Which rungs a severity justifies. Higher severities include the lower rungs,
# so a critical finding still says "warn" as well as "validate".
LADDER: dict[Severity, tuple[Action, ...]] = {
    Severity.insufficient_data: (),
    Severity.ok: (),
    Severity.warning: (Action.warn,),
    Severity.serious: (Action.warn, Action.reduce_exposure, Action.review),
    Severity.critical: (
        Action.warn,
        Action.pause_strategy,
        Action.review,
        Action.retrain,
        Action.validate,
    ),
}

# Checks whose failure implicates the model itself rather than the market, so
# a serious reading escalates to retraining rather than stopping at review.
MODEL_CHECKS: frozenset[Check] = frozenset(
    {Check.prediction_drift, Check.prediction_distribution, Check.calibration}
)


def recommended_actions(findings: list[Finding]) -> list[Action]:
    actions: list[Action] = []
    for finding in findings:
        if not finding.actionable:
            continue
        rungs = list(LADDER[finding.severity])
        if finding.check in MODEL_CHECKS and finding.severity is Severity.serious:
            rungs += [Action.retrain, Action.validate]
        for action in rungs:
            if action not in actions:
                actions.append(action)
    order = list(Action)
    return sorted(actions, key=order.index)


def build_alerts(findings: list[Finding], at: datetime) -> list[Alert]:
    """One alert per severity band that has actionable findings."""
    alerts: list[Alert] = []
    for severity in (Severity.critical, Severity.serious, Severity.warning):
        band = [f for f in findings if f.severity is severity]
        if not band:
            continue
        actions = recommended_actions(band)
        top = actions[-1] if actions else Action.none
        checks = sorted({str(f.check) for f in band})
        alerts.append(
            Alert(
                severity=severity,
                action=top,
                title=f"{severity.value}: {', '.join(checks)}",
                body=(
                    f"{len(band)} finding(s) at {severity.value}. "
                    f"Recommended: {', '.join(a.value for a in actions) or 'none'}. "
                    "No model is promoted, deployed or replaced by monitoring."
                ),
                raised_at=at,
                findings=tuple(band),
            )
        )
    return alerts


def overall(findings: list[Finding]) -> Severity:
    return worst([f.severity for f in findings])
