"""What must be true before a fit is allowed to start.

Section 7. Every check here reads something L23 already measured rather than
re-measuring it: the dataset's own quality report, its leakage findings, its
split, its class balance. A second implementation of "is this data usable"
would be a second answer, and the one nobody looked at would be the one a model
got trained on.

**A dataset that is not READY cannot be trained on.** That is the whole gate in
one sentence. L23's builder marks a dataset READY only when every leakage check
passes, and it cannot be overridden by any route — so "the data is
leakage-tested" is a fact this module can rely on rather than re-establish.

**Nothing is cleaned here.** Section 7 says never silently clean away important
problems, and this module has no write path to anything. It returns findings; a
blocking one refuses the job.

**A refusal names the check.** A caller told "data quality failed" learns
nothing; a caller told "the WIN share is 0.97, so a constant predictor scores
0.97 accuracy" knows what to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.datasets.builder import Dataset, DatasetStatus
from app.training.config import TrainingConfig


@dataclass(frozen=True)
class Gate:
    check: str
    passed: bool
    blocking: bool
    detail: str
    evidence: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "passed": self.passed,
            "blocking": self.blocking,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class GateReport:
    gates: list[Gate]

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates if g.blocking)

    def failures(self) -> list[Gate]:
        return [g for g in self.gates if g.blocking and not g.passed]

    def warnings(self) -> list[Gate]:
        return [g for g in self.gates if not g.blocking and not g.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": len(self.gates),
            "blocking_failures": len(self.failures()),
            "warnings": len(self.warnings()),
            "gates": [g.as_dict() for g in self.gates],
            "consequence": (
                "a blocking failure refuses the training job. Nothing is cleaned here: "
                "this module reads what L23 already measured and has no write path."
            ),
        }


def evaluate(
    dataset: Dataset, config: TrainingConfig, *, targets: list[int] | None = None
) -> GateReport:
    """Every gate, in one pass. Blocking failures refuse the run."""
    gates: list[Gate] = []

    # --- the dataset's own verdict --------------------------------------
    gates.append(
        Gate(
            check="dataset_is_ready",
            passed=dataset.status is DatasetStatus.ready,
            blocking=True,
            detail=(
                "READY"
                if dataset.status is DatasetStatus.ready
                else f"the dataset is {dataset.status}: {dataset.blocked_reason}. L23 marks "
                "a dataset READY only when every leakage check passes, and training on "
                "one that did not would make the whole check ceremonial."
            ),
            evidence={"status": str(dataset.status)},
        )
    )
    gates.append(
        Gate(
            check="leakage_report_passed",
            passed=dataset.leakage.passed,
            blocking=True,
            detail=(
                "every leakage check passed"
                if dataset.leakage.passed
                else "; ".join(f"{f.check}: {f.detail}" for f in dataset.leakage.failures())
            ),
            evidence={"failed": len(dataset.leakage.failures())},
        )
    )
    gates.append(
        Gate(
            check="series_quality_usable",
            passed=dataset.quality.usable,
            blocking=True,
            detail=(
                f"quality score {dataset.quality.score:.3f}"
                if dataset.quality.usable
                else "; ".join(dataset.quality.blocking)
            ),
            evidence={
                "score": round(dataset.quality.score, 4),
                "warnings": list(dataset.quality.warnings),
            },
        )
    )

    # --- shape ----------------------------------------------------------
    rows = len(dataset.rows)
    gates.append(
        Gate(
            check="sufficient_sample",
            passed=rows >= config.minimum_rows,
            blocking=True,
            detail=(
                f"{rows} usable rows against a {config.minimum_rows}-row minimum"
                if rows >= config.minimum_rows
                else f"{rows} usable rows is below the configured minimum of "
                f"{config.minimum_rows}. Fitting anyway produces coefficients "
                "indistinguishable from real ones, which is the failure this floor exists "
                "to prevent."
            ),
            evidence={"rows": rows, "minimum": config.minimum_rows, "dropped": dataset.dropped},
        )
    )
    gates.append(
        Gate(
            check="chronologically_ordered",
            passed=all(
                dataset.rows[i].at >= dataset.rows[i - 1].at for i in range(1, len(dataset.rows))
            ),
            blocking=True,
            detail=(
                "every row is at or after the one before it. A split of unordered rows "
                "is a random split wearing a chronological name."
            ),
        )
    )
    gates.append(
        Gate(
            check="split_exists",
            passed=dataset.split is not None,
            blocking=True,
            detail=(
                "train, validation and test are cut chronologically"
                if dataset.split
                else "the dataset carries no split, so there is no protected holdout"
            ),
            evidence=dataset.split.as_dict() if dataset.split else None,
        )
    )

    # --- features -------------------------------------------------------
    available = set(dataset.rows[0].features) if dataset.rows else set()
    requested = set(config.features) or available
    missing = sorted(requested - available)
    gates.append(
        Gate(
            check="features_available",
            passed=not missing,
            blocking=True,
            detail=(
                f"{len(requested)} requested features are all present"
                if not missing
                else f"the dataset does not carry {', '.join(missing)}. A model cannot be "
                "fitted on a feature that is not there, and substituting one is what "
                "every layer of this pipeline exists to refuse."
            ),
            evidence={"missing": missing, "available": sorted(available)},
        )
    )

    # --- labels ---------------------------------------------------------
    if targets is not None:
        positives = sum(targets)
        n = len(targets)
        share = positives / n if n else 0.0
        both_present = 0 < positives < n
        gates.append(
            Gate(
                check="both_classes_present",
                passed=both_present,
                blocking=True,
                detail=(
                    f"{positives} positive of {n}"
                    if both_present
                    else f"{positives} positive of {n}: one class is absent, so there is "
                    "nothing to separate. A fit on a single class produces a constant."
                ),
                evidence={"positive": positives, "total": n},
            )
        )
        # Reported, not corrected. Section 39 of L23's brief and section 15 of
        # this one both say document the distribution first.
        gates.append(
            Gate(
                check="class_balance",
                passed=0.05 <= share <= 0.95,
                blocking=False,
                detail=(
                    f"the positive class is {share:.1%} of rows"
                    + (
                        ""
                        if 0.05 <= share <= 0.95
                        else f". A constant predictor scores {max(share, 1 - share):.1%} "
                        "accuracy on this, so accuracy alone will say almost nothing"
                    )
                ),
                evidence={"positive_share": round(share, 6), "class_weight": config.class_weight},
            )
        )

    # --- versions -------------------------------------------------------
    manifest_config = dataset.manifest().get("config", {})
    gates.append(
        Gate(
            check="versions_locked",
            passed=bool(dataset.fingerprint),
            blocking=True,
            detail=(
                "the dataset carries a fingerprint over its configuration and its bars, "
                "so the run can prove afterwards that the data did not move under it"
            ),
            evidence={
                "dataset_fingerprint": dataset.fingerprint,
                "feature_set_version": manifest_config.get("feature_set_version"),
                "label_set_version": manifest_config.get("label_set_version"),
            },
        )
    )

    return GateReport(gates)
