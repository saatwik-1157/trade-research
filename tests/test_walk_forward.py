#!/usr/bin/env python
"""The walk-forward, and whether its verdict was earned.

This tool retired the only model result that has ever cleared a permutation
control here -- USDCAD at +7.40 points on a single split, against a
walk-forward mean of -1.65. A verdict that strong has to be earned, so these
checks ask whether the tool could have said yes:

  * **Power.** If it cannot find an effect that IS there, a negative verdict
    is blindness rather than evidence.
  * **Restraint.** On a coin-flip label it must return roughly zero, or every
    null it reports is manufactured.
  * **No lookahead.** Each fold must train strictly on its own past. A
    walk-forward that leaked would report the one thing it exists to
    disprove, and it would look like a discovery.
  * **The right baseline.** The margin is against the TEST block's own
    majority share, because the balance drifts from 50.10% to 59.94% across
    these datasets and scoring against the training set's would flatter or
    punish a model for reasons that are not about the model.
  * **Scaling.** The first version fed the fitter RAW features, it saturated,
    and it emitted one constant class per fold -- which read as "no signal in
    the data" and was "the fitter never got a chance to look".

**The pure checks need nothing but the standard library and always run. The
fitter checks need the backend, which brings in sqlalchemy, and CI installs
requirements.txt only -- so they skip there and say so.** An earlier version
called itself "verified CI-safe" on the strength of running under the project
venv, which has sqlalchemy. Verifying against the wrong environment is not
verifying, and that is the second time this week the same mistake reached a
push.
"""
from __future__ import annotations

import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from walk_forward_models import (  # noqa: E402
    fold_edges, in_block, margin_of, time_fold_edges,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


# ============================ pure checks: no dependencies, always run =====

print()
print("Each fold trains strictly on its own past")
edges = fold_edges(1000, 4)
check("one more edge than folds", len(edges), 5)
check("the first block starts at the start", edges[0], 200)
check("and the last ends at the end", edges[-1], 1000)
check("every test block is non-empty",
      [edges[i + 1] > edges[i] for i in range(4)], [True] * 4)
check("training ends exactly where testing begins",
      [edges[i] for i in range(4)], [200, 400, 600, 800])
check("no test block starts before its training ends",
      all(edges[i] <= edges[i + 1] for i in range(4)), True)
odd = fold_edges(997, 3)
check("an awkward row count still yields increasing edges",
      all(odd[i] < odd[i + 1] for i in range(3)), True)

print()
print("The margin is against the TEST block's own majority")
truth = [1] * 70 + [0] * 30
check("always predicting the majority scores a zero margin",
      round(margin_of([1] * 100, truth), 6), 0.0)
check("always predicting the minority is far negative",
      round(margin_of([0] * 100, truth), 6), -40.0)
check("a perfect model scores the distance to the majority",
      round(margin_of(truth, truth), 6), 30.0)
check("a different test balance gives a different baseline",
      round(margin_of([1] * 100, [1] * 50 + [0] * 50), 6), 0.0)
check("an empty block is nan rather than a divide by zero",
      margin_of([], []) != margin_of([], []), True)


print()
print("A pooled fold boundary is a TIME, and no row falls outside every block")
# Seven symbols sharing 400 hourly stamps, the pooled shape exactly.
STAMPS = list(range(400))
ROWS = sorted(t for t in STAMPS for _ in range(7))
ED = time_fold_edges(ROWS, 5)
check("one more edge than folds", len(ED), 6)
# The defect this replaces: the last edge WAS the final timestamp, and the
# block being half-open meant every row at that stamp was in no block at all.
# On the real run that silently dropped 7 of 34,482 rows.
check("the last edge is open rather than the final stamp", ED[-1], None)
check("the inner edges are real stamps in increasing order",
      all(ED[i] < ED[i + 1] for i in range(4)), True)

_tested = [t for t in ROWS if any(in_block(t, ED[i], ED[i + 1]) for i in range(5))]
_before = [t for t in ROWS if t < ED[0]]
check("every row is either pre-first-edge or tested, none lost",
      len(_tested) + len(_before), len(ROWS))
check("and the FINAL stamp is tested rather than dropped",
      in_block(max(ROWS), ED[4], ED[5]), True)
check("each row is tested at most once",
      max(sum(1 for i in range(5) if in_block(t, ED[i], ED[i + 1]))
          for t in set(ROWS)), 1)

# The reason the boundary is a time at all: an index cut through 2,800 rows
# sorted by stamp lands mid-group and puts a bar's own siblings in training.
_leaks = []
for i in range(5):
    tr = [t for t in ROWS if t < ED[i]]
    te = [t for t in ROWS if in_block(t, ED[i], ED[i + 1])]
    if tr and te and max(tr) >= min(te):
        _leaks.append(i)
check("no fold trains on a stamp it also tests", _leaks, [])
check("an index split on the same rows WOULD split a stamp",
      ROWS[fold_edges(len(ROWS), 5)[0] - 1] == ROWS[fold_edges(len(ROWS), 5)[0]],
      True)


# ============================ fitter checks: need the backend =============

try:
    from app.training import trainers
    from app.training.config import ModelFamily, TrainingConfig
    BACKEND = True
except ImportError as exc:  # pragma: no cover - this is the CI path
    print()
    print(f"  SKIP  the fitter checks: {exc}")
    print("        app.training pulls in sqlalchemy, which requirements.txt")
    print("        does not install. The pure checks above still ran.")
    BACKEND = False

FEATS = ("a", "b", "c")


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def _predict(coef, matrix):
    return [
        1 if _sig(coef.bias
                  + sum(w * v for w, v in zip(coef.weights, vals, strict=True))) >= 0.5
        else 0
        for vals in matrix
    ]


def _power_and_restraint(cfg) -> None:
    """A planted effect must be found; a coin flip must not be."""
    def walk(planted: bool, seed: int = 5, n: int = 4000, folds: int = 4):
        rng = random.Random(seed)
        rows, targets = [], []
        for _ in range(n):
            a, b, c = rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)
            targets.append((1 if a > 0 else 0) if planted else rng.choice([0, 1]))
            rows.append({"a": a, "b": b, "c": c})
        out = []
        ed = fold_edges(n, folds)
        for i in range(folds):
            tr_end, te_end = ed[i], ed[i + 1]
            tx, tk = trainers.vectorise(rows[:tr_end], FEATS)
            ex, ek = trainers.vectorise(rows[tr_end:te_end], FEATS)
            ty = [targets[k] for k in tk]
            ey = [targets[tr_end + k] for k in ek]
            coef, _ = trainers.fit_weighted_logistic(tx, ty, features=FEATS, config=cfg)
            out.append(margin_of(_predict(coef, ex), ey))
        return out

    planted, noise = walk(True), walk(False)
    print()
    print("The walk-forward finds a real effect and does not invent one")
    check("a planted signal is found in every fold", all(m > 20 for m in planted), True)
    check("and its mean margin is large", statistics.fmean(planted) > 20, True)
    check("a coin-flip label produces no effect",
          abs(statistics.fmean(noise)) < 3, True)
    check("no noise fold clears the spread hurdle by much", max(noise) < 5, True)
    # If a fold leaked its own future, a coin flip would become learnable.
    check("noise stays unlearnable, so no fold sees its own future",
          statistics.fmean(noise) < 3, True)


def _scaling(cfg) -> None:
    """Scaling is what separates a fitted model from a constant.

    Driven through `fit_fold` itself rather than grepped for. The first
    version of this check searched the source for the word `scaled`, and when
    HALF the scaling was reverted -- train raw, test scaled -- the string was
    still present and not one check failed.
    """
    from walk_forward_models import fit_fold

    rng = random.Random(3)
    n = 1200
    rows, targets = [], []
    for _ in range(n):
        price = 1.17 + rng.gauss(0, 0.01)   # ~1
        ret = rng.gauss(0, 0.0001)          # ~1e-4
        rsi = 50 + rng.gauss(0, 15)         # ~50
        targets.append(1 if rsi > 50 else 0)   # a real, learnable relationship
        rows.append({"a": price, "b": ret, "c": rsi})

    preds, block = fit_fold(rows, targets, 800, n, feats=FEATS,
                            feature_version="v-test")
    print()
    print("Features at real magnitudes still produce a varying model")
    check("a fold returns a prediction per test row", len(preds), len(block))
    check("the predictions VARY rather than collapsing to one class",
          len(set(preds)), 2)
    check("and it finds the planted relationship", margin_of(preds, block) > 20, True)
    check("a fold with too little history returns nothing",
          fit_fold(rows, targets, 50, 200, feats=FEATS, feature_version="v-test"),
          ([], []))


if BACKEND:
    CFG = TrainingConfig(family=ModelFamily.trade_probability, dataset_key="k",
                         dataset_version="1", model_version="t", features=FEATS)
    _power_and_restraint(CFG)
    _scaling(CFG)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print(f"\nall walk-forward checks passed"
      f"{'' if BACKEND else ' (fitter checks skipped: no backend)'}")
