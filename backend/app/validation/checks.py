"""The checks, each answering for itself.

Section 27's shape: a report is a list of named verdicts, not a score. Each
function here returns one `Finding`, and `report.py` combines them by a rule
that cannot average a failure away.

**Every check can return BLOCKED.** Section 26 draws the distinction that most
of these exist to preserve: FAIL says the candidate is not good enough, BLOCKED
says we could not tell. A calibration check on 40 predictions has not measured
calibration; reporting PASS or FAIL from it would be a claim the sample cannot
support, so it reports BLOCKED and says why.

**Nothing here repairs anything.** Section 7: do not silently repair evaluation
data. These read what L23 already measured, what L25 already recorded, and what
the model itself declares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.base import BaseModel
from app.datasets.builder import Dataset, DatasetStatus
from app.monitoring.stats import expected_calibration_error, reliability_bins
from app.validation import economics, statistics
from app.validation.config import Severity, Thresholds


@dataclass(frozen=True)
class Finding:
    """One check's answer, with the evidence that produced it."""

    check: str
    severity: Severity
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": str(self.severity),
            "summary": self.summary,
            "evidence": self.evidence,
        }


def _finding(
    check: str,
    ok: bool,
    summary: str,
    evidence: dict[str, Any] | None = None,
    *,
    warn: bool = False,
) -> Finding:
    severity = Severity.passed if ok else (Severity.warning if warn else Severity.failed)
    return Finding(check, severity, summary, evidence or {})


def _blocked(check: str, why: str, evidence: dict[str, Any] | None = None) -> Finding:
    return Finding(check, Severity.blocked, why, evidence or {})


# =========================================================== data integrity


def data_integrity(dataset: Dataset, thresholds: Thresholds) -> Finding:
    """Section 7. Reads L23's own verdict rather than forming a second one."""
    if dataset.status is not DatasetStatus.ready:
        return _blocked(
            "data_integrity",
            f"the dataset is {dataset.status}, not READY: {dataset.blocked_reason}. "
            "Validation cannot be trusted on data the pipeline itself refused.",
            {"status": str(dataset.status)},
        )
    if not dataset.quality.usable:
        return _blocked(
            "data_integrity",
            "; ".join(dataset.quality.blocking),
            {"quality": dataset.quality.as_dict()},
        )
    ordered = all(dataset.rows[i].at >= dataset.rows[i - 1].at for i in range(1, len(dataset.rows)))
    if not ordered:
        return _finding("data_integrity", False, "rows are not in chronological order", {})
    return _finding(
        "data_integrity",
        True,
        f"READY, {len(dataset.rows)} rows, quality {dataset.quality.score:.3f}, "
        "chronologically ordered",
        {
            "rows": len(dataset.rows),
            "quality_score": round(dataset.quality.score, 4),
            "dropped": dict(dataset.dropped),
            "warnings": list(dataset.quality.warnings),
        },
    )


def sample_size(rows: int, trades: int, thresholds: Thresholds) -> Finding:
    """Section 24. Every report shows its n, and a tiny one is not a weak
    result — it is a number whose interval is wider than what it measures."""
    if rows < thresholds.minimum_samples:
        return _blocked(
            "sample_size",
            f"{rows} evaluation rows against a {thresholds.minimum_samples} minimum. "
            "A verdict from this many is a number with a confidence interval wider "
            "than the differences anyone would read from it.",
            {"rows": rows, "trades": trades},
        )
    if trades < thresholds.minimum_trades:
        return _finding(
            "sample_size",
            False,
            f"{rows} rows but only {trades} trades against a "
            f"{thresholds.minimum_trades} minimum. The ML metrics are supported; the "
            "economic ones are not.",
            {"rows": rows, "trades": trades},
            warn=True,
        )
    return _finding(
        "sample_size",
        True,
        f"{rows} rows and {trades} trades",
        {"rows": rows, "trades": trades},
    )


# ============================================================ model artifact


def model_artifact(model: BaseModel, dataset: Dataset, expected_feature_version: str) -> Finding:
    """Section 8. The artifact loads, predicts, and matches what it claims."""
    if not model.fitted:
        return _blocked(
            "model_artifact",
            f"{model.identity.key} v{model.identity.version} has no fitted parameters, "
            "so there is nothing to validate.",
            model.identity.as_dict(),
        )
    if not model.contract.accepts_version(expected_feature_version):
        return _finding(
            "model_artifact",
            False,
            f"the model was fitted against feature set {model.contract.feature_version} "
            f"and the evaluation data is {expected_feature_version}. A model evaluated "
            "on features it was not fitted on is being measured on a different quantity.",
            {
                "model_feature_version": model.contract.feature_version,
                "dataset_feature_version": expected_feature_version,
            },
        )
    available = set(dataset.rows[0].features) if dataset.rows else set()
    missing = sorted(set(model.contract.features) - available)
    if missing:
        return _finding(
            "model_artifact",
            False,
            f"the evaluation data does not carry {', '.join(missing)}, which the model "
            "requires. Nothing is substituted.",
            {"missing": missing},
        )
    return _finding(
        "model_artifact",
        True,
        f"{model.identity.key} v{model.identity.version} loads, declares "
        f"{len(model.contract.features)} features and matches the evaluation data",
        {**model.identity.as_dict(), "features": list(model.contract.features)},
    )


def version_locking(model: BaseModel, dataset: Dataset) -> Finding:
    """Section 6. Every dependency is an exact, immutable reference."""
    identity = model.identity
    missing = [
        name
        for name, value in (
            ("feature_version", identity.feature_version),
            ("dataset_version", identity.dataset_version),
            ("dataset_fingerprint", identity.dataset_fingerprint),
        )
        if not value
    ]
    if missing:
        return _finding(
            "version_locking",
            False,
            f"the candidate does not state {', '.join(missing)}. A report against "
            "dependencies that cannot be named cannot be checked afterwards.",
            {"missing": missing},
            warn=True,
        )
    trained_on = identity.dataset_fingerprint or ""
    if trained_on != dataset.fingerprint:
        return _blocked(
            "version_locking",
            f"the model was fitted on dataset {trained_on[:12]} and "
            f"is being validated on {dataset.fingerprint[:12]}. These are different "
            "data; the comparison would be meaningless.",
            {
                "trained_on": identity.dataset_fingerprint,
                "validating_on": dataset.fingerprint,
            },
        )
    return _finding(
        "version_locking",
        True,
        "model, feature set, label set and dataset are all named exactly, and the "
        "dataset fingerprint matches the one the model was fitted on",
        identity.as_dict(),
    )


# =================================================================== leakage


def leakage(dataset: Dataset) -> Finding:
    """Section 9. L23's six checks are the evidence; this reads their verdict.

    Re-running them would be a second implementation of the thing that has to
    be right, and the dataset cannot have reached READY with any of them
    failing — so a failure here means something changed after the build, which
    is worth blocking on rather than explaining.
    """
    report = dataset.leakage
    if not report.findings:
        return _blocked(
            "leakage",
            "the dataset carries no leakage report, so there is no evidence either way. "
            "A missing check is not a passing one.",
        )
    if not report.passed:
        return _finding(
            "leakage",
            False,
            "; ".join(f"{f.check}: {f.detail}" for f in report.failures()),
            report.as_dict(),
        )
    return _finding(
        "leakage",
        True,
        f"all {len(report.findings)} checks pass, including the section 56 test: "
        "features recomputed with future bars appended are byte-identical",
        {"checks": [f.check for f in report.findings]},
    )


def temporal(dataset: Dataset) -> Finding:
    """Sections 10 and 12. Train precedes validation precedes test, and the
    test segment was never consulted during training."""
    split = dataset.split
    if split is None:
        return _blocked("temporal", "the dataset carries no split, so there is no holdout")
    sizes = split.sizes()
    if min(sizes.values()) < 1:
        return _blocked("temporal", f"a segment is empty: {sizes}", sizes)
    times = [row.at for row in dataset.rows]
    train_end = times[split.train[1] - 1]
    validation_start = times[split.validation[0]]
    test_start = times[split.test[0]]
    if not (train_end < validation_start < test_start):
        return _finding(
            "temporal",
            False,
            "the segments overlap in time, so the holdout is not held out",
            {"train_ends": train_end.isoformat(), "test_starts": test_start.isoformat()},
        )
    return _finding(
        "temporal",
        True,
        f"chronological: train {sizes['train']} -> validation {sizes['validation']} -> "
        f"test {sizes['test']}, test begins {test_start.date()}",
        # `sizes` counts rows and `split` holds index bounds. Both use the key
        # `train`, so the split is nested rather than merged -- two different
        # quantities under one name is how a reader comes to compare them.
        {**sizes, "split": split.as_dict()},
    )


# ======================================================= baseline and metrics


def baseline_comparison(
    candidate: dict[str, Any], baseline: dict[str, Any], thresholds: Thresholds
) -> Finding:
    """Section 13. An improvement is not one metric moving."""
    if not candidate or not baseline:
        return _blocked(
            "baseline_comparison",
            "the training job recorded no baseline, so there is nothing to compare "
            "against. A candidate without a baseline is an unmeasured claim.",
        )
    log_loss_gain = (baseline.get("log_loss") or 0.0) - (candidate.get("log_loss") or 0.0)
    beats_majority = bool(candidate.get("beats_majority"))
    evidence = {
        "candidate_log_loss": candidate.get("log_loss"),
        "baseline_log_loss": baseline.get("log_loss"),
        "log_loss_improvement": round(log_loss_gain, 8),
        "required": thresholds.minimum_baseline_improvement,
        "candidate_accuracy": candidate.get("accuracy"),
        "majority_share": candidate.get("majority_share"),
        "beats_majority": beats_majority,
    }
    if log_loss_gain <= thresholds.minimum_baseline_improvement or not beats_majority:
        return _finding(
            "baseline_comparison",
            False,
            f"log loss improves by {log_loss_gain:.6f} against a required "
            f"{thresholds.minimum_baseline_improvement}, and beats_majority is "
            f"{beats_majority}. A candidate that cannot beat its own training prior has "
            "not learned anything about the market.",
            evidence,
        )
    return _finding(
        "baseline_comparison",
        True,
        f"log loss improves by {log_loss_gain:.6f} on the majority baseline and "
        "accuracy clears the majority share",
        evidence,
    )


def discrimination(auc: float, samples: int, thresholds: Thresholds) -> Finding:
    """Section 14. AUC, because accuracy alone is meaningless on an imbalanced
    label — and this project's labels are imbalanced by construction."""
    if samples < thresholds.minimum_samples:
        return _blocked(
            "discrimination",
            f"{samples} scored rows is below the {thresholds.minimum_samples} minimum",
            {"auc": round(auc, 6), "samples": samples},
        )
    if auc <= thresholds.minimum_auc:
        return _finding(
            "discrimination",
            False,
            f"AUC {auc:.4f} against a required {thresholds.minimum_auc}. The model does "
            "not rank a winning bar above a losing one better than chance.",
            {"auc": round(auc, 6), "samples": samples},
        )
    return _finding(
        "discrimination",
        True,
        f"AUC {auc:.4f} over {samples} rows",
        {"auc": round(auc, 6), "samples": samples},
    )


def calibration(probabilities: list[float], outcomes: list[int], thresholds: Thresholds) -> Finding:
    """Section 16. Measured, and a poor result is a WARNING rather than a FAIL.

    A miscalibrated model can still rank correctly, and ranking is what the AI
    seat uses it for — so this constrains how the number may be *read* rather
    than whether the model is usable. Saying otherwise would reject a usable
    filter for a property it is not being asked to have.
    """
    if len(probabilities) < thresholds.minimum_samples:
        return _blocked(
            "calibration",
            f"{len(probabilities)} predictions is too few to measure calibration; the "
            "figure would have a wider interval than the thing it describes",
            {"samples": len(probabilities)},
        )
    error = expected_calibration_error(probabilities, outcomes)
    evidence = {
        "expected_calibration_error": round(error, 6),
        "maximum": thresholds.maximum_calibration_error,
        "reliability": reliability_bins(probabilities, outcomes),
        "samples": len(probabilities),
    }
    if error > thresholds.maximum_calibration_error:
        return _finding(
            "calibration",
            False,
            f"expected calibration error {error:.4f} exceeds "
            f"{thresholds.maximum_calibration_error}. The ranking may still be useful, "
            "but the probability must not be read as a frequency: 0.80 does not mean "
            "80%.",
            evidence,
            warn=True,
        )
    return _finding(
        "calibration",
        True,
        f"expected calibration error {error:.4f}",
        evidence,
    )


# ================================================================== economic


def economic(summary: dict[str, Any], thresholds: Thresholds) -> Finding:
    """Section 17, through the project's own backtest engine."""
    trades = int(summary.get("trades") or 0)
    if trades == 0:
        return _blocked(
            "economic",
            summary.get("note")
            or "no trades were generated, so there is no economic result to report",
            {"entries_offered": summary.get("entries_offered", 0)},
        )
    if trades < thresholds.minimum_trades:
        return _blocked(
            "economic",
            f"{trades} trades is below the {thresholds.minimum_trades} minimum. A "
            "profit factor from this many is not a weak result, it is an unmeasured one.",
            {"trades": trades},
        )

    profit_factor = summary.get("profit_factor")
    drawdown = economics.drawdown_ratio(summary.get("rows") or [])
    evidence = {
        "trades": trades,
        "entries_offered": summary.get("entries_offered"),
        "profit_factor": profit_factor,
        "minimum_profit_factor": thresholds.minimum_profit_factor,
        "drawdown_ratio": round(drawdown, 6) if drawdown is not None else None,
        "maximum_drawdown_ratio": thresholds.maximum_drawdown_ratio,
        "engine": summary.get("engine"),
        "friction": summary.get("friction"),
    }
    if profit_factor is None or profit_factor < thresholds.minimum_profit_factor:
        return _finding(
            "economic",
            False,
            f"profit factor {profit_factor} against a required "
            f"{thresholds.minimum_profit_factor}, after the spread this broker actually "
            "charges. Cost drag is the one effect this repository has measured to "
            "significance.",
            evidence,
        )
    if drawdown is not None and drawdown > thresholds.maximum_drawdown_ratio:
        return _finding(
            "economic",
            False,
            f"the worst drawdown is {drawdown:.2f} of gross profit, above "
            f"{thresholds.maximum_drawdown_ratio}",
            evidence,
            warn=True,
        )
    return _finding(
        "economic",
        True,
        f"profit factor {profit_factor} over {trades} trades, through "
        "tools/rule_backtest.simulate with the spread charged",
        evidence,
    )


# ============================================== significance and walk-forward


def significance(result: statistics.PermutationResult | None, thresholds: Thresholds) -> Finding:
    """Section 23, and the gate this project actually trusts."""
    if result is None:
        return _blocked(
            "significance",
            "too few observations to permute, so no null was built. A missing null is "
            "not a passing one.",
        )
    evidence = result.as_dict()
    if not result.beats_null:
        return _finding(
            "significance",
            False,
            f"p = {result.p_value:.4f} against alpha {result.alpha}: the model does not "
            f"beat its own shuffled self. The null's best shuffle scored "
            f"{result.null_best:.4f} against the model's {result.observed:.4f}.",
            evidence,
        )
    if not result.beats_corrected:
        return _finding(
            "significance",
            False,
            f"p = {result.p_value:.4f} clears alpha {result.alpha} but not the "
            f"Bonferroni-corrected {result.corrected_alpha:.5f} for "
            f"{result.candidates_tried} candidates tried. A model selected from many "
            "runs and quoted uncorrected has been selected, not tested.",
            evidence,
            warn=True,
        )
    return _finding(
        "significance",
        True,
        f"p = {result.p_value:.4f}, below the corrected "
        f"{result.corrected_alpha:.5f} for {result.candidates_tried} candidate(s)",
        evidence,
    )


def walk_forward(windows: list[dict[str, Any]], thresholds: Thresholds) -> Finding:
    """Section 11. Aggregate performance is not the judgement; consistency is."""
    if len(windows) < thresholds.minimum_walk_forward_windows:
        return _blocked(
            "walk_forward",
            f"{len(windows)} windows against a "
            f"{thresholds.minimum_walk_forward_windows} minimum, so consistency cannot "
            "be assessed",
            {"windows": windows},
        )
    values = [w["metric"] for w in windows if w.get("metric") is not None]
    if not values:
        return _blocked("walk_forward", "no window produced a metric", {"windows": windows})

    positive = sum(1 for v in values if v > 0.5)
    mean = sum(values) / len(values)
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    variance = sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1)
    evidence = {
        "windows": len(values),
        "mean": round(mean, 6),
        "median": round(median, 6),
        "std": round(variance**0.5, 6),
        "worst": round(min(values), 6),
        "best": round(max(values), 6),
        "windows_above_chance": positive,
        "per_window": windows,
        "why": (
            "this repository's strongest-looking result held on a single split and "
            "dissolved under a walk-forward: re-ranked on only the eras before each "
            "test era, it was profitable in 1 fold of 4."
        ),
    }
    share = positive / len(values)
    if share < thresholds.minimum_stable_share:
        return _finding(
            "walk_forward",
            False,
            f"above chance in {positive} of {len(values)} windows ({share:.0%}), below "
            f"the required {thresholds.minimum_stable_share:.0%}. Performance "
            "concentrated in a few windows is an era, not an edge.",
            evidence,
            warn=True,
        )
    return _finding(
        "walk_forward",
        True,
        f"above chance in {positive} of {len(values)} windows, mean {mean:.4f}",
        evidence,
    )


# ========================================================== robustness, regime


def robustness(probes: list[dict[str, Any]], thresholds: Thresholds) -> Finding:
    """Section 22. A result that survives only one threshold is fitted to it."""
    if not probes:
        return _blocked("robustness", "no probe produced a result")
    stable = sum(1 for p in probes if p.get("stable"))
    share = stable / len(probes)
    evidence = {
        "probes": len(probes),
        "stable": stable,
        "share": round(share, 4),
        "required": thresholds.minimum_stable_share,
        "detail": probes,
    }
    if share < thresholds.minimum_stable_share:
        return _finding(
            "robustness",
            False,
            f"{stable} of {len(probes)} probes stayed profitable ({share:.0%}), below "
            f"{thresholds.minimum_stable_share:.0%}. A model whose edge disappears when "
            "the threshold moves a little is fitted to the threshold.",
            evidence,
            warn=True,
        )
    return _finding(
        "robustness",
        True,
        f"{stable} of {len(probes)} probes remain profitable under threshold and cost variation",
        evidence,
    )


def overfitting(
    train_metric: float | None, test_metric: float | None, thresholds: Thresholds
) -> Finding:
    """Section 19. A gap is a warning with evidence, not an automatic rejection."""
    if train_metric is None or test_metric is None:
        return _blocked(
            "overfitting",
            "the training job did not record both an in-sample and an out-of-sample "
            "figure, so degradation cannot be measured",
        )
    gap = train_metric - test_metric
    evidence = {
        "train": round(train_metric, 6),
        "test": round(test_metric, 6),
        "gap": round(gap, 6),
        "maximum": thresholds.maximum_train_test_gap,
    }
    if gap > thresholds.maximum_train_test_gap:
        return _finding(
            "overfitting",
            False,
            f"the model scores {train_metric:.4f} in sample and {test_metric:.4f} out "
            f"of it, a gap of {gap:.4f} above the configured "
            f"{thresholds.maximum_train_test_gap}",
            evidence,
            warn=True,
        )
    return _finding(
        "overfitting",
        True,
        f"in-sample {train_metric:.4f} against out-of-sample {test_metric:.4f}",
        evidence,
    )


def regime(breakdown: list[dict[str, Any]], thresholds: Thresholds) -> Finding:
    """Section 21. Reported by regime, and silent where the sample is too thin.

    A regime with nine trades gets its count printed and no verdict: section 21
    says not to claim generalisation from too few samples, and printing a win
    rate beside a count of nine invites exactly that claim.
    """
    if not breakdown:
        return _blocked(
            "regime",
            "no regime model is registered, so performance cannot be broken down by "
            "market state. Not a failure of the candidate.",
        )
    measured = [r for r in breakdown if r["trades"] >= thresholds.minimum_trades]
    evidence = {
        "regimes": breakdown,
        "measured": len(measured),
        "too_thin": [r["regime"] for r in breakdown if r["trades"] < thresholds.minimum_trades],
    }
    if not measured:
        return _blocked(
            "regime",
            "every regime has fewer trades than the minimum, so no regime-level claim "
            "is supportable",
            evidence,
        )
    losing = [r["regime"] for r in measured if (r.get("profit_factor") or 0) < 1.0]
    if losing:
        return _finding(
            "regime",
            False,
            f"loses money in {', '.join(losing)}. Performance concentrated in one "
            "market state is a regime bet, not a model.",
            evidence,
            warn=True,
        )
    return _finding(
        "regime",
        True,
        f"profitable in all {len(measured)} regimes with a supportable sample",
        evidence,
    )
