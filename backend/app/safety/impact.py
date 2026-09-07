"""What a change costs the certification. **L63 section 10.**

This is the one part of L63 that has something real to work on **today**. Every
other section describes watching a control loop that is not running; this reads
a set of changed files -- which the repository produces on every commit -- and
answers whether the certification still stands.

It works because L62 left a map behind. Each invariant names the module that
enforces it, and each gate names its invariants, so a changed file resolves to
invariants, to gates, and to a verdict. **No new bookkeeping was needed; the
registry already was the mapping.**

**Unrecognised is HIGH, never NONE.** A path nobody classified is a path nobody
thought about, and the safe reading of "I do not know what this touches" is not
"nothing". That is the whole difference between an impact analyser and a
rubber stamp, and it is the branch that would be most tempting to soften the
first time it flags something dull.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.safety.certification import GATES
from app.safety.invariants import INVARIANTS, InvariantStatus, Severity


class Impact(StrEnum):
    """L63 section 10. What a change requires."""

    NONE = "NONE"
    TARGETED_REVALIDATION = "TARGETED_REVALIDATION"
    FULL_REVALIDATION = "FULL_REVALIDATION"
    SUSPEND_AUTONOMY = "SUSPEND_AUTONOMY"


#: Paths whose change genuinely cannot affect certification.
#:
#: Deliberately short and deliberately about DOCUMENTS AND VIEWS. Everything
#: else is presumed to matter until the mapping says otherwise. A generous
#: exempt list is how a change to a "harmless" module stops being reviewed.
EXEMPT_PREFIXES: tuple[str, ...] = (
    "docs/",
    "frontend/",
    "reports/",
    "tools/",
)

EXEMPT_SUFFIXES: tuple[str, ...] = (".md", ".txt", ".json", ".csv")


#: Paths that suspend autonomy on any change, regardless of what changed.
#:
#: These are the modules whose correctness every other judgement rests on. A
#: diff to one of them means the platform is running code that has not been
#: certified, and the honest response is to stop acting until it has been --
#: not to reason about whether the change looked safe.
CRITICAL_PATHS: tuple[str, ...] = (
    "backend/app/risk/engine.py",
    "backend/app/oms/service.py",
    "backend/app/oms/state.py",
    "backend/app/brokers/base.py",
    "backend/app/execution/pipeline.py",
    "backend/app/safety/",
)


@dataclass(frozen=True)
class ImpactAssessment:
    """What changed, what it touches, and what has to happen about it."""

    impact: Impact
    changed: tuple[str, ...]
    affected_invariants: tuple[str, ...]
    affected_gates: tuple[str, ...]
    unrecognised: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def requires_revalidation(self) -> bool:
        return self.impact is not Impact.NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "impact": str(self.impact),
            "requires_revalidation": self.requires_revalidation,
            "changed": list(self.changed),
            "affected_invariants": list(self.affected_invariants),
            "affected_gates": list(self.affected_gates),
            "unrecognised": list(self.unrecognised),
            "reasons": list(self.reasons),
        }


def _module_of(path: str) -> str | None:
    """`backend/app/risk/engine.py` -> `app.risk.engine`."""
    p = path.replace("\\", "/")
    marker = "backend/app/"
    if marker not in p:
        return None
    tail = p.split(marker, 1)[1]
    if not tail.endswith(".py"):
        return None
    tail = tail[: -len(".py")]
    if tail.endswith("/__init__"):
        tail = tail[: -len("/__init__")]
    return "app." + tail.replace("/", ".")


def _exempt(path: str) -> bool:
    p = path.replace("\\", "/")
    if any(p.startswith(x) for x in EXEMPT_PREFIXES):
        return True
    # A test file changing does not decertify: tests are the evidence, and a
    # test that changed is caught by running it, which revalidation does anyway.
    if "/tests/" in p or p.startswith("tests/"):
        return True
    return any(p.endswith(x) for x in EXEMPT_SUFFIXES)


def _critical(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(p.startswith(c) or p == c.rstrip("/") for c in CRITICAL_PATHS)


def analyse(changed: list[str] | tuple[str, ...]) -> ImpactAssessment:
    """What this set of changed files does to the certification.

    Written worst-first, because an assessment that averaged its inputs would
    let one critical path be diluted by nine documentation edits.
    """
    changed = tuple(sorted(changed))
    reasons: list[str] = []

    considered = [c for c in changed if not _exempt(c)]
    if not considered:
        return ImpactAssessment(
            Impact.NONE,
            changed,
            (),
            (),
            (),
            (
                "every changed path is documentation, a view, a report or a test. "
                "None of them can alter what the platform does at a venue.",
            ),
        )

    # 1. Anything under a critical path suspends autonomy.
    critical = tuple(c for c in considered if _critical(c))
    if critical:
        reasons.append(
            f"{', '.join(critical)} is a path every other judgement rests on. Autonomy "
            "is suspended until revalidation, rather than the change being reasoned "
            "about: a diff here means the platform is running code that has not been "
            "certified"
        )

    # 2. Map the rest onto invariants through the registry.
    invariants: set[str] = set()
    unrecognised: list[str] = []
    for path in considered:
        module = _module_of(path)
        if module is None:
            unrecognised.append(path)
            continue
        hit = [
            i.id
            for i in INVARIANTS
            if i.status is InvariantStatus.ENFORCED
            and (i.enforced_by == module or module.startswith(i.enforced_by + "."))
        ]
        if hit:
            invariants.update(hit)
        else:
            unrecognised.append(path)

    gates = tuple(sorted(g.id for g in GATES if set(g.invariants) & invariants))

    if invariants:
        reasons.append(
            f"{len(invariants)} enforced invariant(s) are implemented by changed modules"
        )
    if unrecognised:
        reasons.append(
            f"{len(unrecognised)} changed path(s) map to no invariant: "
            f"{', '.join(sorted(unrecognised)[:5])}"
            + ("…" if len(unrecognised) > 5 else "")
            + ". Unrecognised is treated as HIGH impact, never as none -- a path nobody "
            "classified is a path nobody thought about"
        )

    if critical:
        impact = Impact.SUSPEND_AUTONOMY
    elif unrecognised:
        impact = Impact.FULL_REVALIDATION
    elif any(
        next(i for i in INVARIANTS if i.id == inv).severity is Severity.CRITICAL
        for inv in invariants
    ):
        impact = Impact.FULL_REVALIDATION
        reasons.append("a CRITICAL invariant is affected, so revalidation is not targeted")
    else:
        impact = Impact.TARGETED_REVALIDATION

    return ImpactAssessment(
        impact,
        changed,
        tuple(sorted(invariants)),
        gates,
        tuple(sorted(unrecognised)),
        tuple(reasons),
    )


#: L63 section 11. What each trigger requires.
#:
#: A mapping rather than a function so it can be read as a table and audited
#: against the brief. Everything not named here is not "no action" -- callers
#: use `.get(trigger, Impact.FULL_REVALIDATION)`, and `trigger_impact` below
#: does that so the fail-closed default is not something each caller has to
#: remember.
TRIGGERS: dict[str, Impact] = {
    "POLICY_CHANGED": Impact.FULL_REVALIDATION,
    "RISK_RULE_CHANGED": Impact.SUSPEND_AUTONOMY,
    "OMS_CHANGED": Impact.SUSPEND_AUTONOMY,
    "BROKER_ADAPTER_CHANGED": Impact.SUSPEND_AUTONOMY,
    "MT5_CONFIGURATION_CHANGED": Impact.FULL_REVALIDATION,
    "MODEL_CHANGED": Impact.TARGETED_REVALIDATION,
    "STRATEGY_CHANGED": Impact.TARGETED_REVALIDATION,
    "PORTFOLIO_POLICY_CHANGED": Impact.FULL_REVALIDATION,
    "SECURITY_CHANGE": Impact.FULL_REVALIDATION,
    "DATABASE_SCHEMA_CHANGE": Impact.FULL_REVALIDATION,
    "EXECUTION_BEHAVIOR_DEGRADATION": Impact.SUSPEND_AUTONOMY,
    "CONTROL_LOOP_ANOMALY": Impact.SUSPEND_AUTONOMY,
    "CRITICAL_INVARIANT_FAILURE": Impact.SUSPEND_AUTONOMY,
    "CERTIFICATION_EXPIRY": Impact.FULL_REVALIDATION,
    "DOCUMENTATION_CHANGED": Impact.NONE,
    "FRONTEND_CHANGED": Impact.NONE,
}


def trigger_impact(trigger: str) -> Impact:
    """What a named trigger requires. **An unknown trigger is not NONE.**"""
    return TRIGGERS.get(trigger, Impact.FULL_REVALIDATION)


__all__ = [
    "CRITICAL_PATHS",
    "EXEMPT_PREFIXES",
    "TRIGGERS",
    "Impact",
    "ImpactAssessment",
    "analyse",
    "trigger_impact",
]
