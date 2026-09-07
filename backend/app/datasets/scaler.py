"""Normalisation that cannot be fitted on data it must not see.

Section 17 and section 58. Fitting a scaler on the whole dataset leaks: the
mean and standard deviation of the test period are future information, and
every training row is then standardised against numbers that could not have
been known. The leak is invisible in every metric — the model simply scores
better than it should, and nothing in the output says why.

**The API is the guarantee.** `fit()` takes rows *and a split*, and reads only
`split.train`. There is no function here that takes a whole dataset and returns
a scaler, so the mistake this module exists to prevent cannot be expressed.
`transform()` then applies the stored parameters to any segment.

A scaler carries its own `feature_version` and the row range it was fitted on,
because section 14 of the next level forbids pairing a model with the wrong
preprocessing and that check needs something to compare.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.datasets.splits import Split


class ScalerError(Exception):
    """A scaler that cannot be fitted or applied. Never silently skipped."""


@dataclass(frozen=True)
class Scaler:
    """Per-feature mean and standard deviation, fitted on training rows only."""

    feature_version: str
    means: dict[str, float]
    deviations: dict[str, float]
    fitted_rows: tuple[int, int]
    fitted_on: str = "train"
    # Features whose training values never varied. Standardising by a zero
    # deviation is a division by zero, and substituting 1.0 silently would turn
    # a constant into a signal, so they are named and passed through unchanged.
    constant_features: tuple[str, ...] = ()

    def transform(self, rows: list[dict[str, float | None]]) -> list[dict[str, float | None]]:
        """Apply the fitted parameters. Nothing here updates them."""
        out: list[dict[str, float | None]] = []
        for row in rows:
            scaled: dict[str, float | None] = {}
            for name, value in row.items():
                if name not in self.means:
                    scaled[name] = value
                    continue
                if value is None:
                    scaled[name] = None
                    continue
                deviation = self.deviations[name]
                scaled[name] = value if deviation == 0.0 else (value - self.means[name]) / deviation
            out.append(scaled)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "feature_version": self.feature_version,
            "fitted_on": self.fitted_on,
            "fitted_rows": list(self.fitted_rows),
            "means": {k: round(v, 12) for k, v in sorted(self.means.items())},
            "deviations": {k: round(v, 12) for k, v in sorted(self.deviations.items())},
            "constant_features": list(self.constant_features),
            "guarantee": (
                "fitted on the training segment only; validation and test are transformed "
                "with these parameters and never contribute to them"
            ),
        }


def fit(
    rows: list[dict[str, float | None]],
    split: Split,
    *,
    feature_version: str,
    features: tuple[str, ...],
) -> Scaler:
    """Fit on `split.train` and nothing else.

    The split is a required argument rather than an optional one precisely so
    that "fit on everything" has no spelling. A caller who genuinely wants a
    scaler over the whole series has to construct a split that says so, which
    is then visible in `fitted_rows` and caught by the leakage check.
    """
    start, stop = split.train
    if stop <= start:
        raise ScalerError("the training segment is empty; there is nothing to fit on")
    if stop > len(rows):
        raise ScalerError(
            f"the training segment ends at row {stop} but only {len(rows)} rows were given"
        )

    means: dict[str, float] = {}
    deviations: dict[str, float] = {}
    constant: list[str] = []

    for name in features:
        values = [row[name] for row in rows[start:stop] if row.get(name) is not None]
        sample = [v for v in values if v is not None]
        if len(sample) < 2:
            raise ScalerError(
                f"feature {name!r} has {len(sample)} usable training values; a standard "
                "deviation needs at least two, and inventing one would be the leak this "
                "module exists to prevent wearing a different hat"
            )
        mean = sum(sample) / len(sample)
        variance = sum((v - mean) ** 2 for v in sample) / (len(sample) - 1)
        deviation = math.sqrt(variance)
        means[name] = mean
        deviations[name] = deviation
        if deviation == 0.0:
            constant.append(name)

    return Scaler(
        feature_version=feature_version,
        means=means,
        deviations=deviations,
        fitted_rows=(start, stop),
        constant_features=tuple(constant),
    )


def refit_changes_nothing(
    scaler: Scaler,
    rows: list[dict[str, float | None]],
    split: Split,
    *,
    features: tuple[str, ...],
) -> bool:
    """True when adding validation and test rows would not move the parameters.

    Used by the leakage check rather than by the pipeline: it re-fits on the
    training segment of a longer row list and compares. If a scaler's numbers
    move when rows after the training boundary appear, something fitted on more
    than the training segment.
    """
    again = fit(rows, split, feature_version=scaler.feature_version, features=features)
    for name in features:
        if not math.isclose(again.means[name], scaler.means[name], rel_tol=1e-12, abs_tol=1e-15):
            return False
        if not math.isclose(
            again.deviations[name], scaler.deviations[name], rel_tol=1e-12, abs_tol=1e-15
        ):
            return False
    return True
