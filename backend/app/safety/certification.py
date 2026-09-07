"""The twelve gates, and what the platform is certified for. **L62 sections 16-18.**

A certification is a claim, and the only thing that makes it worth anything is
that it can come out negative. So `certify()` derives its result from the
invariant registry rather than from a constant, and the two rules that decide
it are the two that make the claim falsifiable:

* **A gate whose invariants are all NOT_APPLICABLE does not PASS.** It returns
  `NOT_TESTED`. A gate around a component that does not exist has verified
  nothing, and a green row for it would be the single most misleading thing
  this file could produce.
* **One failing CRITICAL invariant revokes certification outright**, with no
  weighing against the others. Twenty-four passes do not average out a
  breached veto.

The consequence is the honest one: this platform is `CONDITIONALLY_CERTIFIED`,
not `CERTIFIED`, and it is the two `NOT_TESTED` gates that hold it there.
`SAFETY_CERTIFICATION.md` says which and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.safety.invariants import INVARIANTS, Invariant, InvariantStatus, Severity, by_id


class CertificationState(StrEnum):
    """L62 section 18."""

    NOT_CERTIFIED = "NOT_CERTIFIED"
    #: Every gate that could be tested passed, and at least one could not be.
    CONDITIONALLY_CERTIFIED = "CONDITIONALLY_CERTIFIED"
    CERTIFIED = "CERTIFIED"
    CERTIFICATION_EXPIRED = "CERTIFICATION_EXPIRED"
    CERTIFICATION_REVOKED = "CERTIFICATION_REVOKED"


class GateStatus(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"
    #: Nothing to test. Never to be reported as PASS.
    NOT_TESTED = "NOT_TESTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Gate:
    """One certification gate and the invariants that make it up."""

    id: str
    name: str
    question: str
    invariants: tuple[str, ...]


GATES: tuple[Gate, ...] = (
    Gate(
        "GATE-01",
        "Architecture Safety",
        "Is there any unauthorized execution path?",
        ("INV-03", "INV-04", "INV-24"),
    ),
    Gate("GATE-02", "Risk Safety", "Is the RiskEngine still the final veto?", ("INV-01", "INV-02")),
    Gate(
        "GATE-03",
        "Policy Safety",
        "Can a hard constraint be overridden?",
        (
            "INV-05",
            "INV-06",
            "INV-15",
            "INV-16",
            "INV-26",
            "INV-27",
            "INV-28",
            "INV-29",
            "INV-30",
            "INV-31",
        ),
    ),
    Gate("GATE-04", "State Safety", "Are invalid transitions rejected?", ("INV-13", "INV-14")),
    Gate(
        "GATE-05",
        "Data Safety",
        "Can stale or future data drive an unsafe decision?",
        ("INV-11", "INV-22"),
    ),
    Gate("GATE-06", "Execution Safety", "Are unknown order states reconciled?", ("INV-10",)),
    Gate("GATE-07", "Recovery Safety", "Do failures enter a safe state?", ("INV-19", "INV-20")),
    Gate(
        "GATE-08", "Control Loop Safety", "Can adaptation run away?", ("INV-17", "INV-18", "INV-21")
    ),
    Gate("GATE-09", "Account Isolation", "Can one account contaminate another?", ("INV-12",)),
    Gate(
        "GATE-10", "Reproducibility", "Can a historical decision be replayed?", ("INV-22", "INV-25")
    ),
    Gate(
        "GATE-11",
        "Security",
        "Can AI or a caller cross a privilege boundary?",
        ("INV-02", "INV-23"),
    ),
    Gate(
        "GATE-12",
        "Environment Safety",
        "Is paper still the default and live still off?",
        ("INV-07", "INV-08", "INV-09"),
    ),
)


@dataclass(frozen=True)
class GateResult:
    gate: Gate
    status: GateStatus
    reason: str
    invariants: tuple[Invariant, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.id,
            "name": self.gate.name,
            "question": self.gate.question,
            "status": str(self.status),
            "reason": self.reason,
            "invariants": [
                {"id": i.id, "status": str(i.status), "severity": str(i.severity)}
                for i in self.invariants
            ],
        }


@dataclass(frozen=True)
class Certification:
    state: CertificationState
    at: datetime
    gates: tuple[GateResult, ...]
    reasons: tuple[str, ...]

    @property
    def blocking(self) -> tuple[GateResult, ...]:
        """The gates that are the reason this is not CERTIFIED."""
        return tuple(
            g
            for g in self.gates
            if g.status in (GateStatus.FAIL, GateStatus.NOT_TESTED, GateStatus.BLOCKED)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "at": self.at.isoformat(),
            "gates": [g.as_dict() for g in self.gates],
            "reasons": list(self.reasons),
            "blocking_gates": [g.gate.id for g in self.blocking],
        }


def evaluate_gate(gate: Gate, *, failed: frozenset[str] = frozenset()) -> GateResult:
    """One gate's status, from the status of the invariants under it.

    `failed` is the set of invariant ids a verification run actually observed
    failing. It is a parameter rather than something read from a store because
    a gate must never be able to report PASS from its own memory -- the caller
    has to supply what the run found.
    """
    invs = tuple(by_id(i) for i in gate.invariants)

    broken = tuple(i for i in invs if i.id in failed)
    if broken:
        worst = min(broken, key=lambda i: list(Severity).index(i.severity))
        return GateResult(
            gate,
            GateStatus.FAIL,
            f"{len(broken)} invariant(s) failed, most severe {worst.id} ({worst.severity})",
            invs,
        )

    unenforced = tuple(i for i in invs if i.status is InvariantStatus.UNENFORCED)
    if unenforced:
        return GateResult(
            gate,
            GateStatus.FAIL,
            f"{', '.join(i.id for i in unenforced)} applies here and nothing enforces it",
            invs,
        )

    applicable = tuple(i for i in invs if i.status is InvariantStatus.ENFORCED)
    if not applicable:
        return GateResult(
            gate,
            GateStatus.NOT_TESTED,
            "every invariant under this gate is NOT_APPLICABLE: the components it "
            "would constrain do not exist, so the gate has verified nothing. This is "
            "not a pass",
            invs,
        )

    absent = tuple(i for i in invs if i.status is InvariantStatus.NOT_APPLICABLE)
    if absent:
        return GateResult(
            gate,
            GateStatus.WARNING,
            f"{', '.join(i.id for i in applicable)} verified; "
            f"{', '.join(i.id for i in absent)} not applicable here",
            invs,
        )

    return GateResult(
        gate,
        GateStatus.PASS,
        f"{', '.join(i.id for i in applicable)} verified",
        invs,
    )


def certify(*, now: datetime, failed: frozenset[str] = frozenset()) -> Certification:
    """The certification state, derived rather than declared."""
    results = tuple(evaluate_gate(g, failed=failed) for g in GATES)
    reasons: list[str] = []

    critical_failures = tuple(
        i for i in INVARIANTS if i.id in failed and i.severity is Severity.CRITICAL
    )
    if critical_failures:
        reasons.append(
            "A CRITICAL invariant failed ("
            + ", ".join(i.id for i in critical_failures)
            + "). Certification is revoked rather than downgraded: passes elsewhere do "
            "not average out a breached veto."
        )
        return Certification(CertificationState.CERTIFICATION_REVOKED, now, results, tuple(reasons))

    if any(g.status is GateStatus.FAIL for g in results):
        reasons.append(
            "A gate failed: " + ", ".join(g.gate.id for g in results if g.status is GateStatus.FAIL)
        )
        return Certification(CertificationState.NOT_CERTIFIED, now, results, tuple(reasons))

    untested = tuple(g for g in results if g.status is GateStatus.NOT_TESTED)
    if untested:
        reasons.append(
            "Every gate that could be tested passed. "
            + ", ".join(g.gate.id for g in untested)
            + " could not be: the components those gates would constrain do not exist "
            "in this repository, so they have verified nothing and are not reported as "
            "passing."
        )
        return Certification(
            CertificationState.CONDITIONALLY_CERTIFIED, now, results, tuple(reasons)
        )

    warned = tuple(g for g in results if g.status is GateStatus.WARNING)
    if warned:
        reasons.append(
            "Passed with " + ", ".join(g.gate.id for g in warned) + " partially applicable."
        )
        return Certification(
            CertificationState.CONDITIONALLY_CERTIFIED, now, results, tuple(reasons)
        )

    reasons.append("All twelve gates verified against enforced invariants.")
    return Certification(CertificationState.CERTIFIED, now, results, tuple(reasons))


__all__ = [
    "GATES",
    "Certification",
    "CertificationState",
    "Gate",
    "GateResult",
    "GateStatus",
    "certify",
    "evaluate_gate",
]
