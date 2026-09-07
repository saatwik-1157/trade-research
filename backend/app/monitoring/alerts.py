"""Raising an alert once, keeping it raised, and saying when it clears.

Sections 22 to 25. The escalation ladder in `escalation.py` already decides
*what* an alert recommends; this decides *whether one is sent at all*.

**A fingerprint, not a timestamp.** An alert's identity is
`(model version, scope, check, subject, severity)` — everything that makes two
alerts the same problem, and nothing that makes them different moments. Drift on
`rsi_14` at WARNING is one alert however many times it is observed.

**Three transitions, and only two of them notify:**

    absent  → firing     a new problem. NOTIFY.
    firing  → firing     the same problem, still there. SUPPRESS.
    firing  → resolved   it cleared. NOTIFY, as a recovery.

The middle one is §24's whole point: *"Feature drift remains → no alert every
second."* Without it a monitor that runs hourly produces 24 identical
notifications a day and is muted within a week, at which point it detects
nothing.

**A severity change is a new alert.** WARNING → CRITICAL is not "the same
problem still there"; it is a problem that got worse, and suppressing it would
hide the transition an operator most needs to see. The fingerprint includes the
severity precisely so that this cannot be suppressed.

**Recovery is recorded, not inferred.** Section 25. A condition that stops being
reported does not silently disappear from the history: it gets a row saying it
cleared, so the operational history reads as *raised at 09:00, cleared at 14:20*
rather than as a gap somebody has to interpret.

**Nothing here acts.** An alert is a message. `FORBIDDEN_ACTIONS` in
`findings.py` already forbids the vocabulary; this module writes rows and
publishes events, and imports nothing that could trade.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.monitoring.findings import Finding, Severity

#: Severities that raise an alert at all. `ok` is not an alert, and
#: `insufficient_data` is deliberately not one either: "we could not measure
#: this" is a real state and it is reported on the snapshot, but paging somebody
#: because a window was quiet is how a monitor teaches people to ignore it.
ALERTING: frozenset[Severity] = frozenset({Severity.warning, Severity.serious, Severity.critical})

#: Section 23. The three levels an alert carries, mapped from the five
#: severities the checks produce.
NOTIFICATION_SEVERITY: dict[Severity, str] = {
    Severity.warning: "warning",
    Severity.serious: "warning",
    Severity.critical: "critical",
}


def fingerprint(
    *,
    model_version_id: str,
    scope_key: str,
    check: str,
    subject: str,
    severity: str,
) -> str:
    """What makes two alerts the same problem. Section 24.

    Deliberately includes the SEVERITY: a WARNING that becomes CRITICAL is a
    different fingerprint and therefore a new alert, because suppressing it
    would hide the transition an operator most needs to see.

    Deliberately excludes the statistic and the time: a PSI of 0.31 and one of
    0.34 on the same feature in the same band are the same problem observed
    twice, and alerting on both is the flood §24 exists to prevent.
    """
    raw = "|".join([model_version_id, scope_key, check, subject, severity])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Candidate:
    """One alert a run would raise, before deduplication."""

    fingerprint: str
    model_version_id: str
    model_key: str
    model_version: str | None
    scope_key: str
    check: str
    subject: str
    severity: Severity
    title: str
    body: str
    metric: str | None = None
    current_value: float | None = None
    baseline_value: float | None = None
    threshold: float | None = None
    sample_size: int = 0
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "model_version_id": self.model_version_id,
            "model": self.model_key,
            "version": self.model_version,
            "scope_key": self.scope_key,
            "check": self.check,
            "subject": self.subject,
            "severity": str(self.severity),
            "title": self.title,
            "body": self.body,
            "metric": self.metric,
            "current_value": self.current_value,
            "baseline_value": self.baseline_value,
            "threshold": self.threshold,
            "sample_size": self.sample_size,
            "detail": self.detail or {},
        }


def candidates_from(
    findings: list[Finding],
    *,
    model_version_id: str,
    model_key: str,
    model_version: str | None,
    scope_key: str,
    baseline_id: str | None,
) -> list[Candidate]:
    """One candidate per actionable finding. Section 22.

    Per FINDING rather than per run, so a drifting feature and a degraded
    calibration are two alerts that can clear independently. A single
    run-level alert would clear only when everything cleared, which is how a
    dashboard ends up permanently red.
    """
    out: list[Candidate] = []
    for finding in findings:
        if finding.severity not in ALERTING:
            continue
        detail = dict(finding.detail or {})
        detail["baseline_id"] = baseline_id
        detail["reference_n"] = finding.reference_n
        detail["survives_fdr"] = finding.survives_fdr
        out.append(
            Candidate(
                fingerprint=fingerprint(
                    model_version_id=model_version_id,
                    scope_key=scope_key,
                    check=str(finding.check),
                    subject=finding.subject,
                    severity=str(finding.severity),
                ),
                model_version_id=model_version_id,
                model_key=model_key,
                model_version=model_version,
                scope_key=scope_key,
                check=str(finding.check),
                subject=finding.subject,
                severity=finding.severity,
                title=f"{finding.severity}: {finding.check} on {finding.subject}",
                body=finding.summary,
                metric=str(finding.check),
                current_value=finding.statistic,
                baseline_value=None,
                threshold=_threshold_of(finding),
                sample_size=finding.current_n,
                detail=detail,
            )
        )
    return out


def _threshold_of(finding: Finding) -> float | None:
    """The bar this finding was compared against, when it named one.

    §22 asks an alert to carry its threshold. Read from the finding's own detail
    rather than re-derived, so the number in the alert is the number the check
    actually used.
    """
    detail = finding.detail or {}
    for name in (
        "psi_serious",
        "critical_ms",
        "serious_at",
        "error_rate_critical",
        "consecutive_loss_limit",
    ):
        value = detail.get(name)
        if isinstance(value, int | float):
            return float(value)
    return None


@dataclass(frozen=True)
class Decision:
    """What to do about one candidate, and why. Section 24."""

    candidate: Candidate
    notify: bool
    reason: str


def deduplicate(
    candidates: list[Candidate],
    *,
    open_alerts: dict[str, datetime],
    now: datetime,
    cooldown: timedelta,
) -> list[Decision]:
    """Which of these are new enough to send. Section 24.

    `open_alerts` maps a fingerprint to when it last notified. A fingerprint
    that is not there is new; one that is there and inside the cooldown is
    suppressed; one that is there and outside it re-notifies, because a problem
    that has persisted for a day is worth saying again.
    """
    out: list[Decision] = []
    for candidate in candidates:
        last = open_alerts.get(candidate.fingerprint)
        if last is None:
            out.append(Decision(candidate, True, "new: this exact condition was not already open"))
        elif now - last >= cooldown:
            minutes = int(cooldown.total_seconds() / 60)
            out.append(
                Decision(
                    candidate,
                    True,
                    f"still open after the {minutes}-minute cooldown, so it is said again",
                )
            )
        else:
            out.append(
                Decision(
                    candidate,
                    False,
                    "already open and inside the cooldown. Suppressed rather than "
                    "repeated: a monitor that says the same thing every run is muted "
                    "within a week, and then it detects nothing.",
                )
            )
    return out


def recoveries(
    *,
    open_alerts: set[str],
    current: list[Candidate],
) -> set[str]:
    """Fingerprints that were open and are no longer reported. Section 25.

    Returned as a set for the caller to resolve and announce. A condition that
    simply stops appearing must NOT vanish from the history: the operational
    record should read "raised at 09:00, cleared at 14:20" rather than leaving a
    gap somebody has to interpret.
    """
    still = {c.fingerprint for c in current}
    return open_alerts - still


def summarise(candidate: Candidate, *, notified: bool, reason: str) -> dict[str, Any]:
    return {
        **candidate.as_dict(),
        "notified": notified,
        "deduplication": reason,
        "authority": (
            "an alert is a message. Monitoring never retrains, promotes, replaces or "
            "deploys a model, and never modifies a strategy, a risk limit or a position "
            "size."
        ),
    }
