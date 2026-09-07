"""Chronological splitting, and the walk-forward folds built on it.

**Nothing here shuffles.** Section 26. A random split of time-series trading
data puts tomorrow in the training set and yesterday in the test set, and the
resulting score measures interpolation rather than prediction. There is no
`shuffle` parameter to set wrongly: the functions take an ordered series and
cut it, and the cut points are the only thing a caller controls.

**One split is not evidence, and this module says so in its output.** The
project has a worked example. `donchian_fade_55` was called
`holds_out_of_sample` by splits at 0.5, 0.7 and 0.85 — three readings of the
same recent era, counted three times, not three confirmations. The
walk-forward folds exist because that is what actually separated it: re-ranked
on only the eras before each test era, the search picked a different candidate
every time and was profitable in 1 fold of 4.

So `chronological()` returns a split that carries `single_split_warning`, and
`walk_forward()` is the function a validation harness should be using.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class SplitError(Exception):
    """A split that cannot be made. Refused rather than adjusted into validity."""


@dataclass(frozen=True)
class Split:
    """Index boundaries into an ordered row list. Half-open, `[start, stop)`.

    Indices rather than copies, so the caller keeps one list and there is no
    way for a row to end up in two segments through a copying mistake.
    """

    train: tuple[int, int]
    validation: tuple[int, int]
    test: tuple[int, int]
    boundaries: tuple[datetime | None, datetime | None]

    def sizes(self) -> dict[str, int]:
        return {
            "train": self.train[1] - self.train[0],
            "validation": self.validation[1] - self.validation[0],
            "test": self.test[1] - self.test[0],
        }

    def as_dict(self) -> dict[str, Any]:
        first, second = self.boundaries
        return {
            "train": list(self.train),
            "validation": list(self.validation),
            "test": list(self.test),
            "train_ends": first.isoformat() if first else None,
            "validation_ends": second.isoformat() if second else None,
            "sizes": self.sizes(),
            "ordering": "chronological; no shuffling anywhere in this pipeline",
            "single_split_warning": (
                "one split is one observation. Splits at different fractions all put the "
                "same recent era in the holdout, so agreement between them is not "
                "confirmation. Read the walk-forward folds before believing a result."
            ),
        }


def chronological(
    times: list[datetime],
    *,
    train: float = 0.6,
    validation: float = 0.2,
) -> Split:
    """Cut an ordered series into train -> validation -> test, in time order.

    `train` and `validation` are fractions of the row count; the test segment is
    whatever remains. Fractions rather than dates because a dataset's date range
    is not known until it is built, and a caller who wants dates can convert.
    """
    if not times:
        raise SplitError("cannot split an empty series")
    if train <= 0 or validation < 0:
        raise SplitError("the training fraction must be positive and validation non-negative")
    if train + validation >= 1.0:
        raise SplitError(
            f"train ({train}) + validation ({validation}) leaves no test segment; "
            "a split whose holdout is empty measures nothing"
        )
    _require_ordered(times)

    n = len(times)
    train_end = int(n * train)
    validation_end = train_end + int(n * validation)
    if train_end < 1 or validation_end >= n:
        raise SplitError(
            f"{n} rows cannot be cut {train}/{validation}: every segment must hold at "
            "least one row, and a segment of zero rows is not a small segment"
        )

    return Split(
        train=(0, train_end),
        validation=(train_end, validation_end),
        test=(validation_end, n),
        boundaries=(times[train_end - 1], times[validation_end - 1]),
    )


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold: train on everything before, test on what follows."""

    index: int
    train: tuple[int, int]
    test: tuple[int, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold": self.index,
            "train": list(self.train),
            "test": list(self.test),
            "train_rows": self.train[1] - self.train[0],
            "test_rows": self.test[1] - self.test[0],
        }


def walk_forward(
    times: list[datetime],
    *,
    folds: int = 4,
    initial_train: float = 0.4,
) -> list[Fold]:
    """Expanding-window folds. Fold k trains on everything before its test block.

    Expanding rather than sliding: the platform's own walk-forward in
    `rule_search.py` re-ranks on all prior eras, and a sliding window would
    throw away history for no measured reason. Each fold's training set is a
    strict prefix of its test set's past, which is what makes the folds
    independent evidence rather than the same split read several times.
    """
    if folds < 2:
        raise SplitError("a single fold is a single split; ask for at least two")
    if not 0 < initial_train < 1:
        raise SplitError("initial_train must be a fraction between 0 and 1")
    _require_ordered(times)

    n = len(times)
    start = int(n * initial_train)
    remaining = n - start
    if start < 1 or remaining < folds:
        raise SplitError(
            f"{n} rows cannot make {folds} folds after an initial {initial_train} "
            "training block; there would be a fold with no test rows"
        )

    block = remaining // folds
    out: list[Fold] = []
    for k in range(folds):
        test_start = start + k * block
        test_end = n if k == folds - 1 else test_start + block
        out.append(Fold(index=k + 1, train=(0, test_start), test=(test_start, test_end)))
    return out


def _require_ordered(times: list[datetime]) -> None:
    """Refuse a series that is not already in time order.

    A split of unordered rows is a random split wearing a chronological name,
    and it would pass every other check in this pipeline.
    """
    for i in range(1, len(times)):
        if times[i] < times[i - 1]:
            raise SplitError(
                f"row {i} is stamped {times[i].isoformat()}, before row {i - 1} at "
                f"{times[i - 1].isoformat()}. A chronological split of unordered rows "
                "is a random split, so this is refused rather than sorted -- the "
                "ordering problem is upstream and sorting here would hide it."
            )
