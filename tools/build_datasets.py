#!/usr/bin/env python3
"""Build one L23 dataset per major from the bars now in `market_bars`.

`ingest_bars.py` took the raw layer from 705 rows to 35,700. This turns them
into datasets: quality -> features (<= T) -> labels (> T) -> alignment ->
chronological split -> scaler fitted on train only -> leakage checks, and
READY only if those pass.

One dataset PER SYMBOL, because `DatasetConfig` takes a single symbol on
purpose: a dataset pooling two instruments has to answer the unit question
first, and the project has the metals-points error on file as what happens
when it is not answered. Multi-symbol training combines single-symbol
datasets, keeping provenance per row.

**The spread is stated, not defaulted.** `LabelConfig.spread_points` has no
default, deliberately, because the one effect this repository has measured to
significance is what happens when cost is left implicit: a WIN label computed
without the spread is a label for a market nobody trades in. The figure used
here is the same measured MEDIAN spread `cost_hurdle.py` and `rule_search.py`
use -- taken from the bars' own recorded spread, not from a live quote, since
a single quote can understate this broker by 3-8x.

    python tools/build_datasets.py --dry-run
    python tools/build_datasets.py --limit 5000
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD"]
# A real FX spread is single digits to low double digits of points on this
# broker (EURUSD 2, USDCHF 7, worst observed 112). Anything past this is a
# unit error, not an expensive instrument.
IMPLAUSIBLE_POINTS = Decimal("1000")

# MT5 point size: JPY pairs quote to three decimals, the rest to five.
POINT_SIZE = {"USDJPY": Decimal("0.001")}
DEFAULT_POINT_SIZE = Decimal("0.00001")


def point_size(symbol: str) -> Decimal:
    """Price movement of one point, used to turn a point spread into a price."""
    return POINT_SIZE.get(symbol, DEFAULT_POINT_SIZE)


def median_spread_points(rows, symbol: str) -> tuple[Decimal | None, str]:
    """Median recorded spread, ALREADY IN POINTS, from the bars themselves.

    Bars recording a zero spread are UNRECORDED rather than free -- averaging
    them in halves the apparent cost of trading, which `CLAUDE.md` records as
    a measured error. They are excluded and counted.

    **`market_bars.spread` holds two different units and the column cannot
    tell you which.** The MT5 provider stores POINTS -- `providers/mt5.py`
    reads MT5's own integer `spread` field and writes
    `spread=Decimal(spread_points)` -- while the `EURUSD.R` fixture already on
    file stores PRICE (2e-05). The first version of this file divided by the
    point size, which is right for the fixture and wrong for every real bar,
    and produced a EURUSD spread of 200,000 points against a true 2. That is
    the metals-points error again, one column lower down, and a label built on
    it would have priced a cost 100,000x too high.

    The sanity fence is the reason it was caught rather than believed: a real
    FX spread is single-digit to low-double-digit points, so anything above
    1,000 is a unit error rather than an expensive instrument.
    """
    vals = [r.spread for r in rows if r.spread is not None and r.spread > 0]
    if not vals:
        return None, "no bar recorded a spread; cannot state a cost"
    vals = sorted(vals)
    med = Decimal(vals[len(vals) // 2]).quantize(Decimal("0.1"))
    if med > IMPLAUSIBLE_POINTS:
        return None, (f"median spread {med} points is implausible for FX; "
                      "the column is probably holding price, not points")
    return med, f"median of {len(vals)} bars that recorded one"


async def run(symbols: list[str], limit: int, dry_run: bool) -> int:
    from app.core.settings import get_settings
    from app.datasets import builder as build_mod
    from app.datasets import service as ds_service
    from app.datasets import labels as label_engine
    from app.db.session import make_engine, make_session_factory
    from app.marketdata.service import MarketDataService
    from app.marketdata.types import (Availability, Bar, Provider,
                                      Timeframe)

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    service = MarketDataService()
    tf = Timeframe.H1

    print(f"\n  building one dataset per symbol from market_bars"
          f"{'  [DRY RUN]' if dry_run else ''}")
    print("  " + "-" * 74)
    print(f"  {'symbol':9}{'bars':>7}{'spread':>9}{'rows':>8}{'train':>8}"
          f"{'status':>9}   note")

    total_rows = 0
    results = []
    async with factory() as db:
        for sym in symbols:
            stored = await service.stored_bars(db, Provider.mt5, sym, tf,
                                               limit=limit)
            if not stored:
                print(f"  {sym:9}{0:>7}{'-':>9}{'-':>8}{'-':>8}{'-':>9}   "
                      "no bars on file")
                continue
            spread, note = median_spread_points(stored, sym)
            if spread is None:
                print(f"  {sym:9}{len(stored):>7}{'-':>9}{'-':>8}{'-':>8}"
                      f"{'REFUSED':>9}   {note}")
                continue

            # bar_time, not time: the field name is load-bearing. It is the
            # bar's OPEN time, and a provider stamping closes would shift
            # every signal by one bar. Also carry spread_availability through
            # rather than letting it default, so the label's cost figure is
            # traceable to a bar that actually recorded one.
            bars = [
                Bar(symbol=sym, provider=Provider.mt5, timeframe=tf,
                    bar_time=r.bar_time, open=r.open, high=r.high, low=r.low,
                    close=r.close, volume=r.volume, spread=r.spread,
                    spread_availability=Availability(r.spread_availability))
                for r in stored
            ]
            cfg = build_mod.DatasetConfig(
                key=f"{sym.lower()}_h1_v2",
                symbol=sym,
                timeframe=tf,
                provider=Provider.mt5,
                provider_symbol=sym,
                label_config=label_engine.LabelConfig(
                    spread_points=spread * point_size(sym)),
            )
            if dry_run:
                print(f"  {sym:9}{len(bars):>7}{str(spread):>9}{'-':>8}"
                      f"{'-':>8}{'-':>9}   not built")
                continue
            try:
                # build_and_register, not build: the in-memory object is
                # discarded when the process ends, and `project_state` reads
                # the `datasets` table. A dataset nobody recorded is one
                # nobody can train from or reproduce.
                ds, record = await ds_service.build_and_register(
                    db,
                    key=cfg.key,
                    symbol=sym,
                    provider_symbol=sym,
                    provider=Provider.mt5,
                    timeframe=tf,
                    horizon=cfg.label_config.horizon,
                    # PRICE, not points. LabelConfig.spread_points is
                    # subtracted straight from a close, so the value it wants
                    # is points x point size. Passing 2.0 for EURUSD gave
                    # 1.17 - 2.0 and a forward return of -1.71 on every row.
                    spread_points=spread * point_size(sym),
                    limit=limit,
                )
                await db.commit()
            except Exception as exc:                  # noqa: BLE001
                await db.rollback()
                print(f"  {sym:9}{len(bars):>7}{str(spread):>9}{'-':>8}"
                      f"{'-':>8}{'FAILED':>9}   {type(exc).__name__}: {exc}")
                continue
            n = len(ds.rows)
            total_rows += n
            # Split.train is a HALF-OPEN (start, stop) INDEX PAIR, not a list
            # of rows -- indices rather than copies, so a row cannot land in
            # two segments. len() on it is 2, which the first version of this
            # script printed as the training size for every symbol. Another
            # getattr-with-a-default reading the wrong shape and reporting a
            # plausible-looking number instead of failing.
            lo, hi = ds.split.train
            n_train = hi - lo
            status = str(getattr(ds, "status", "?")).split(".")[-1]
            leak = getattr(ds, "leakage", None)
            bad = [f.name for f in getattr(leak, "findings", []) or []
                   if getattr(f, "blocking", False)] if leak else []
            print(f"  {sym:9}{len(bars):>7}{str(spread):>9}{n:>8}{n_train:>8}"
                  f"{status:>9}   " + (", ".join(bad) if bad else note))
            results.append((sym, n, status))

    await engine.dispose()
    print("  " + "-" * 74)
    print(f"  {'total':9}{'':>7}{'':>9}{total_rows:>8}")
    if results:
        ready = sum(1 for _s, _n, st in results if st.upper() == "READY")
        print(f"\n  {ready} of {len(results)} datasets READY, {total_rows:,} rows total")
        print("  8,711 rows are needed at 80% power to demonstrate the "
              "1.50-point spread hurdle.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=",".join(MAJORS))
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(run(syms, args.limit, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
