#!/usr/bin/env python3
"""Train one trade-probability model per registered dataset, with a control.

The datasets are now large enough to say something: 4,926 rows each against
the 8,711 needed at 80% power to demonstrate the 1.50-point spread hurdle,
where the old 638-row set put the target inside the noise.

**The control is the point, and it is the same design that has worked all
week.** `--shuffle-labels` fits the identical model on the identical features
with the labels randomly permuted, which destroys any relationship between
features and outcome while preserving the class balance, the row count and
the fitting procedure exactly. A model that scores the same on shuffled
labels is fitting noise, and nothing else in the run can tell you that.
`volume_search.py` only caught its own null because the control was in it,
and the calendar search died this morning when a Tuesday control scored
+86.0 against Thursday's +86.6.

**Read accuracy against `majority_share`, never on its own.** `metrics.py`
says so in its own docstring: a dataset that is 70% WIN gives 70% accuracy to
a model that always says WIN and has learned nothing. The margin over the
majority class is the only part that could be skill.

**And read the margin against the COST hurdle, not against zero.** A model
that is 51% accurate where the majority is 50% has found 1 point of win rate,
and this broker's spread demands 1.50 before a trade breaks even. Beating the
baseline is necessary and nowhere near sufficient.

    python tools/train_models.py --dry-run
    python tools/train_models.py --shuffle-labels
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

# Features that RECONSTRUCT families this repository has already refuted.
# `shape_search.py` names them: handing a model all 22 searches dead ground
# and new ground at once, and no result could be attributed to either.
REFUTED = {
    "rsi_14",              # the RSI family
    "ema_spread_10_50",    # the EMA cross before it is thresholded
    "high_20_distance",    # the Donchian break
    "low_20_distance",     # the Donchian break
    "sma_distance_20",     # with volatility_20, the Bollinger band
    "roc_10",              # momentum at a shorter lookback
    # Measured 2026-09-23: a Tuesday CONTROL scored +86.0 against Thursday's
    # +86.6, so this column carries drift rather than a calendar effect.
    "day_of_week",
}


def _shuffling(inner, *, seed: str):
    """Wrap a dataset loader so the LABELS are permuted and nothing else is.

    The control this file exists for, and it was a flag wired to nothing in
    the first version -- declared in the help text, passed into run(), and
    never once consulted. A control that does not run is worse than no
    control, because the output still says CONTROL.

    Permuting preserves the class balance, the row count, the feature values
    and the fitting procedure exactly, and destroys only the relationship
    between the two. A model that scores the same here is fitting noise.
    """
    import random

    async def load(db):
        ds = await inner(db)
        rows = list(ds.rows)
        labels = [r.labels for r in rows]
        random.Random(seed).shuffle(labels)
        for row, lab in zip(rows, labels, strict=True):
            object.__setattr__(row, "labels", lab)
        return ds

    return load


async def run(keys: list[str], shuffle: bool, subset: bool, dry_run: bool) -> int:
    from app.core.settings import get_settings
    from app.datasets import features as feature_engine
    from app.datasets.service import build_dataset_loader
    from app.db.session import make_engine, make_session_factory
    # DatasetRecord, not Dataset: app.datasets.builder owns the name
    # Dataset for the built thing itself, and the ORM module says so in
    # its own docstring. Imported wrong twice before reading it.
    from app.models.datasets import DatasetRecord
    from app.training.config import ModelFamily, TrainingConfig
    from app.training.service import TrainingService
    from sqlalchemy import select

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    # The service owns its own sessions: a training job outlives the
    # request that queued it, so it cannot borrow the caller's.
    service = TrainingService(factory)

    feats = tuple(f for f in feature_engine.DEFAULT_FEATURES
                  if not (subset and f in REFUTED))
    print(f"\n  training trade_probability, {len(feats)} features"
          f"{' (refuted families excluded)' if subset else ''}"
          f"{'  [SHUFFLED LABELS - CONTROL]' if shuffle else ''}"
          f"{'  [DRY RUN]' if dry_run else ''}")
    print("  " + "-" * 76)
    print(f"  {'dataset':14}{'rows':>7}{'accuracy':>10}{'majority':>10}"
          f"{'margin':>9}{'vs 1.50':>9}   gates")

    async with factory() as db:
        records = (await db.scalars(
            select(DatasetRecord).where(DatasetRecord.status == "READY")
        )).all()
        # Raw text rather than an ORM model: there is no users model in
        # app/models, and the only thing needed here is an existing id to
        # satisfy the foreign key.
        from sqlalchemy import text
        user_id = await db.scalar(text("select id from users limit 1"))
        if not user_id:
            print("  no user rows; training_runs requires a real requester")
            return 1

        chosen = [r for r in records if (not keys or r.key in keys)]
        if not chosen:
            print("  no READY datasets matched")
            return 1

        for rec in sorted(chosen, key=lambda r: r.key):
            if dry_run:
                print(f"  {rec.key:14}{rec.row_count:>7}{'-':>10}{'-':>10}"
                      f"{'-':>9}{'-':>9}   not trained")
                continue
            cfg = TrainingConfig(
                family=ModelFamily.trade_probability,
                dataset_key=rec.key,
                dataset_version=rec.version,
                model_version="1",
                features=feats,
            )
            loader = build_dataset_loader(rec)
            if shuffle:
                loader = _shuffling(loader, seed=rec.key)
            try:
                # A REAL user id: training_runs.requested_by_user_id is a
                # foreign key into users, so a placeholder like "cli" fails
                # with ForeignKeyViolationError -- which surfaced only as a
                # MissingGreenlet from the pool until the head of the
                # traceback was read.
                run_row = await service.queue(db, cfg, user_id=user_id,
                                              loader=loader)
                await db.commit()
                ok = await service.wait_for(run_row.id, timeout=600.0)
            except Exception as exc:                  # noqa: BLE001
                await db.rollback()
                print(f"  {rec.key:14}{rec.row_count:>7}{'-':>10}{'-':>10}"
                      f"{'-':>9}{'-':>9}   {type(exc).__name__}: {exc}")
                continue

            # Re-READ in a fresh session rather than refreshing this object.
            # The job runs as a background task with its own session -- it has
            # to, because it outlives the request that queued it -- so the row
            # this session holds is stale, and refreshing it across that
            # boundary raises MissingGreenlet.
            from app.models.ai import TrainingRun as RunRow
            async with factory() as rdb:
                fresh = await rdb.get(RunRow, run_row.id)
                status = fresh.status if fresh else "missing"
                m = (fresh.metrics if fresh else None) or {}
            run_row = type("R", (), {"status": status})()
            # The classification block, not the top level. metrics is keyed
            # fit / gates / baseline / economic / comparison / separation /
            # calibration / classification, and reading accuracy off the top
            # returned None for every run.
            cls = m.get("classification") or {}
            eco = m.get("economic") or {}
            acc = cls.get("accuracy")
            maj = cls.get("majority_share")
            if acc is None or maj is None:
                print(f"  {rec.key:14}{rec.row_count:>7}{'-':>10}{'-':>10}"
                      f"{'-':>9}{'-':>9}   status={run_row.status} "
                      f"{'' if ok else '(did not finish)'}")
                continue
            margin = (acc - maj) * 100
            ev = eco.get("expected_value")
            pf = eco.get("profit_factor")
            print(f"  {rec.key:14}{rec.row_count:>7}{acc*100:>9.2f}%"
                  f"{maj*100:>9.2f}%{margin:>+8.2f}pt"
                  f"{'CLEARS' if margin >= 1.50 else 'below':>9}"
                  # 6dp: a forward return here is ~1e-5, so rounding to 2
                  # printed 0.0 for every run and hid the figure entirely.
                  f"   EV {ev if ev is None else format(ev, '+.6f')}"
                  f"  PF {pf if pf is None else round(pf, 2)}"
                  f"  n {eco.get(chr(39)+chr(39)) if False else eco.get('trades')}")

    await service.shutdown()
    await engine.dispose()
    print("\n  accuracy alone means nothing: a dataset that is 70% WIN gives "
          "70% to a model\n  that always says WIN. Read the margin, and read "
          "it against the 1.50-point\n  spread hurdle rather than against zero.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keys", default="", help="comma-separated dataset keys")
    ap.add_argument("--shuffle-labels", action="store_true",
                    help="CONTROL: fit on permuted labels; a model that scores "
                         "the same here is fitting noise")
    ap.add_argument("--exclude-refuted", action="store_true",
                    help="drop the 7 features that reconstruct refuted families")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    return asyncio.run(run(keys, args.shuffle_labels, args.exclude_refuted,
                           args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
