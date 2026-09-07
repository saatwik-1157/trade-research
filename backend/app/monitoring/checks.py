"""The eight monitoring checks.

Every check returns a Finding, and a check that cannot be answered returns
`insufficient_data` with the reason rather than a reassuring `ok`. "We did not
measure this" and "this is fine" are different claims, and only one of them
is safe to act on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.monitoring.findings import Check, Finding, Severity
from app.monitoring.stats import (
    MIN_SAMPLE,
    PSI_BAND_SOURCE,
    PSI_SERIOUS,
    PSI_WARN,
    benjamini_hochberg,
    categorical_psi,
    expected_calibration_error,
    ks_statistic,
    population_stability_index,
    welch_t,
)


def _insufficient(check: Check, subject: str, ref_n: int, cur_n: int) -> Finding:
    return Finding(
        check=check,
        subject=subject,
        severity=Severity.insufficient_data,
        summary=(
            f"{ref_n} reference and {cur_n} current observations; "
            f"below the {MIN_SAMPLE} needed before a statistic means anything"
        ),
        reference_n=ref_n,
        current_n=cur_n,
    )


def _psi_severity(psi: float) -> Severity:
    if psi >= PSI_SERIOUS:
        return Severity.serious
    if psi >= PSI_WARN:
        return Severity.warning
    return Severity.ok


def distribution_drift(
    check: Check,
    subject: str,
    reference: Sequence[float],
    current: Sequence[float],
    min_sample: int = MIN_SAMPLE,
) -> Finding:
    """PSI plus a KS test between a reference window and a current one."""
    if len(reference) < min_sample or len(current) < min_sample:
        return _insufficient(check, subject, len(reference), len(current))

    psi = population_stability_index(reference, current)
    d, p = ks_statistic(reference, current)
    severity = _psi_severity(psi)
    return Finding(
        check=check,
        subject=subject,
        severity=severity,
        summary=(
            f"PSI {psi:.3f} ({PSI_BAND_SOURCE}: warn {PSI_WARN}, serious {PSI_SERIOUS}); "
            f"KS D {d:.3f}, p {p:.4f}"
        ),
        statistic=psi,
        p_value=p,
        reference_n=len(reference),
        current_n=len(current),
        detail={"ks_d": d, "psi_warn": PSI_WARN, "psi_serious": PSI_SERIOUS},
    )


def feature_drift(
    reference: Mapping[str, Sequence[float]],
    current: Mapping[str, Sequence[float]],
    q: float = 0.05,
    min_sample: int = MIN_SAMPLE,
) -> list[Finding]:
    """Drift per feature, corrected for testing many features at once.

    Twenty features is twenty hypotheses; about one clears p<0.05 per run with
    nothing happening. The FDR verdict is attached to each finding and a
    feature that fails correction is demoted to `ok`, so the report cannot be
    read off the naive p-values.
    """
    names = sorted(set(reference) | set(current))
    findings = [
        distribution_drift(
            Check.feature_drift, name, reference.get(name, []), current.get(name, []), min_sample
        )
        for name in names
    ]

    testable = [f for f in findings if f.p_value is not None]
    if not testable:
        return findings
    survives = benjamini_hochberg([f.p_value or 1.0 for f in testable], q)
    corrected: dict[str, Finding] = {}
    for finding, survived in zip(testable, survives, strict=True):
        severity = finding.severity
        if not survived and severity in (Severity.warning, Severity.serious):
            severity = Severity.ok
        corrected[finding.subject] = Finding(
            **{
                **finding.__dict__,
                "severity": severity,
                "survives_fdr": survived,
                "summary": finding.summary
                + ("; survives FDR" if survived else "; does not survive FDR correction"),
            }
        )
    return [corrected.get(f.subject, f) for f in findings]


def prediction_drift(
    reference: Sequence[float], current: Sequence[float], min_sample: int = MIN_SAMPLE
) -> Finding:
    return distribution_drift(Check.prediction_drift, "predictions", reference, current, min_sample)


def prediction_distribution(predictions: Sequence[float], min_sample: int = MIN_SAMPLE) -> Finding:
    """Has the model collapsed to one answer?

    A model that outputs the same probability for everything has stopped
    discriminating, and drift against a reference will not always catch it -
    the reference may have been degenerate too.
    """
    n = len(predictions)
    if n < min_sample:
        return _insufficient(Check.prediction_distribution, "predictions", 0, n)
    lo, hi = min(predictions), max(predictions)
    spread = hi - lo
    mean = sum(predictions) / n
    variance = sum((p - mean) ** 2 for p in predictions) / n
    if spread < 1e-9:
        severity, note = Severity.critical, "every prediction is identical"
    elif variance < 1e-6:
        severity, note = Severity.serious, "predictions are nearly constant"
    else:
        severity, note = Severity.ok, "predictions vary"
    return Finding(
        check=Check.prediction_distribution,
        subject="predictions",
        severity=severity,
        summary=f"{note}: range {lo:.4f}-{hi:.4f}, variance {variance:.6f}",
        statistic=variance,
        current_n=n,
        detail={"min": lo, "max": hi, "mean": mean},
    )


def calibration(
    probabilities: Sequence[float],
    outcomes: Sequence[int],
    warn_at: float = 0.10,
    serious_at: float = 0.20,
    min_sample: int = MIN_SAMPLE,
) -> Finding:
    """Do predicted probabilities match observed frequencies?"""
    n = len(probabilities)
    if n != len(outcomes):
        raise ValueError("probabilities and outcomes differ in length")
    if n < min_sample:
        return _insufficient(Check.calibration, "probabilities", 0, n)
    ece = expected_calibration_error(probabilities, outcomes)
    severity = (
        Severity.serious
        if ece >= serious_at
        else Severity.warning
        if ece >= warn_at
        else Severity.ok
    )
    return Finding(
        check=Check.calibration,
        subject="probabilities",
        severity=severity,
        summary=f"expected calibration error {ece:.3f} over {n} predictions",
        statistic=ece,
        current_n=n,
        detail={"warn_at": warn_at, "serious_at": serious_at},
    )


def performance(
    reference_r: Sequence[float],
    current_r: Sequence[float],
    min_sample: int = MIN_SAMPLE,
) -> Finding:
    """Has realised performance fallen against a reference window?

    Measured in R multiples, because currency cannot be pooled across
    positions sized differently - the correction `track_record.py` already
    applies. Only a fall is a finding: an improvement is noise too, but it is
    not a reason to pause anything.
    """
    if len(reference_r) < min_sample or len(current_r) < min_sample:
        return _insufficient(Check.performance, "r_multiple", len(reference_r), len(current_r))

    ref_mean = sum(reference_r) / len(reference_r)
    cur_mean = sum(current_r) / len(current_r)
    t, p = welch_t(current_r, reference_r)
    fell = cur_mean < ref_mean
    if fell and p < 0.01:
        severity = Severity.serious
    elif fell and p < 0.05:
        severity = Severity.warning
    else:
        severity = Severity.ok
    return Finding(
        check=Check.performance,
        subject="r_multiple",
        severity=severity,
        summary=(
            f"mean R {cur_mean:+.4f} against reference {ref_mean:+.4f}; Welch t {t:+.2f}, p {p:.4f}"
        ),
        statistic=cur_mean - ref_mean,
        p_value=p,
        reference_n=len(reference_r),
        current_n=len(current_r),
        detail={"reference_mean_r": ref_mean, "current_mean_r": cur_mean, "t": t},
    )


def trade_outcomes(
    r_multiples: Sequence[float],
    consecutive_loss_limit: int = 10,
    min_sample: int = 30,
) -> Finding:
    """Realised outcomes: losing streak and share of losers.

    The sample floor is lower here on purpose. This is not a significance
    test - a run of ten losses is an operational fact worth surfacing whether
    or not it is statistically surprising.
    """
    n = len(r_multiples)
    if n < min_sample:
        return _insufficient(Check.trade_outcomes, "trades", 0, n)
    streak = worst_streak = 0
    for r in r_multiples:
        streak = streak + 1 if r < 0 else 0
        worst_streak = max(worst_streak, streak)
    losers = sum(1 for r in r_multiples if r < 0)
    severity = Severity.warning if worst_streak >= consecutive_loss_limit else Severity.ok
    return Finding(
        check=Check.trade_outcomes,
        subject="trades",
        severity=severity,
        summary=(
            f"{losers}/{n} losing trades, longest losing run {worst_streak} "
            f"(limit {consecutive_loss_limit})"
        ),
        statistic=float(worst_streak),
        current_n=n,
        detail={"losers": losers, "loss_rate": losers / n},
    )


def market_regime(
    reference_labels: Sequence[str],
    current_labels: Sequence[str],
    min_sample: int = MIN_SAMPLE,
) -> Finding:
    """Has the mix of market regimes shifted under the model?"""
    if len(reference_labels) < min_sample or len(current_labels) < min_sample:
        return _insufficient(
            Check.market_regime, "regime", len(reference_labels), len(current_labels)
        )
    psi = categorical_psi(reference_labels, current_labels)
    return Finding(
        check=Check.market_regime,
        subject="regime",
        severity=_psi_severity(psi),
        summary=f"regime mix PSI {psi:.3f} ({PSI_BAND_SOURCE})",
        statistic=psi,
        reference_n=len(reference_labels),
        current_n=len(current_labels),
    )


# ============================================ L29: the checks that were missing
#
# The eight above measure the MODEL. These four measure the model's SERVICE --
# how fast it answered, how often it failed, whether its confidence changed
# shape, and whether performance moved while the inputs did not.
#
# `latency` and `availability` deliberately have a lower sample floor than the
# distribution checks: a hundred timing observations is a distribution, and ten
# inference failures is an operational fact worth surfacing whether or not it is
# statistically surprising. That is the same reasoning `trade_outcomes` uses.


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Deterministic, and no dependency."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    index = min(int(q * len(ordered)), len(ordered) - 1)
    return ordered[index]


def latency(
    total_ms: Sequence[float],
    *,
    p95_warning_ms: float = 500.0,
    p95_critical_ms: float = 2000.0,
    min_sample: int = 30,
) -> Finding:
    """How long the AI layer took. Section 19.

    The p95 rather than the mean, because a mean hides the tail and the tail is
    what a time-sensitive strategy actually experiences. Reported with the
    median beside it, so a reader can see whether the tail is the whole story.

    **This is about the DISTRIBUTION over a window.** L27 already enforces a
    per-signal budget and applies the failure policy when one call exceeds it;
    this says whether the service as a whole has become slow.
    """
    values = [float(v) for v in total_ms if v is not None]
    n = len(values)
    if n < min_sample:
        return _insufficient(Check.latency, "total_ms", 0, n)

    p50 = _percentile(values, 0.50)
    p95 = _percentile(values, 0.95)
    p99 = _percentile(values, 0.99)
    severity = (
        Severity.critical
        if p95 >= p95_critical_ms
        else Severity.warning
        if p95 >= p95_warning_ms
        else Severity.ok
    )
    return Finding(
        check=Check.latency,
        subject="total_ms",
        severity=severity,
        summary=(
            f"p95 {p95:.1f}ms over {n} inferences (median {p50:.1f}ms, p99 {p99:.1f}ms); "
            f"bars {p95_warning_ms:.0f}/{p95_critical_ms:.0f}ms"
        ),
        statistic=p95,
        current_n=n,
        detail={
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
            "max": round(max(values), 3),
            "warning_ms": p95_warning_ms,
            "critical_ms": p95_critical_ms,
            "note": (
                "the p95, not the mean: a mean hides the tail, and the tail is what a "
                "time-sensitive strategy experiences. Latency never bypasses the risk "
                "engine -- L27 applies the configured failure policy instead."
            ),
        },
    )


def availability(
    statuses: Sequence[str],
    *,
    error_rate_warning: float = 0.05,
    error_rate_critical: float = 0.20,
    min_sample: int = 20,
) -> Finding:
    """How often the AI layer failed to answer. Section 20.

    `statuses` are L27's own: OK, MODEL_UNAVAILABLE, INCOMPATIBLE,
    FEATURES_UNAVAILABLE, INVALID_OUTPUT, LATENCY_EXCEEDED, NO_PROBABILITY,
    LOOKAHEAD_REFUSED, PIPELINE_ERROR, CONTEXT_ERROR, DISABLED.

    **DISABLED is not a failure and is excluded from the denominator.** A
    strategy configured AI_DISABLED did not fail to answer; it was never asked,
    and counting it as availability would make turning the AI off look like
    perfect uptime.
    """
    asked = [s for s in statuses if s != "DISABLED"]
    n = len(asked)
    if n < min_sample:
        return _insufficient(Check.availability, "inference", 0, n)

    failures = [s for s in asked if s != "OK"]
    rate = len(failures) / n
    by_status: dict[str, int] = {}
    for status in failures:
        by_status[status] = by_status.get(status, 0) + 1

    severity = (
        Severity.critical
        if rate >= error_rate_critical
        else Severity.warning
        if rate >= error_rate_warning
        else Severity.ok
    )
    return Finding(
        check=Check.availability,
        subject="inference",
        severity=severity,
        summary=(
            f"{len(failures)}/{n} inferences did not answer ({rate:.1%}); "
            f"bars {error_rate_warning:.0%}/{error_rate_critical:.0%}"
        ),
        statistic=rate,
        current_n=n,
        detail={
            "by_status": dict(sorted(by_status.items())),
            "excluded_disabled": len(statuses) - n,
            "note": (
                "AI_DISABLED runs are excluded from the denominator: a strategy that was "
                "never asked did not fail to answer, and counting it would make turning "
                "the AI off look like perfect uptime. Under AI_REQUIRED a failure means "
                "no trade, which L27 already enforces."
            ),
        },
    )


def confidence_drift(
    reference: Sequence[float],
    current: Sequence[float],
    *,
    extreme_at: float = 0.95,
    min_sample: int = MIN_SAMPLE,
) -> Finding:
    """Has the model's confidence changed shape? Section 10.

    A model that suddenly produces many near-certain predictions has changed
    behaviour whether or not it has become wrong, and §10 is explicit that this
    is a flag for investigation rather than a conclusion. So the summary reports
    the share of extreme predictions on both sides and does not say "the model
    is wrong" in any branch.
    """
    if len(reference) < min_sample or len(current) < min_sample:
        return _insufficient(Check.confidence_drift, "confidence", len(reference), len(current))

    psi = population_stability_index(reference, current)
    d, p = ks_statistic(reference, current)
    ref_extreme = sum(1 for v in reference if v >= extreme_at) / len(reference)
    cur_extreme = sum(1 for v in current if v >= extreme_at) / len(current)

    return Finding(
        check=Check.confidence_drift,
        subject="confidence",
        severity=_psi_severity(psi),
        summary=(
            f"confidence PSI {psi:.3f} ({PSI_BAND_SOURCE}); predictions at or above "
            f"{extreme_at:.2f}: {cur_extreme:.1%} now against {ref_extreme:.1%} in the "
            "reference. A change in confidence is a reason to investigate, not a "
            "conclusion that the model is wrong."
        ),
        statistic=psi,
        p_value=p,
        reference_n=len(reference),
        current_n=len(current),
        detail={
            "ks_d": d,
            "reference_extreme_share": round(ref_extreme, 6),
            "current_extreme_share": round(cur_extreme, 6),
            "extreme_at": extreme_at,
        },
    )


def possible_concept_drift(findings: Sequence[Finding]) -> Finding:
    """Did performance move while the INPUTS did not? Section 15.

    Concept drift is a change in the relationship between inputs and outcomes.
    It cannot be measured directly here, and §15 forbids claiming it when only
    input drift was observed — so this INFERS it from the one pattern that is
    actually suggestive: outcome-dependent checks degraded and the input
    distributions did not move.

    The reverse pattern — inputs moved, performance held — is explicitly NOT
    concept drift, and the summary says so, because that is the case a reader is
    most likely to misread.
    """
    inputs_moved = any(
        f.check in (Check.feature_drift, Check.feature_distribution) and f.actionable
        for f in findings
    )
    outcomes_moved = any(
        f.check in (Check.calibration, Check.performance) and f.actionable for f in findings
    )
    measured = [
        f
        for f in findings
        if f.check
        in (
            Check.feature_drift,
            Check.feature_distribution,
            Check.calibration,
            Check.performance,
        )
        and f.severity is not Severity.insufficient_data
    ]

    if not measured:
        return _insufficient(Check.possible_concept_drift, "relationship", 0, 0)

    if outcomes_moved and not inputs_moved:
        return Finding(
            check=Check.possible_concept_drift,
            subject="relationship",
            severity=Severity.warning,
            summary=(
                "performance or calibration degraded while the input distributions did "
                "not move. That is the signature of the relationship between inputs and "
                "outcomes changing rather than the world changing. POSSIBLE concept "
                "drift: investigation recommended, not a conclusion."
            ),
            current_n=len(measured),
            detail={"inputs_moved": False, "outcomes_moved": True},
        )

    return Finding(
        check=Check.possible_concept_drift,
        subject="relationship",
        severity=Severity.ok,
        summary=(
            "no concept-drift signature. "
            + (
                "The inputs moved and performance held, which is a market that changed "
                "rather than a model that broke -- and it is NOT concept drift."
                if inputs_moved
                else "Neither the inputs nor the outcome-dependent checks moved."
            )
        ),
        current_n=len(measured),
        detail={"inputs_moved": inputs_moved, "outcomes_moved": outcomes_moved},
    )
