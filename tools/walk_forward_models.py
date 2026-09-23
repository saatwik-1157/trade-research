#!/usr/bin/env python3
"""Walk a trained model forward across eras. The gate the training run omits.

`training/service.py` fits one chronological split and its own comparison
block says so: *"this is a comparison on a held-out segment of one dataset.
It is not the L26 gate: no permutation null, no correction for the number of
candidates tried, and no walk-forward. A model is not validated until it has
cleared those."* The gate report adds *"Read the walk-forward folds before
believing a result."* Neither the folds nor the null are computed anywhere,
so this file computes both.

**It exists because a single split is the trap this repository keeps falling
into.** `donchian_fade_55` cleared a pooled t of 2.79 and was positive in
three of five eras. The D1 exit grid scored an in-sample t of 6.59 and posted
-1.82 out of sample. The H4 grid looked like an exit effect and was one
directional era seen from inside a grid. Every one of them passed the check
the training service performs and failed the one it does not.

Each fold trains on everything before a block and tests on that block, so no
fold ever sees its own future. The shuffled-label control runs the identical
procedure with the labels permuted, which holds the class balance, the row
count, the features and the fitting fixed and destroys only the link between
them.

    python tools/walk_forward_models.py --key usdcad_h1_v2
    python tools/walk_forward_models.py --folds 5
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))


def _sigmoid(x: float) -> float:
    if x >= 0:
        import math
        return 1.0 / (1.0 + math.exp(-x))
    import math
    e = math.exp(x)
    return e / (1.0 + e)


async def run(keys: list[str], folds: int, shuffle: bool) -> int:
    from app.core.settings import get_settings
    from app.datasets.service import build_dataset_loader
    from app.db.session import make_engine, make_session_factory
    from app.models.datasets import DatasetRecord
    from app.training import trainers
    from app.training.config import ModelFamily, TrainingConfig
    from app.datasets import features as feature_engine
    from sqlalchemy import select

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    feats = feature_engine.DEFAULT_FEATURES

    print(f"\n  walk-forward, {folds} folds, each trained only on its own past"
          f"{'  [SHUFFLED LABELS - CONTROL]' if shuffle else ''}")
    print("  " + "-" * 74)
    head = f"  {'dataset':16}" + "".join(f"{'f' + str(i + 1):>9}" for i in range(folds))
    print(head + f"{'mean':>9}{'folds +':>9}")

    async with factory() as db:
        records = (await db.scalars(
            select(DatasetRecord).where(DatasetRecord.status == "READY")
        )).all()
        chosen = [r for r in records if (not keys or r.key in keys)]
        if not chosen:
            print("  no READY datasets matched")
            return 1

        for rec in sorted(chosen, key=lambda r: r.key):
            ds = await build_dataset_loader(rec)(db)
            rows = list(ds.rows)
            # The trade_probability target, built the same way the service
            # builds it: WIN against everything else.
            targets_all = [
                1 if (r.labels.get("bracket_outcome") == "WIN") else 0
                for r in rows
            ]
            if shuffle:
                random.Random(rec.key).shuffle(targets_all)

            feat_rows = [r.features for r in rows]
            margins: list[float] = []
            n = len(rows)
            # Equal blocks; fold i trains on [0, start_i) and tests on block i.
            edges = [int(n * (i + 1) / (folds + 1)) for i in range(folds + 1)]
            for i in range(folds):
                tr_end, te_end = edges[i], edges[i + 1]
                tr_x, tr_keep = trainers.vectorise(feat_rows[:tr_end], feats)
                te_x, te_keep = trainers.vectorise(feat_rows[tr_end:te_end], feats)
                tr_y = [targets_all[k] for k in tr_keep]
                te_y = [targets_all[tr_end + k] for k in te_keep]
                if len(tr_y) < 200 or len(te_y) < 100 or len(set(tr_y)) < 2:
                    margins.append(float("nan"))
                    continue
                cfg = TrainingConfig(
                    family=ModelFamily.trade_probability,
                    dataset_key=rec.key, dataset_version=rec.version,
                    model_version="wf", features=feats,
                )
                coef, _report = trainers.fit_weighted_logistic(
                    tr_x, tr_y, features=feats, config=cfg
                )
                preds = [
                    1 if _sigmoid(
                        coef.bias
                        + sum(w * v for w, v in zip(coef.weights, vals, strict=True))
                    ) >= 0.5 else 0
                    for vals in te_x
                ]
                acc = sum(1 for p, t in zip(preds, te_y, strict=True) if p == t) / len(te_y)
                share = max(te_y.count(1), te_y.count(0)) / len(te_y)
                margins.append((acc - share) * 100)

            good = [m for m in margins if m == m]
            mean = statistics.fmean(good) if good else float("nan")
            pos = sum(1 for m in good if m > 0)
            print(f"  {rec.key:16}"
                  + "".join(f"{m:>+9.2f}" if m == m else f"{'-':>9}" for m in margins)
                  + f"{mean:>+9.2f}{pos:>6}/{len(good)}")

    await engine.dispose()
    print("\n  margin is accuracy minus the TEST block's own majority share, in\n"
          "  win-rate points. The spread hurdle is 1.50. A candidate carried by\n"
          "  one fold is a regime, not an edge -- which is how donchian_fade_55\n"
          "  and both exit grids died.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default="", help="one dataset key, or all READY")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--shuffle-labels", action="store_true")
    args = ap.parse_args()
    keys = [k.strip() for k in args.key.split(",") if k.strip()]
    return asyncio.run(run(keys, args.folds, args.shuffle_labels))


if __name__ == "__main__":
    raise SystemExit(main())
