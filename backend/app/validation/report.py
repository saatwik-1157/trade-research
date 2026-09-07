"""The report: a list of named verdicts, and what the overall one means.

Section 27, stated as code. There is no score. `ValidationReport.as_dict()`
returns the checks with their own severities, the verdict derived from them by
`verdict_from()`, and the evidence each one rested on — so a reader disagreeing
with a threshold can see the number it was compared against and re-decide.

**A PASS is a statement about validation, not about deployment.** Section 34.
The words are in the payload rather than only in a docstring, because the
payload is what a frontend renders and what an operator reads at 2am.

**A report is never a promotion.** Nothing in this module writes to
`model_versions.status`, and nothing calls the registry. Section 30: validation
records a verdict; a human decides what to do with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.validation.checks import Finding
from app.validation.config import (
    VALIDATION_ENGINE_VERSION,
    Severity,
    ValidationConfig,
    Verdict,
    verdict_from,
)

# What a verdict authorises, in the words that go into the payload. Section 34
# is explicit that a PASS must not read as "deploy to live trading now", and the
# only reliable way to stop it reading that way is to say the opposite next to
# it every time.
MEANS: dict[Verdict, str] = {
    Verdict.passed: (
        "the candidate satisfies the validation requirements that were checked, under "
        "the thresholds recorded in this report. It does NOT mean: deploy to live "
        "trading, allocate capital, or that the model will be profitable. It means the "
        "candidate may now be CONSIDERED by the model registry."
    ),
    Verdict.conditional: (
        "every hard requirement is met and at least one check returned a warning. The "
        "candidate may be considered, and the warnings constrain how its output may be "
        "read -- a calibration warning means the probability must not be quoted as a "
        "frequency."
    ),
    Verdict.failed: (
        "at least one requirement was not met. The candidate is not eligible for "
        "consideration. This is a statement about the evidence, not a permanent one: "
        "the failing check names what would have to change."
    ),
    Verdict.blocked: (
        "at least one check could not be evaluated, so no verdict about the model is "
        "supportable. BLOCKED is not FAIL: the candidate has not been judged and no "
        "conclusion about its quality may be drawn from this report."
    ),
}

# The order checks are presented in. Deliberately not alphabetical and not the
# order they were computed in: it runs from "is the evidence usable" through
# "did the model learn" to "would it have made money", so a reader who stops
# early stops at the right place.
PRESENTATION_ORDER: tuple[str, ...] = (
    "data_integrity",
    "sample_size",
    "model_artifact",
    "version_locking",
    "leakage",
    "temporal",
    "baseline_comparison",
    "discrimination",
    "calibration",
    "overfitting",
    "economic",
    "significance",
    "walk_forward",
    "robustness",
    "regime",
)


@dataclass
class ValidationReport:
    """One candidate's validation, complete enough to disagree with."""

    config: ValidationConfig
    findings: list[Finding] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def add(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        return finding

    @property
    def verdict(self) -> Verdict:
        return verdict_from([f.severity for f in self.findings])

    @property
    def passed(self) -> bool:
        """PASS or CONDITIONAL. Both mean "eligible for consideration".

        Not a property that anything downstream may use to promote: the
        registry is level 28's and takes a human decision. This exists so a
        report can be *counted*.
        """
        return self.verdict in (Verdict.passed, Verdict.conditional)

    def counts(self) -> dict[str, int]:
        out = {str(s): 0 for s in Severity}
        for finding in self.findings:
            out[str(finding.severity)] += 1
        return out

    def ordered(self) -> list[Finding]:
        index = {name: i for i, name in enumerate(PRESENTATION_ORDER)}
        return sorted(self.findings, key=lambda f: (index.get(f.check, 99), f.check))

    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.blocked]

    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.failed]

    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.warning]

    def summary_line(self) -> str:
        """One sentence a human can read without opening the evidence."""
        verdict = self.verdict
        if verdict is Verdict.blocked:
            first = self.blockers()[0]
            return f"BLOCKED on {first.check}: {first.summary}"
        if verdict is Verdict.failed:
            names = ", ".join(f.check for f in self.failures())
            return f"FAIL on {names}"
        if verdict is Verdict.conditional:
            names = ", ".join(f.check for f in self.warnings())
            return f"CONDITIONAL: passes every requirement, with warnings on {names}"
        return f"PASS on all {len(self.findings)} checks"

    def recommendations(self) -> list[str]:
        """What would have to change, taken from the checks themselves.

        Generated from the findings rather than written as advice, so it cannot
        drift from what was actually measured.
        """
        out: list[str] = []
        for finding in self.ordered():
            if finding.severity is Severity.blocked:
                out.append(f"unblock {finding.check}: {finding.summary}")
            elif finding.severity is Severity.failed:
                out.append(f"{finding.check} must improve: {finding.summary}")
        for finding in self.ordered():
            if finding.severity is Severity.warning:
                out.append(
                    f"{finding.check} is a caveat on how to read this model: {finding.summary}"
                )
        return out

    def as_dict(self) -> dict[str, Any]:
        verdict = self.verdict
        return {
            "validation_engine_version": VALIDATION_ENGINE_VERSION,
            "verdict": str(verdict),
            "summary": self.summary_line(),
            "means": MEANS[verdict],
            "authority": (
                "advisory and terminal. This report does not activate, promote, deploy "
                "or size anything, and no code path leads from it to an order. A human "
                "reads it and decides."
            ),
            "checks": [f.as_dict() for f in self.ordered()],
            "counts": self.counts(),
            "recommendations": self.recommendations(),
            "config": self.config.as_dict(),
            "context": self.context,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "method": {
                "scoring": (
                    "there is no score. Each check carries its own severity and the "
                    "overall verdict is BLOCKED > FAIL > CONDITIONAL > PASS over them. A "
                    "weighted composite can always be tuned until it hides the check "
                    "that mattered."
                ),
                "blocked": (
                    "BLOCKED means a check could not be evaluated -- too few samples, a "
                    "missing baseline, an unreadable artifact. It is never reported as a "
                    "pass and never as a failure, because both would be claims about the "
                    "model that the evidence does not support."
                ),
                "separation": (
                    "ML metrics and economic metrics are reported separately and never "
                    "merged. A model can score well on the first and lose money on the "
                    "second; this repository has measured exactly that."
                ),
                "reproducibility": (
                    "every random draw in this report is seeded, and the thresholds used "
                    "are recorded in `config`. Re-running the same configuration against "
                    "the same dataset fingerprint produces the same verdict."
                ),
            },
        }


def markdown(report: ValidationReport) -> str:
    """The report as the table section 27 asks for.

    A rendering of `as_dict()`, not a second source of truth -- the numbers come
    from the findings either way.
    """
    lines = [
        f"# Validation report — {report.config.model_version_id}",
        "",
        f"**{report.verdict}** — {report.summary_line()}",
        "",
        MEANS[report.verdict],
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for finding in report.ordered():
        detail = finding.summary.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {finding.check} | {finding.severity} | {detail} |")
    if report.recommendations():
        lines += ["", "## What would have to change", ""]
        lines += [f"- {line}" for line in report.recommendations()]
    lines += [
        "",
        "## How to read this",
        "",
        "There is no score. A verdict is the most severe check, because a composite "
        "number can be tuned until it hides the one that mattered.",
        "",
        f"Engine {VALIDATION_ENGINE_VERSION}. Thresholds used are in the report payload.",
    ]
    return "\n".join(lines)
