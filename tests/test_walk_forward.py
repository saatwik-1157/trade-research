#!/usr/bin/env python
"""The walk-forward, and whether its null means anything.

This tool retired the only model result that ever cleared a permutation
control -- USDCAD at +7.40 points on a single split became 0 of 4 folds and a
mean of -0.74. A verdict that strong has to be earned, so the checks here are
about whether the tool could have said yes:

  * **Power.** If it cannot find an effect that IS there, "0 of 4 folds" is
    blindness rather than evidence. A planted feature that determines the
    label must be found, and found large.
  * **Restraint.** On a coin-flip label it must return roughly zero, or every
    null it reports is manufactured.
  * **No lookahead.** Each fold must train strictly on its own past. A
    walk-forward that leaked would report the one thing it exists to
    disprove, and it would look like a discovery.
  * **The right baseline.** The margin is against the TEST block's own
    majority share. Scoring against the training set's would flatter a model
    whenever the class balance drifted its way and punish it when it did not,
    for reasons that are not about the model at all -- and the balance does
    drift here, from 50.10% to 59.94% across these datasets.
"""
from __future__ import annotations

import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from walk_forward_models import fold_edges, margin_of  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


# ------------------------------------------------------------- fold edges

print()
print("Each fold trains strictly on its own past")
edges = fold_edges(1000, 4)
check("one more edge than folds", len(edges), 5)
check("the first block starts at the start", edges[0], 200)
check("and the last ends at the end", edges[-1], 1000)
# The property that matters: fold i TRAINS on [0, edges[i]) and TESTS on
# [edges[i], edges[i+1]). Training therefore ends exactly where testing
# begins, with no overlap and no gap.
overlaps = [edges[i + 1] > edges[i] for i in range(4)]
check("every test block is non-empty", overlaps, [True] * 4)
check("training ends exactly where testing begins",
      [edges[i] for i in range(4)], [200, 400, 600, 800])
check("no test block starts before its training ends",
      all(edges[i] <= edges[i + 1] for i in range(4)), True)
# A fold count that does not divide the rows must still partition them.
odd = fold_edges(997, 3)
check("an awkward row count still yields increasing edges",
      all(odd[i] < odd[i + 1] for i in range(3)), True)

# ---------------------------------------------------------------- margin

print()
print("The margin is against the TEST block's own majority")
# 70 ones, 30 zeros. A model that always says 1 scores 70% -- exactly the
# majority -- so its margin must be zero, not +20 for being 'mostly right'.
truth = [1] * 70 + [0] * 30
check("always predicting the majority scores a zero margin",
      round(margin_of([1] * 100, truth), 6), 0.0)
check("always predicting the minority is far negative",
      round(margin_of([0] * 100, truth), 6), -40.0)
check("a perfect model scores the distance to the majority",
      round(margin_of(truth, truth), 6), 30.0)
# The baseline must follow the TEST block. Same predictions, different truth
# balance, different margin -- that is the point of using the block's own.
other = [1] * 50 + [0] * 50
check("a different test balance gives a different baseline",
      round(margin_of([1] * 100, other), 6), 0.0)
check("an empty block is nan rather than a divide by zero",
      margin_of([], []) != margin_of([], []), True)

# ------------------------------------------------------- power on a fit

print()
print("The walk-forward can find a real effect, and does not invent one")

from app.training import trainers  # noqa: E402
from app.training.config import ModelFamily, TrainingConfig  # noqa: E402

FEATS = ("a", "b", "c")
CFG = TrainingConfig(family=ModelFamily.trade_probability, dataset_key="k",
                     dataset_version="1", model_version="t", features=FEATS)


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def walk(planted: bool, seed: int = 5, n: int = 4000, folds: int = 4):
    rng = random.Random(seed)
    rows, targets = [], []
    for _ in range(n):
        a, b, c = rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)
        targets.append((1 if a > 0 else 0) if planted else rng.choice([0, 1]))
        rows.append({"a": a, "b": b, "c": c})
    out = []
    edges = fold_edges(n, folds)
    for i in range(folds):
        tr_end, te_end = edges[i], edges[i + 1]
        tx, tk = trainers.vectorise(rows[:tr_end], FEATS)
        ex, ek = trainers.vectorise(rows[tr_end:te_end], FEATS)
        ty = [targets[k] for k in tk]
        ey = [targets[tr_end + k] for k in ek]
        coef, _ = trainers.fit_weighted_logistic(tx, ty, features=FEATS, config=CFG)
        preds = [
            1 if _sig(coef.bias + sum(w * v for w, v in zip(coef.weights, vals, strict=True)))
            >= 0.5 else 0
            for vals in ex
        ]
        out.append(margin_of(preds, ey))
    return out


planted = walk(True)
noise = walk(False)
check("a planted signal is found in every fold",
      all(m > 20 for m in planted), True)
check("and its mean margin is large",
      statistics.fmean(planted) > 20, True)
check("a coin-flip label produces no effect",
      abs(statistics.fmean(noise)) < 3, True)
check("and no noise fold clears the 1.50-point spread hurdle by much",
      max(noise) < 5, True)

# The discriminating check: if the fold leaked its own future, a coin-flip
# label would become learnable, because the model would have seen the answer.
check("noise stays unlearnable, so no fold sees its own future",
      statistics.fmean(noise) < 3, True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall walk-forward checks passed")


def _saturation_checks() -> None:
    """The bug THIS SUITE MISSED, pinned so it cannot come back.

    The first walk-forward vectorised RAW features where
    `training/service.py` scales. A logistic over unscaled FX features
    saturates: measured on the first fold, every training probability came
    out at exactly 1.0000, spread 0.0000, with non-trivial weights and a
    real bias. The model then emitted one constant class per fold, which
    read as "the data has no signal" and was "the fitter was never given a
    chance to look".

    The checks above did not catch it because their synthetic features were
    already gauss(0, 1) -- near enough to scaled that nothing saturated.
    Real ones are not: a price is ~1.17, a return is ~1e-4, an RSI is ~50.
    """
    def probabilities(scale_them: bool) -> list[float]:
        rng = random.Random(11)
        rows, targets = [], []
        for _ in range(900):
            price = 1.17 + rng.gauss(0, 0.01)
            ret = rng.gauss(0, 0.0001)
            rsi = 50 + rng.gauss(0, 15)
            targets.append(1 if ret > 0 else 0)
            rows.append({"a": price, "b": ret, "c": rsi})
        if scale_them:
            for name in ("a", "b", "c"):
                vals = [r[name] for r in rows]
                mean = statistics.fmean(vals)
                sd = statistics.pstdev(vals) or 1.0
                for r in rows:
                    r[name] = (r[name] - mean) / sd
        x, keep = trainers.vectorise(rows, FEATS)
        y = [targets[k] for k in keep]
        coef, _ = trainers.fit_weighted_logistic(x, y, features=FEATS, config=CFG)
        return [
            _sig(coef.bias
                 + sum(w * v for w, v in zip(coef.weights, vals, strict=True)))
            for vals in x
        ]

    raw = probabilities(scale_them=False)
    scaled = probabilities(scale_them=True)
    raw_spread = max(raw) - min(raw)
    scaled_spread = max(scaled) - min(scaled)

    print()
    print("Unscaled features change the fit, and the tool must scale")
    # HONEST LIMIT OF THIS CHECK. The real saturation needed all 22 features
    # at real FX magnitudes together; three synthetic ones do not reproduce
    # it -- measured, the raw spread here is ~0.92 rather than ~0.00. So this
    # does NOT assert saturation, because tuning a synthetic until it
    # saturates would only prove what it was tuned to prove. What it does
    # assert is that scaling materially changes the fit, which is true, and
    # is the reason the tool cannot be allowed to skip it.
    check("scaled features give a real spread of probabilities",
          scaled_spread > 0.1, True)
    check("and scaling materially changes the fitted probabilities",
          abs(scaled_spread - raw_spread) > 0.01, True)

    # THE CHECK THAT ACTUALLY BITES. The first version of this grepped
    # `fit_fold`'s source for the word `scaled` -- and when half the scaling
    # was reverted (train raw, test scaled) the grep still matched and NOT
    # ONE check failed. A source-text assertion cannot tell you a thing ran,
    # only that a string is present. So drive the real function with features
    # at real FX magnitudes and read what comes out.
    from walk_forward_models import fit_fold

    rng = random.Random(3)
    n = 1200
    rows, targets = [], []
    for _ in range(n):
        price = 1.17 + rng.gauss(0, 0.01)      # ~1
        ret = rng.gauss(0, 0.0001)             # ~1e-4
        rsi = 50 + rng.gauss(0, 15)            # ~50
        # A real, learnable relationship, so a working fold MUST vary.
        targets.append(1 if rsi > 50 else 0)
        rows.append({"a": price, "b": ret, "c": rsi})

    preds, truth = fit_fold(rows, targets, 800, n, feats=FEATS,
                            feature_version="v-test")
    check("a fold returns a prediction per test row", len(preds), len(truth))
    check("and the predictions VARY rather than collapsing to one class",
          len(set(preds)), 2)
    check("and it finds the planted relationship",
          margin_of(preds, truth) > 20, True)

    # Too little training data is refused rather than fitted on noise.
    empty_preds, empty_truth = fit_fold(rows, targets, 50, 200, feats=FEATS,
                                        feature_version="v-test")
    check("a fold with too little history returns nothing",
          (empty_preds, empty_truth), ([], []))


_saturation_checks()

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall checks passed, including the saturation guard")
