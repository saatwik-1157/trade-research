"""Which stresses to run, and what running them has actually established. **L67.**

**The most dangerous thing this module could produce is a coverage report that
says COVERED.**

Every other artefact in the safety stack refuses something. This one makes a
positive claim -- *the portfolio has been tested against that* -- and a
positive claim is the kind that gets quoted. A stress orchestrator that reports
green without having run anything does not merely fail to help; it
manufactures exactly the confidence the rest of this platform exists to avoid.

So coverage is **earned, never assumed**:

* `UNKNOWN` is the default and is not `COVERED`. Section 7 says so, and
  `is_covered` returns True for one value only.
* There is no path from "no run" to `COVERED`. `matrix()` derives status from
  runs that were actually recorded, and a dimension nobody exercised comes back
  `UNCOVERED` rather than absent from the report.
* A run goes `STALE`. A stress result is a statement about the portfolio that
  was tested, and portfolios change.

Read honestly against this deployment, the matrix comes back almost entirely
`UNCOVERED`. That is the correct answer and it is the useful one.

**It orchestrates; it does not execute.** Section 44: stress must never become
auto-execution. Nothing here reaches the RiskEngine, the OMS or an adapter, and
nothing under `app/` imports it -- both asserted by tests. It reuses L66's
`ScenarioDefinition`, `Recommendation` and risk matrix rather than defining
second copies.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.portfolio.scenario import (
    Confidence,
    Recommendation,
    ScenarioDefinition,
    ScenarioKind,
    Severity,
    classify,
)


class StressClass(StrEnum):
    """L67 section 4. Mapped onto L66's `ScenarioKind` rather than replacing it."""

    MARKET = "MARKET_STRESS"
    VOLATILITY = "VOLATILITY_STRESS"
    CORRELATION = "CORRELATION_STRESS"
    LIQUIDITY = "LIQUIDITY_STRESS"
    SPREAD = "SPREAD_STRESS"
    SLIPPAGE = "SLIPPAGE_STRESS"
    MARGIN = "MARGIN_STRESS"
    DRAWDOWN = "DRAWDOWN_STRESS"
    REGIME = "REGIME_STRESS"
    STRATEGY = "STRATEGY_STRESS"
    MODEL = "MODEL_STRESS"
    EXECUTION = "EXECUTION_STRESS"
    BROKER = "BROKER_STRESS"
    INFRASTRUCTURE = "INFRASTRUCTURE_STRESS"
    COMBINED = "COMBINED_STRESS"
    CASCADING = "CASCADING_STRESS"


#: Which L66 scenario kind each stress class runs as. The mapping exists so
#: this module adds a vocabulary for *scheduling* without adding a second
#: schema for *scenarios* -- section 4 says not to duplicate scenario schemas.
KIND_OF: dict[StressClass, ScenarioKind] = {
    StressClass.MARKET: ScenarioKind.STRESS,
    StressClass.VOLATILITY: ScenarioKind.VOLATILITY,
    StressClass.CORRELATION: ScenarioKind.CORRELATION,
    StressClass.LIQUIDITY: ScenarioKind.LIQUIDITY,
    StressClass.SPREAD: ScenarioKind.LIQUIDITY,
    StressClass.SLIPPAGE: ScenarioKind.EXECUTION,
    StressClass.MARGIN: ScenarioKind.STRESS,
    StressClass.DRAWDOWN: ScenarioKind.STRESS,
    StressClass.REGIME: ScenarioKind.REGIME,
    StressClass.STRATEGY: ScenarioKind.STRATEGY_FAILURE,
    StressClass.MODEL: ScenarioKind.MODEL_FAILURE,
    StressClass.EXECUTION: ScenarioKind.EXECUTION,
    StressClass.BROKER: ScenarioKind.BROKER_FAILURE,
    StressClass.INFRASTRUCTURE: ScenarioKind.INFRASTRUCTURE_FAILURE,
    StressClass.COMBINED: ScenarioKind.STRESS,
    StressClass.CASCADING: ScenarioKind.STRESS,
}


class Coverage(StrEnum):
    """L67 section 7. What running a stress has established.

    **`UNKNOWN` is not `COVERED`.** The brief says so and it is the only rule
    in this module that matters more than any other: a coverage matrix is a
    positive claim, and a positive claim made from an absence is the failure
    this whole platform is built against.
    """

    COVERED = "COVERED"
    PARTIALLY_COVERED = "PARTIALLY_COVERED"
    UNCOVERED = "UNCOVERED"
    #: It was covered, and the result is old enough to be about a different
    #: portfolio. Not COVERED, and not UNCOVERED either -- the distinction
    #: tells an operator whether to run it or to re-run it.
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"

    @property
    def is_covered(self) -> bool:
        """One value returns True. Deliberately not four."""
        return self is Coverage.COVERED


#: How long a stress result stands before it describes a portfolio that may no
#: longer exist. Configuration, not measurement: no stress has ever been run
#: here. Recorded in `STRESS_COVERAGE_POLICY.md` as requiring approval.
DEFAULT_RESULT_TTL = timedelta(days=7)


@dataclass(frozen=True)
class StressRun:
    """One stress that actually ran. The only thing that creates coverage."""

    stress_class: StressClass
    account_id: str
    at: datetime
    #: False when the run itself failed. A stress that crashed covers nothing --
    #: recording it as a run would turn an outage into evidence.
    completed: bool = True
    scenario_fingerprint: str = ""
    confidence: Confidence = field(default_factory=Confidence)


def coverage_of(
    runs: tuple[StressRun, ...],
    stress_class: StressClass,
    account_id: str,
    *,
    now: datetime,
    ttl: timedelta = DEFAULT_RESULT_TTL,
) -> Coverage:
    """What this account's coverage of this stress class actually is.

    Account-scoped throughout. Section 26: a stress result for account A says
    nothing about account B, and a matrix that pooled them would report
    coverage the fragile account does not have.
    """
    mine = [
        r
        for r in runs
        if r.stress_class is stress_class and r.account_id == account_id and r.completed
    ]
    if not mine:
        return Coverage.UNCOVERED
    newest = max(r.at for r in mine)
    if now - newest > ttl:
        return Coverage.STALE
    # A run with no stated confidence has not established what it appears to.
    if all(r.confidence.is_low() for r in mine):
        return Coverage.PARTIALLY_COVERED
    return Coverage.COVERED


@dataclass(frozen=True)
class CoverageMatrix:
    """L67 section 7. What has been tested, per account."""

    account_id: str
    at: datetime
    cells: dict[StressClass, Coverage]

    def covered(self) -> tuple[StressClass, ...]:
        return tuple(k for k, v in self.cells.items() if v.is_covered)

    def gaps(self) -> tuple[StressClass, ...]:
        return tuple(k for k, v in self.cells.items() if not v.is_covered)

    def fraction_covered(self) -> Decimal:
        if not self.cells:
            return Decimal("0")
        return Decimal(len(self.covered())) / Decimal(len(self.cells))

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "at": self.at.isoformat(),
            "cells": {k.value: v.value for k, v in self.cells.items()},
            "covered": [k.value for k in self.covered()],
            "gaps": [k.value for k in self.gaps()],
            "fraction_covered": str(self.fraction_covered()),
            "authority": (
                "A record of what has been tested, not a statement that the portfolio "
                "is safe. Coverage is earned by a completed run and is never assumed."
            ),
        }


def matrix(
    runs: tuple[StressRun, ...],
    account_id: str,
    *,
    now: datetime,
    ttl: timedelta = DEFAULT_RESULT_TTL,
) -> CoverageMatrix:
    """Every stress class, always. A dimension nobody ran is reported UNCOVERED
    rather than left out -- a matrix that only lists what was tested makes the
    untested invisible, which is the opposite of its job."""
    return CoverageMatrix(
        account_id=account_id,
        at=now,
        cells={cls: coverage_of(runs, cls, account_id, now=now, ttl=ttl) for cls in StressClass},
    )


@dataclass(frozen=True)
class Gap:
    """L67 section 8. A named hole in coverage."""

    gap_id: str
    stress_class: StressClass
    account_id: str
    severity: Severity
    evidence: str
    recommended_scenario: str
    priority: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "stress_class": self.stress_class.value,
            "account_id": self.account_id,
            "severity": self.severity.name,
            "evidence": self.evidence,
            "recommended_scenario": self.recommended_scenario,
            "priority": self.priority,
        }


# --------------------------------------------------------------- priority


def priority_of(
    definition: ScenarioDefinition,
    *,
    coverage: Coverage,
    exposed: bool = False,
    unresolved_warning: bool = False,
) -> int:
    """How urgently this should run. Higher first.

    **Severity leads and likelihood follows**, which is section 5's rule that
    prioritisation must not rest on predicted probability alone. The risk class
    already encodes both via L66's enumerated matrix -- reused rather than
    recomputed here, so a change to the matrix moves scheduling too.

    Being uncovered is worth more than being severe. A severe scenario that has
    already been run tells you less than a mild one that never has, because the
    first has an answer and the second has none.
    """
    score = int(classify(definition.likelihood, definition.severity)) * 10
    if not coverage.is_covered:
        score += 25
    if coverage is Coverage.UNKNOWN:
        # Worse than uncovered: nobody even knows whether it was run.
        score += 10
    if exposed:
        score += 15
    if unresolved_warning:
        score += 15
    return score


# ---------------------------------------------------- combinatorial control


#: Hard caps on combined-scenario generation. L67 sections 10 and 40.
#:
#: Configuration, not measurement. The numbers are deliberately small: the cost
#: of a cap that is too tight is a scenario somebody runs by hand, and the cost
#: of one too loose is the explosion this exists to prevent.
MAX_DIMENSIONS = 3
MAX_COMBINATIONS = 24
MAX_RECURSION_DEPTH = 2


class GenerationStop(StrEnum):
    """Why generation stopped. Never silent."""

    COMPLETE = "COMPLETE"
    DIMENSION_CAP = "DIMENSION_CAP"
    COMBINATION_CAP = "COMBINATION_CAP"
    RECURSION_CAP = "RECURSION_CAP"


@dataclass(frozen=True)
class Generated:
    combinations: tuple[tuple[StressClass, ...], ...]
    stop: GenerationStop
    reason: str

    @property
    def truncated(self) -> bool:
        return self.stop is not GenerationStop.COMPLETE


def combinations(
    classes: tuple[StressClass, ...],
    *,
    dimensions: int = 2,
    max_dimensions: int = MAX_DIMENSIONS,
    max_combinations: int = MAX_COMBINATIONS,
    depth: int = 0,
) -> Generated:
    """Bounded combined-scenario generation. Sections 9 and 10.

    **It stops loudly.** Every cap returns a `GenerationStop` naming which one
    was hit, because a generator that silently truncates produces a coverage
    report whose gaps look like decisions. Section 40 asks for limits; the part
    that makes them safe is that exceeding one is visible.

    The caps are checked *before* generating rather than after, so an
    over-large request costs nothing -- the explosion this prevents is one that
    happens during generation, not after it.
    """
    if depth > MAX_RECURSION_DEPTH:
        return Generated(
            (),
            GenerationStop.RECURSION_CAP,
            f"generation recursed to depth {depth}, above the limit of "
            f"{MAX_RECURSION_DEPTH}. A scenario that generates scenarios is a loop, "
            "and a loop that generates work is the one that does not stop on its own",
        )
    if dimensions > max_dimensions:
        return Generated(
            (),
            GenerationStop.DIMENSION_CAP,
            f"{dimensions} dimensions requested, above the limit of {max_dimensions}. "
            "A combination nobody can reason about is not a test, it is a number of "
            "runs",
        )

    total = _n_choose_k(len(classes), dimensions)
    if total > max_combinations:
        return Generated(
            (),
            GenerationStop.COMBINATION_CAP,
            f"{len(classes)} classes at {dimensions} dimensions is {total} "
            f"combinations, above the limit of {max_combinations}. Generation stopped "
            "before it ran rather than truncating after: a partial sweep reported as a "
            "sweep is the failure the cap exists to prevent",
        )

    return Generated(
        tuple(itertools.combinations(classes, dimensions)),
        GenerationStop.COMPLETE,
        f"{total} combinations of {dimensions} dimensions",
    )


def _n_choose_k(n: int, k: int) -> int:
    if k > n or k < 0:
        return 0
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


# ------------------------------------------------------------ cascade graph


class Evidence(StrEnum):
    """L67 section 11. How a causal claim is supported.

    The ordering matters: `OBSERVED` is the only one that rests on this
    platform having seen the thing happen. Section 11 says not to assume
    causality from correlation alone, and the way that rule is broken is an
    edge drawn because two things moved together and then read as a mechanism.
    """

    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    HYPOTHETICAL = "HYPOTHETICAL"


@dataclass(frozen=True)
class CascadeEdge:
    """One claimed "this leads to that", and what supports it."""

    frm: str
    to: str
    evidence: Evidence
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.frm,
            "to": self.to,
            "evidence": self.evidence.value,
            "note": self.note,
        }


#: The cascade this platform can describe. Section 11's example chain.
#:
#: **Every edge is HYPOTHETICAL**, and that is the honest label rather than a
#: placeholder. `OBSERVED` would require this platform to have watched the
#: cascade happen; it has 271 orders from an imported ledger, one open position
#: and 700 bars of one instrument. `INFERRED` would require a correlation
#: analysis, which `app/portfolio/exposure.py` has reported as unavailable
#: since L53 and still does.
#:
#: A graph drawn from a textbook and labelled OBSERVED would be the most
#: quotable false claim in the repository.
DEFAULT_CASCADE: tuple[CascadeEdge, ...] = (
    CascadeEdge(
        "market_volatility",
        "spread",
        Evidence.HYPOTHETICAL,
        "widely held; not measured on this broker's recorded spreads",
    ),
    CascadeEdge("spread", "slippage", Evidence.HYPOTHETICAL, "mechanism, not measurement"),
    CascadeEdge("slippage", "execution_quality", Evidence.HYPOTHETICAL, ""),
    CascadeEdge("execution_quality", "strategy_performance", Evidence.HYPOTHETICAL, ""),
    CascadeEdge("market_volatility", "margin", Evidence.HYPOTHETICAL, ""),
    CascadeEdge("margin", "portfolio_drawdown", Evidence.HYPOTHETICAL, ""),
    CascadeEdge("strategy_performance", "portfolio_drawdown", Evidence.HYPOTHETICAL, ""),
    CascadeEdge(
        "portfolio_drawdown",
        "risk_restriction",
        Evidence.OBSERVED,
        "this one IS observed: the daily-loss limit halts new entries, and "
        "tests/test_risk.py exercises it",
    ),
)


def observed_edges(edges: tuple[CascadeEdge, ...] = DEFAULT_CASCADE) -> tuple[CascadeEdge, ...]:
    return tuple(e for e in edges if e.evidence is Evidence.OBSERVED)


# ------------------------------------------------------------- resilience


class ResilienceStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"

    @property
    def blocks_autonomy(self) -> bool:
        """UNKNOWN blocks as hard as CRITICAL, as everywhere else here."""
        return self in (ResilienceStatus.CRITICAL, ResilienceStatus.UNKNOWN)


@dataclass(frozen=True)
class Resilience:
    """L67 sections 12 and 13. What stress established about robustness."""

    account_id: str
    at: datetime
    status: ResilienceStatus
    reasons: tuple[str, ...]
    coverage: CoverageMatrix
    hard_failure: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "at": self.at.isoformat(),
            "status": self.status.value,
            "blocks_autonomy": self.status.blocks_autonomy,
            "reasons": list(self.reasons),
            "coverage": self.coverage.as_dict(),
            "hard_failure": self.hard_failure,
            "kind": "SIMULATION",
            "authority": (
                "A simulation result, not an observation and not an instruction. The "
                "RiskEngine remains the final veto and the OMS owns order state."
            ),
        }


def evaluate(
    *,
    account_id: str,
    now: datetime,
    coverage: CoverageMatrix,
    hard_failure: bool = False,
    degraded_dimensions: tuple[str, ...] = (),
    minimum_coverage: Decimal = Decimal("0.5"),
) -> Resilience:
    """Resilience, worst-first, with hard failure ahead of any score.

    **Section 13: hard safety violations override all numerical scores.** The
    check runs before anything is weighed rather than as the largest term in a
    sum -- a weighted score cannot express "this outranks everything", only
    "this counts for a lot".

    **Insufficient coverage is UNKNOWN, not HEALTHY.** This is the branch the
    module exists for. A portfolio nobody has stressed is not a robust
    portfolio; it is an unmeasured one, and reporting it as healthy would be
    the fabrication the coverage matrix is built to prevent.
    """
    reasons: list[str] = []

    if hard_failure:
        return Resilience(
            account_id,
            now,
            ResilienceStatus.CRITICAL,
            (
                "a hard safety condition failed under stress. This is not weighed "
                "against the coverage figure: a score cannot express 'this outranks "
                "everything', only 'this counts for a lot'",
            ),
            coverage,
            hard_failure=True,
        )

    fraction = coverage.fraction_covered()
    if fraction < minimum_coverage:
        reasons.append(
            f"only {fraction} of stress classes are covered, below the {minimum_coverage} "
            f"minimum. Uncovered: {', '.join(c.value for c in coverage.gaps())}. A "
            "portfolio nobody has stressed is unmeasured, not robust"
        )
        return Resilience(account_id, now, ResilienceStatus.UNKNOWN, tuple(reasons), coverage)

    if degraded_dimensions:
        reasons.append(f"degraded under stress: {', '.join(degraded_dimensions)}")
        return Resilience(account_id, now, ResilienceStatus.DEGRADED, tuple(reasons), coverage)

    reasons.append(
        f"{fraction} of stress classes covered by a recent completed run, and no dimension degraded"
    )
    return Resilience(account_id, now, ResilienceStatus.HEALTHY, tuple(reasons), coverage)


@dataclass(frozen=True)
class Bottleneck:
    """L67 section 15. A component that disproportionately reduces resilience."""

    component: str
    impact: str
    evidence: Evidence
    severity: Severity
    recommendation: Recommendation

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "impact": self.impact,
            "evidence": self.evidence.value,
            "severity": self.severity.name,
            "recommendation": self.recommendation.value,
            "authority": (
                "A recommendation. Section 15: the component is not removed automatically."
            ),
        }


__all__ = [
    "DEFAULT_CASCADE",
    "DEFAULT_RESULT_TTL",
    "KIND_OF",
    "MAX_COMBINATIONS",
    "MAX_DIMENSIONS",
    "MAX_RECURSION_DEPTH",
    "Bottleneck",
    "CascadeEdge",
    "Coverage",
    "CoverageMatrix",
    "Evidence",
    "Gap",
    "Generated",
    "GenerationStop",
    "Resilience",
    "ResilienceStatus",
    "StressClass",
    "StressRun",
    "combinations",
    "coverage_of",
    "evaluate",
    "matrix",
    "observed_edges",
    "priority_of",
]
