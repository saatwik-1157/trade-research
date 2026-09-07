"""Automated leakage detection. A dataset that fails here cannot become READY.

Sections 37, 52, 56, 57 and 58. Leakage is the failure mode this whole level
exists to prevent, and it is the one that never announces itself: every metric
improves, nothing errors, and the model is worthless the first time it sees a
market it cannot already see the answer to.

The checks are deliberately behavioural rather than structural. It is easy to
assert that a module does not import `pandas.shift(-1)`; it is worth very
little. What these do instead is give the pipeline data it should be unable to
use and require that its output does not change:

  * **`no_future_influence`** — the section 56 test. Compute features over
    bars through T, append bars after T, recompute, and require every row at
    or before T to be identical. A rolling window that reached forward, a
    normalisation fitted over the whole array, a "centred" moving average —
    all of them move a value here and nothing else in the system would.

  * **`labels_are_not_features`** — a label name appearing in the feature
    columns is the most direct form of the mistake, and the most embarrassing
    one to find after training.

  * **`splits_are_ordered`** — every training row is strictly before every
    validation row, which is before every test row. Section 5's invariant.

  * **`scaler_fitted_on_train_only`** — re-fit and compare, so the claim is
    checked rather than trusted.

  * **`labels_have_a_future`** — no labelled row may sit within `horizon` bars
    of the end of the series. A label there was computed against data that does
    not exist.

  * **`feature_timestamps_are_causal`** — the feature row's own timestamp is
    the bar it belongs to, and no feature may be dated after it.

Each returns a `LeakageFinding`. `report()` runs all of them; `passed` is
false if any check failed, and `builder.py` refuses to mark a dataset READY
when it is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.datasets.scaler import Scaler, refit_changes_nothing
from app.datasets.splits import Split
from app.marketdata.types import Bar


@dataclass(frozen=True)
class LeakageFinding:
    check: str
    passed: bool
    detail: str
    evidence: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def no_future_influence(
    bars: list[Bar],
    compute: Callable[[list[Bar]], list[dict[str, float | None]]],
    *,
    cut: int | None = None,
) -> LeakageFinding:
    """Section 56, run literally.

    Features are computed twice: once over `bars[:cut]` and once over the whole
    series. Every row up to the cut must match exactly. `cut` defaults to
    two-thirds of the series so that the appended tail is long enough for a
    forward-reaching window of any plausible length to reach into it.

    Exact equality, not a tolerance. A causal calculation over identical input
    produces identical output; a difference of 1e-15 is still a difference, and
    treating it as noise is how a real leak survives its own test.
    """
    if len(bars) < 6:
        return LeakageFinding(
            "no_future_influence",
            False,
            f"{len(bars)} bars is too few to test: with nothing to append, the check "
            "would pass without having looked at anything.",
        )

    boundary = cut if cut is not None else (len(bars) * 2) // 3
    if not 1 <= boundary < len(bars):
        return LeakageFinding("no_future_influence", False, f"cut {boundary} is outside the series")

    prefix = compute(bars[:boundary])
    whole = compute(bars)

    for index in range(boundary):
        before, after = prefix[index], whole[index]
        if before != after:
            differing = {
                name: {"without_future": before.get(name), "with_future": after.get(name)}
                for name in set(before) | set(after)
                if before.get(name) != after.get(name)
            }
            return LeakageFinding(
                "no_future_influence",
                False,
                f"row {index} (bar {bars[index].bar_time.isoformat()}) changed when "
                f"{len(bars) - boundary} later bars were appended. A feature that moves "
                "when the future arrives read the future.",
                {"row": index, "features": differing},
            )

    return LeakageFinding(
        "no_future_influence",
        True,
        f"{boundary} rows computed identically with and without the {len(bars) - boundary} "
        "bars that follow them.",
        {"rows_compared": boundary, "bars_appended": len(bars) - boundary},
    )


def labels_are_not_features(
    feature_names: tuple[str, ...], label_names: tuple[str, ...]
) -> LeakageFinding:
    overlap = sorted(set(feature_names) & set(label_names))
    if overlap:
        return LeakageFinding(
            "labels_are_not_features",
            False,
            f"{len(overlap)} column(s) appear as both a feature and a label: "
            f"{', '.join(overlap)}. A label is the answer; a feature that is the answer "
            "trains a model to read it back.",
            {"columns": overlap},
        )
    return LeakageFinding(
        "labels_are_not_features",
        True,
        f"{len(feature_names)} feature columns and {len(label_names)} label columns, "
        "with no name in both.",
    )


def splits_are_ordered(times: list[datetime], split: Split) -> LeakageFinding:
    """Every training row before every validation row, before every test row."""
    segments = {
        "train": split.train,
        "validation": split.validation,
        "test": split.test,
    }
    bounds: dict[str, tuple[datetime, datetime]] = {}
    for name, (start, stop) in segments.items():
        if stop <= start:
            return LeakageFinding(
                "splits_are_ordered",
                False,
                f"the {name} segment is empty. A split with an empty holdout measures "
                "nothing, and a split with no training rows cannot be fitted.",
                {"segment": name, "range": [start, stop]},
            )
        bounds[name] = (times[start], times[stop - 1])

    order = ["train", "validation", "test"]
    for earlier, later in zip(order, order[1:], strict=False):
        if bounds[earlier][1] >= bounds[later][0]:
            return LeakageFinding(
                "splits_are_ordered",
                False,
                f"the {earlier} segment ends at {bounds[earlier][1].isoformat()}, at or "
                f"after the {later} segment begins at {bounds[later][0].isoformat()}. "
                "Overlapping segments make a holdout that is not held out.",
                {
                    f"{earlier}_ends": bounds[earlier][1].isoformat(),
                    f"{later}_starts": bounds[later][0].isoformat(),
                },
            )

    return LeakageFinding(
        "splits_are_ordered",
        True,
        "train -> validation -> test, strictly in time order.",
        {name: [a.isoformat(), b.isoformat()] for name, (a, b) in bounds.items()},
    )


def scaler_fitted_on_train_only(
    scaler: Scaler,
    rows: list[dict[str, float | None]],
    split: Split,
    *,
    features: tuple[str, ...],
) -> LeakageFinding:
    """Check the claim rather than trust it.

    `Scaler.fit` cannot see beyond the training segment by construction, so
    this passes for anything the pipeline built. It exists for the scaler that
    arrives from somewhere else, and to catch a `fitted_rows` that does not
    match the split it is being used with.
    """
    if tuple(scaler.fitted_rows) != tuple(split.train):
        return LeakageFinding(
            "scaler_fitted_on_train_only",
            False,
            f"the scaler was fitted on rows {list(scaler.fitted_rows)} but this split's "
            f"training segment is {list(split.train)}. A scaler and a split that "
            "disagree cannot both be right about which rows the model may see.",
            {"scaler_rows": list(scaler.fitted_rows), "split_train": list(split.train)},
        )
    if not refit_changes_nothing(scaler, rows, split, features=features):
        return LeakageFinding(
            "scaler_fitted_on_train_only",
            False,
            "re-fitting on the training segment produced different parameters, so the "
            "stored ones came from somewhere else -- the whole dataset, most likely.",
        )
    return LeakageFinding(
        "scaler_fitted_on_train_only",
        True,
        f"parameters for {len(features)} features reproduce exactly from rows "
        f"{list(split.train)} alone.",
    )


def labels_have_a_future(
    total_rows: int, labelled_indices: list[int], horizon: int
) -> LeakageFinding:
    """No labelled row may sit within `horizon` bars of the end."""
    last_safe = total_rows - horizon - 1
    offenders = [i for i in labelled_indices if i > last_safe]
    if offenders:
        return LeakageFinding(
            "labels_have_a_future",
            False,
            f"{len(offenders)} labelled row(s) are within {horizon} bars of the end of "
            "the series, so their outcome was computed against bars that do not exist.",
            {"rows": offenders[:20], "last_safe_row": last_safe},
        )
    return LeakageFinding(
        "labels_have_a_future",
        True,
        f"every labelled row is at or before row {last_safe}, leaving the full "
        f"{horizon}-bar horizon inside the series.",
    )


def feature_timestamps_are_causal(
    row_times: list[datetime], bar_times: list[datetime]
) -> LeakageFinding:
    """A feature row's timestamp is its own bar's, never a later one."""
    if len(row_times) != len(bar_times):
        return LeakageFinding(
            "feature_timestamps_are_causal",
            False,
            f"{len(row_times)} feature rows against {len(bar_times)} bars. Alignment is "
            "by index, so a length mismatch means some row is paired with the wrong bar.",
        )
    for index, (row_at, bar_at) in enumerate(zip(row_times, bar_times, strict=True)):
        if row_at != bar_at:
            return LeakageFinding(
                "feature_timestamps_are_causal",
                False,
                f"row {index} is stamped {row_at.isoformat()} against a bar at "
                f"{bar_at.isoformat()}.",
                {"row": index, "row_time": row_at.isoformat(), "bar_time": bar_at.isoformat()},
            )
    return LeakageFinding(
        "feature_timestamps_are_causal",
        True,
        f"{len(row_times)} rows each carry their own bar's open time.",
    )


@dataclass(frozen=True)
class LeakageReport:
    findings: list[LeakageFinding]

    @property
    def passed(self) -> bool:
        return all(f.passed for f in self.findings)

    def failures(self) -> list[LeakageFinding]:
        return [f for f in self.findings if not f.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": len(self.findings),
            "failed": len(self.failures()),
            "findings": [f.as_dict() for f in self.findings],
            "consequence": (
                "a failing check blocks the dataset at CLEAN; it cannot be marked READY "
                "and nothing downstream may train on it"
            ),
        }
