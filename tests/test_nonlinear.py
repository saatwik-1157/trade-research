#!/usr/bin/env python
"""A hand-rolled learner, and why its power is proven before its null is read.

Every model in this project has been LINEAR, so an interaction -- "feature A
matters only when feature B is high" -- was invisible by construction rather
than by measurement. This ensemble exists to close that gap, and sklearn,
lightgbm, xgboost and scipy are all absent from this environment, so it is
written here.

That is the risk: a learner written for one search can produce a false NULL as
easily as a false signal, and a null is what it was always most likely to
report. So the checks below prove capability first, on the one thing that
justifies the search -- an XOR, where the label depends on the sign agreement
of two features and on neither alone. Logistic regression is provably at
chance on it. If the ensemble cannot clear that, its verdict on real data
means nothing.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from nonlinear_search import (  # noqa: E402
    MIN_LEAF, _grow, _predict_tree, fit_boosted, fit_logistic,
    predict_boosted, predict_logistic,
)

FAILED: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


rng = np.random.default_rng(11)
n = 6000
X = rng.normal(0, 1, (n, 6))
cut = n // 2

print()
print("It sees an interaction that a linear model provably cannot")
y_xor = ((X[:, 0] > 0) == (X[:, 1] > 0)).astype(int)
acc_l = float((predict_logistic(fit_logistic(X[:cut], y_xor[:cut]), X[cut:])
               == y_xor[cut:]).mean())
acc_g = float((predict_boosted(fit_boosted(X[:cut], y_xor[:cut]), X[cut:])
               == y_xor[cut:]).mean())
print(f"        XOR out-of-sample: logistic {acc_l:.3f}, boosted {acc_g:.3f}")
check("logistic is at chance on XOR", bool(acc_l < 0.58), True)
check("the ensemble is far above it", bool(acc_g > 0.85), True)
# Neither feature alone carries anything, which is what makes this the right
# test: a model that merely found a strong main effect would score here too.
check("neither XOR feature predicts on its own",
      bool(abs(np.corrcoef(X[:, 0], y_xor)[0, 1]) < 0.05
           and abs(np.corrcoef(X[:, 1], y_xor)[0, 1]) < 0.05), True)

print()
print("And it does not invent structure that is not there")
y_noise = rng.integers(0, 2, n)
acc_n = float((predict_boosted(fit_boosted(X[:cut], y_noise[:cut]), X[cut:])
               == y_noise[cut:]).mean())
print(f"        coin-flip label out-of-sample: {acc_n:.3f}")
check("a coin flip is not learned", bool(acc_n < 0.56), True)
y_lin = (X[:, 2] > 0).astype(int)
check("but a plain linear signal still is",
      bool(float((predict_boosted(fit_boosted(X[:cut], y_lin[:cut]), X[cut:])
                  == y_lin[cut:]).mean()) > 0.9), True)

print()
print("Trees are scale-invariant, which removes a failure this project has had")
# The first walk-forward here fed a logistic RAW features, it saturated, and
# emitted one constant class per fold -- recorded as "the model makes no
# prediction" when the truth was that the fitter never got to look. A tree
# splits on order statistics, so that failure is structurally unavailable.
m = fit_boosted(X[:cut], y_xor[:cut])
scaled = X.copy()
scaled[:, 0] *= 1000.0
scaled[:, 3] += 5000.0
m2 = fit_boosted(scaled[:cut], y_xor[:cut])
check("rescaling features does not change the predictions",
      bool(np.array_equal(predict_boosted(m, X[cut:]),
                          predict_boosted(m2, scaled[cut:]))), True)
# The contrast, stated correctly. The historical failure was that the FITTER
# could not learn from features at wildly different magnitudes -- every
# training probability came out at 1.0000 -- not that prediction collapsed to
# one class. So the honest check fits both learners on badly scaled features
# and compares what each recovers.
wild = X.copy()
wild[:, 0] *= 1e6          # the interacting features, at absurd magnitudes
wild[:, 1] *= 1e-6
y_w = ((X[:, 0] > 0) == (X[:, 1] > 0)).astype(int)
gb_wild = float((predict_boosted(fit_boosted(wild[:cut], y_w[:cut]), wild[cut:])
                 == y_w[cut:]).mean())
gb_tame = float((predict_boosted(fit_boosted(X[:cut], y_w[:cut]), X[cut:])
                 == y_w[cut:]).mean())
check("the ensemble recovers the same accuracy at absurd magnitudes",
      bool(abs(gb_wild - gb_tame) < 0.01), True)
check("and it is still a real result, not a collapse to one class",
      bool(gb_wild > 0.85), True)

print()
print("Leaves are bounded, so the ensemble cannot memorise single rows")
g = rng.normal(0, 1, 400)
h = np.ones(400)
tree = _grow(X[:400], g, h, depth=3)


def leaf_sizes(t, rows):
    if "leaf" in t:
        return [len(rows)]
    col = X[:400][rows, t["feat"]]
    left = rows[np.isfinite(col) & (col <= t["thr"])]
    right = rows[~np.isin(rows, left)]
    return leaf_sizes(t["left"], left) + leaf_sizes(t["right"], right)


sizes = leaf_sizes(tree, np.arange(400))
check("every leaf holds at least the stated minimum",
      bool(min(sizes) >= MIN_LEAF or len(sizes) == 1), True)
check("and the rows are partitioned, not duplicated or dropped",
      sum(sizes), 400)

print()
print("Boosting actually reduces the loss it is minimising")
y_s = y_xor[:cut]
few = predict_boosted(fit_boosted(X[:cut], y_s, rounds=1), X[:cut])
many = predict_boosted(fit_boosted(X[:cut], y_s, rounds=60), X[:cut])
check("sixty rounds fit the training set better than one",
      bool(float((many == y_s).mean()) > float((few == y_s).mean())), True)
check("a constant label is handled without dividing by zero",
      len(set(predict_boosted(fit_boosted(X[:cut], np.ones(cut, int)),
                              X[cut:]).tolist())), 1)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed")
    raise SystemExit(1)
print("\nall nonlinear checks passed")
