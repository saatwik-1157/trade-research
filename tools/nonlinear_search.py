#!/usr/bin/env python3
"""Every model here has been LINEAR. This is the interaction test.

Twenty-fourth search, and the last untested cell in the modelling work.

`train_models.py` and `pooled_walk_forward.py` both fit
`trainers.fit_weighted_logistic`, which is linear in the features. A logistic
model cannot represent "feature A matters only when feature B is high" -- not
poorly, but *at all* -- so if the only structure in this data is an
interaction, all twenty-three searches above would have missed it by
construction rather than by measurement. That is a gap in what was RUN, and it
is the same kind of gap the Kaufman replication closed when his 28-80 day
range turned out never to have been in the grid.

**The learner is written here rather than imported because it has to be.**
sklearn, lightgbm, xgboost and scipy are all absent from this environment;
numpy is what exists. So this is a small gradient-boosted ensemble of
depth-limited trees, in the same spirit as the repository writing its own
Parabolic SAR and efficiency ratio instead of taking them on trust.

**A hand-rolled learner can produce a false NULL as easily as a false signal,
so its power is proven before its verdict is read** -- and proven on the one
thing that justifies the search. The check plants an XOR: a label that depends
on the SIGN AGREEMENT of two features and on neither feature alone. Logistic
regression is provably at chance on it. If the ensemble does not clear it
comfortably, the tool is broken and its null means nothing.

**Trees need no scaling, which removes a failure this project has already
had.** The first walk-forward here fed a logistic raw features, it saturated,
emitted one constant class per fold, and that was recorded as "the model makes
no prediction" when the truth was "the fitter was never given a chance to
look". A tree splits on order statistics, so monotone rescaling cannot change
it and that failure mode is structurally unavailable.

    python tools/nonlinear_search.py
    python tools/nonlinear_search.py --shuffle-labels
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import statistics
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from walk_forward_models import in_block, margin_of, time_fold_edges  # noqa: E402

KEYS = ("eurusd_h1_v2", "gbpusd_h1_v2", "usdjpy_h1_v2", "usdcad_h1_v2",
        "audusd_h1_v2", "usdchf_h1_v2", "nzdusd_h1_v2")

DEPTH = 3
ROUNDS = 60
LEARN = 0.1
MIN_LEAF = 50


def _best_split(x, g, h, lam=1.0):
    """The split of one feature that most reduces the boosted loss.

    Candidate thresholds are quantiles rather than every distinct value: on
    34,482 rows the exhaustive scan is both slow and prone to carving off
    single observations, which is how a tree memorises.
    """
    best = (0.0, None)
    finite = np.isfinite(x)
    if finite.sum() < 2 * MIN_LEAF:
        return best
    qs = np.quantile(x[finite], np.linspace(0.05, 0.95, 19))
    G, H = g.sum(), h.sum()
    parent = G * G / (H + lam)
    for t in np.unique(qs):
        left = finite & (x <= t)
        right = finite & (x > t)
        nl, nr = int(left.sum()), int(right.sum())
        if nl < MIN_LEAF or nr < MIN_LEAF:
            continue
        gl, hl = g[left].sum(), h[left].sum()
        gr, hr = g[right].sum(), h[right].sum()
        gain = gl * gl / (hl + lam) + gr * gr / (hr + lam) - parent
        if gain > best[0]:
            best = (float(gain), float(t))
    return best


def _grow(X, g, h, depth):
    """One tree, as nested dicts. A leaf is the Newton step for its rows."""
    if depth == 0 or len(g) < 2 * MIN_LEAF:
        return {"leaf": float(-g.sum() / (h.sum() + 1.0))}
    best_gain, best_feat, best_thr = 0.0, None, None
    for j in range(X.shape[1]):
        gain, thr = _best_split(X[:, j], g, h)
        if thr is not None and gain > best_gain:
            best_gain, best_feat, best_thr = gain, j, thr
    if best_feat is None:
        return {"leaf": float(-g.sum() / (h.sum() + 1.0))}
    col = X[:, best_feat]
    left = np.isfinite(col) & (col <= best_thr)
    right = ~left
    return {"feat": best_feat, "thr": best_thr,
            "left": _grow(X[left], g[left], h[left], depth - 1),
            "right": _grow(X[right], g[right], h[right], depth - 1)}


def _predict_tree(tree, X):
    out = np.zeros(len(X))
    if "leaf" in tree:
        return out + tree["leaf"]
    col = X[:, tree["feat"]]
    left = np.isfinite(col) & (col <= tree["thr"])
    out[left] = _predict_tree(tree["left"], X[left])
    out[~left] = _predict_tree(tree["right"], X[~left])
    return out


def fit_boosted(X, y, rounds=ROUNDS, depth=DEPTH, lr=LEARN):
    """Logistic gradient boosting. Returns (trees, base) for `predict`."""
    y = np.asarray(y, float)
    p = float(np.clip(y.mean(), 1e-6, 1 - 1e-6))
    base = math.log(p / (1 - p))
    score = np.full(len(y), base)
    trees = []
    for _ in range(rounds):
        prob = 1.0 / (1.0 + np.exp(-score))
        g = prob - y                       # gradient of logistic loss
        h = np.clip(prob * (1 - prob), 1e-6, None)
        tree = _grow(X, g, h, depth)
        trees.append(tree)
        score += lr * _predict_tree(tree, X)
    return trees, base


def predict_boosted(model, X):
    trees, base = model
    score = np.full(len(X), base)
    for t in trees:
        score += LEARN * _predict_tree(t, X)
    return (score > 0).astype(int)


def fit_logistic(X, y, iters=300, lr=0.1):
    """A plain logistic, for the side-by-side. Scaled, because it must be."""
    mu, sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
    sd = np.where(sd > 0, sd, 1.0)
    Z = np.nan_to_num((X - mu) / sd)
    w = np.zeros(Z.shape[1])
    b = 0.0
    y = np.asarray(y, float)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(Z @ w + b)))
        w -= lr * (Z.T @ (p - y)) / len(y)
        b -= lr * float((p - y).mean())
    return (w, b, mu, sd)


def predict_logistic(model, X):
    w, b, mu, sd = model
    Z = np.nan_to_num((X - mu) / sd)
    return ((Z @ w + b) > 0).astype(int)


def self_test() -> int:
    """Power on an interaction LOGISTIC CANNOT REPRESENT, and restraint on noise."""
    failed = []

    def check(label, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {label:56s} got={got} want={want}")
        if not ok:
            failed.append(label)

    rng = np.random.default_rng(11)
    n = 6000
    X = rng.normal(0, 1, (n, 6))
    # XOR: the label is the SIGN AGREEMENT of two features. Neither feature
    # alone carries any information at all, which is exactly what a linear
    # model is blind to.
    y = ((X[:, 0] > 0) == (X[:, 1] > 0)).astype(int)
    cut = n // 2
    lg = predict_logistic(fit_logistic(X[:cut], y[:cut]), X[cut:])
    gb = predict_boosted(fit_boosted(X[:cut], y[:cut]), X[cut:])
    acc_l = float((lg == y[cut:]).mean())
    acc_g = float((gb == y[cut:]).mean())
    print(f"        XOR out-of-sample: logistic {acc_l:.3f}, boosted {acc_g:.3f}")
    check("logistic is at chance on an interaction", bool(acc_l < 0.58), True)
    check("the ensemble is not", bool(acc_g > 0.85), True)
    check("and it beats the linear model by a wide margin",
          bool(acc_g - acc_l > 0.25), True)

    # Restraint: pure noise must not be learned.
    yn = rng.integers(0, 2, n)
    gbn = predict_boosted(fit_boosted(X[:cut], yn[:cut]), X[cut:])
    acc_n = float((gbn == yn[cut:]).mean())
    print(f"        pure noise out-of-sample: {acc_n:.3f}")
    check("a coin-flip label is not learned", bool(acc_n < 0.56), True)

    # A linear signal must still be found, or the tool is only an XOR detector.
    yl = (X[:, 2] > 0).astype(int)
    gbl = predict_boosted(fit_boosted(X[:cut], yl[:cut]), X[cut:])
    check("a plain linear signal is also found",
          bool(float((gbl == yl[cut:]).mean()) > 0.9), True)

    if failed:
        print(f"\n  {len(failed)} check(s) failed")
        return 1
    print("\n  all nonlinear self-checks passed")
    return 0


async def run(folds: int, shuffle: bool) -> int:
    from app.core.settings import get_settings
    from app.datasets import features as feature_engine
    from app.datasets.service import build_dataset_loader
    from app.db.session import make_engine, make_session_factory
    from app.models.datasets import DatasetRecord
    from sqlalchemy import select

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    feats = tuple(feature_engine.DEFAULT_FEATURES)

    pooled = []
    async with factory() as db:
        for key in KEYS:
            rec = await db.scalar(select(DatasetRecord).where(
                DatasetRecord.key == key, DatasetRecord.status == "READY"))
            if rec is None:
                continue
            ds = await build_dataset_loader(rec)(db)
            pooled.extend(ds.rows)
    await engine.dispose()
    if not pooled:
        print("  nothing pooled")
        return 1

    pooled.sort(key=lambda r: r.at)
    times = [r.at for r in pooled]
    X = np.array([[row.features.get(f, np.nan) for f in feats] for row in pooled],
                 dtype=float)
    y = np.array([1 if r.labels.get("bracket_outcome") == "WIN" else 0
                  for r in pooled])
    if shuffle:
        idx = list(range(len(y)))
        random.Random(4242).shuffle(idx)
        y = y[idx]

    print(f"\n  nonlinear walk-forward: {len(pooled):,} rows, {len(feats)} features"
          f"{'  [SHUFFLED LABELS - CONTROL]' if shuffle else ''}")
    print(f"  depth {DEPTH}, {ROUNDS} rounds, lr {LEARN}, min leaf {MIN_LEAF}; "
          f"trees need no scaling")
    print("  " + "-" * 70)
    print(f"  {'fold':6}{'train':>9}{'test':>8}{'majority':>10}"
          f"{'BOOSTED':>10}{'logistic':>10}")

    edges = time_fold_edges(times, folds)
    gb_margins, lg_margins = [], []
    for i in range(folds):
        a, b = edges[i], edges[i + 1]
        tr = [j for j, t in enumerate(times) if t < a]
        te = [j for j, t in enumerate(times) if in_block(t, a, b)]
        if len(tr) < 500 or len(te) < 200:
            continue
        Xtr, ytr = X[tr], y[tr]
        Xte, yte = X[te], y[te]
        gb = predict_boosted(fit_boosted(Xtr, ytr), Xte)
        lg = predict_logistic(fit_logistic(Xtr, ytr), Xte)
        mg, ml = margin_of(list(gb), list(yte)), margin_of(list(lg), list(yte))
        gb_margins.append(mg)
        lg_margins.append(ml)
        share = max(yte.sum(), len(yte) - yte.sum()) / len(yte)
        print(f"  {i + 1:<6}{len(tr):>9,}{len(te):>8,}{share * 100:>9.2f}%"
              f"{mg:>+10.2f}{ml:>+10.2f}")

    print("  " + "-" * 70)
    if gb_margins:
        print(f"  BOOSTED  mean {statistics.fmean(gb_margins):+.2f} points, "
              f"{sum(1 for m in gb_margins if m > 0)} of {len(gb_margins)} positive, "
              f"{sum(1 for m in gb_margins if m >= 1.50)} clearing 1.50")
        print(f"  logistic mean {statistics.fmean(lg_margins):+.2f} points, "
              f"{sum(1 for m in lg_margins if m > 0)} of {len(lg_margins)} positive")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--shuffle-labels", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    return asyncio.run(run(args.folds, args.shuffle_labels))


if __name__ == "__main__":
    raise SystemExit(main())
