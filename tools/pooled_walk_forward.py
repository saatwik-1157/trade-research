#!/usr/bin/env python3
"""One model over all seven majors at once, walked forward.

The per-symbol datasets are 4,926 rows each against 22 features -- 224 rows a
feature, and 8,711 rows are needed at 80% power to demonstrate the 1.50-point
spread hurdle. Pooling the seven gives **34,482 rows**, four times the
requirement and 1,567 rows a feature. If more data is what the models were
short of, this is where it shows.

`DatasetConfig` takes a single symbol deliberately, and its docstring says
why: a dataset pooling two instruments has to answer the unit question first.
**It is answered here rather than assumed.** Every feature used is
dimensionless -- returns and log returns, body/range/wick as percentages,
distances in ATR or standard deviations, RSI, hour of day, a rollover flag --
so a row from USDJPY and a row from EURUSD are the same quantity. That is not
true of price or of points, which is the metals error this repository
documents, and it is why the label is `bracket_outcome` (WIN/LOSS, path
dependent, already scale free) rather than a currency amount.

**The fold boundary is a TIME, not an index.** Seven symbols share the same
hourly timestamps, so index-splitting 34,482 sorted rows cuts through groups
of seven and puts a bar's own siblings in training while its row is tested.
Rows strictly before the boundary train; rows in the block test.

**The scaler is fitted per fold on that fold's past**, as in the per-symbol
walk-forward, because `scaler.fit` takes a Split precisely so that "fit on
everything" has no spelling.

    python tools/pooled_walk_forward.py
    python tools/pooled_walk_forward.py --shuffle-labels
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from walk_forward_models import (  # noqa: E402
    _sigmoid, in_block, margin_of, time_fold_edges,
)

KEYS = ("eurusd_h1_v2", "gbpusd_h1_v2", "usdjpy_h1_v2", "usdcad_h1_v2",
        "audusd_h1_v2", "usdchf_h1_v2", "nzdusd_h1_v2")


async def run(folds: int, shuffle: bool, subset: bool) -> int:
    from app.core.settings import get_settings
    from app.datasets import features as feature_engine
    from app.datasets import scaler as scaler_mod
    from app.datasets.service import build_dataset_loader
    from app.datasets.splits import Split
    from app.db.session import make_engine, make_session_factory
    from app.models.datasets import DatasetRecord
    from app.training import trainers
    from app.training.config import ModelFamily, TrainingConfig
    from sqlalchemy import select

    from train_models import REFUTED

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    feats = tuple(f for f in feature_engine.DEFAULT_FEATURES
                  if not (subset and f in REFUTED))

    pooled: list = []
    async with factory() as db:
        for key in KEYS:
            rec = await db.scalar(
                select(DatasetRecord).where(DatasetRecord.key == key,
                                            DatasetRecord.status == "READY")
            )
            if rec is None:
                print(f"  {key}: not READY, skipped")
                continue
            ds = await build_dataset_loader(rec)(db)
            pooled.extend(ds.rows)
            version = str((ds.manifest().get("config") or {}).get("feature_set_version"))
    await engine.dispose()

    if not pooled:
        print("  nothing pooled")
        return 1

    pooled.sort(key=lambda r: r.at)
    times = [r.at for r in pooled]
    feat_rows = [r.features for r in pooled]
    targets = [1 if r.labels.get("bracket_outcome") == "WIN" else 0 for r in pooled]
    if shuffle:
        random.Random(4242).shuffle(targets)

    print(f"\n  pooled walk-forward: {len(pooled):,} rows from {len(KEYS)} majors, "
          f"{len(feats)} features"
          f"{'  [SHUFFLED LABELS - CONTROL]' if shuffle else ''}")
    print(f"  {len(set(times)):,} distinct timestamps, "
          f"{len(pooled) / max(1, len(feats)):,.0f} rows per feature "
          f"(8,711 needed at 80% power)")
    print("  " + "-" * 74)
    print(f"  {'fold':6}{'train':>9}{'test':>8}{'majority':>10}{'accuracy':>10}"
          f"{'margin':>9}   tested from")

    edges = time_fold_edges(times, folds)
    margins: list[float] = []
    for i in range(folds):
        t_train_end, t_test_end = edges[i], edges[i + 1]
        train_idx = [j for j, t in enumerate(times) if t < t_train_end]
        # in_block, not `t_train_end <= t < t_test_end`: the last edge is
        # None (open), and the clamped version dropped one timestamp's rows.
        test_idx = [j for j, t in enumerate(times)
                    if in_block(t, t_train_end, t_test_end)]
        if len(train_idx) < 500 or len(test_idx) < 200:
            print(f"  {i + 1:<6}{len(train_idx):>9,}{len(test_idx):>8,}"
                  f"{'-':>10}{'-':>10}{'-':>9}   too small")
            margins.append(float("nan"))
            continue

        split = Split(train=(0, len(train_idx)),
                      validation=(len(train_idx), len(train_idx)),
                      test=(len(train_idx), len(train_idx) + len(test_idx)),
                      boundaries=(None, None))
        ordered = [feat_rows[j] for j in train_idx] + [feat_rows[j] for j in test_idx]
        fitted = scaler_mod.fit(ordered, split, feature_version=version, features=feats)
        scaled = fitted.transform(ordered)

        tr_x, tr_keep = trainers.vectorise(scaled[:len(train_idx)], feats)
        te_x, te_keep = trainers.vectorise(scaled[len(train_idx):], feats)
        tr_y = [targets[train_idx[k]] for k in tr_keep]
        te_y = [targets[test_idx[k]] for k in te_keep]
        if len(set(tr_y)) < 2 or len(te_y) < 200:
            margins.append(float("nan"))
            continue

        cfg = TrainingConfig(family=ModelFamily.trade_probability,
                             dataset_key="pooled", dataset_version="1",
                             model_version="pwf", features=feats)
        coef, _ = trainers.fit_weighted_logistic(tr_x, tr_y, features=feats, config=cfg)
        preds = [
            1 if _sigmoid(coef.bias
                          + sum(w * v for w, v in zip(coef.weights, vals, strict=True)))
            >= 0.5 else 0
            for vals in te_x
        ]
        m = margin_of(preds, te_y)
        margins.append(m)
        share = max(te_y.count(1), te_y.count(0)) / len(te_y)
        acc = sum(1 for p, t in zip(preds, te_y, strict=True) if p == t) / len(te_y)
        varied = "" if len(set(preds)) > 1 else "   (one class only)"
        print(f"  {i + 1:<6}{len(tr_y):>9,}{len(te_y):>8,}{share * 100:>9.2f}%"
              f"{acc * 100:>9.2f}%{m:>+9.2f}   {t_train_end:%Y-%m-%d}{varied}")

    good = [m for m in margins if m == m]
    if good:
        print("  " + "-" * 74)
        print(f"  mean margin {statistics.fmean(good):+.2f} points, "
              f"{sum(1 for m in good if m > 0)} of {len(good)} folds positive, "
              f"{sum(1 for m in good if m >= 1.50)} clearing the 1.50 hurdle")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--shuffle-labels", action="store_true")
    ap.add_argument("--exclude-refuted", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args.folds, args.shuffle_labels, args.exclude_refuted))


if __name__ == "__main__":
    raise SystemExit(main())
