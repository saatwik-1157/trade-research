#!/usr/bin/env python3
"""Fill `market_bars` from MT5 so a dataset can be built that means something.

The L23 dataset was 638 rows because `market_bars` held 705, and 700 of those
were a fixture: one month of `EURUSD.R` H1 from January 2026, plus five real
EURUSD bars. A model trained on that could not demonstrate the edge it would
have to find -- at n=638 one standard error of a win rate is 1.98 points
against a 1.50-point hurdle, so the target sits inside the noise and 8,744
rows are needed at 80% power.

This ingests real H1 bars for the seven majors through the platform's own
`MarketDataService`, which is the only writer to the raw layer. It does NOT
write rows itself and does not touch the dataset builder.

**It goes through the service rather than around it**, which matters for two
reasons the service docstrings already give. `store_bars` validates every
candle and a failing one is counted and logged rather than stored or
repaired -- storing a corrupt bar puts it beyond the validator that caught
it, and repairing one is a fabricated price with a real timestamp. And the
write is idempotent on the unique key, so re-running is a no-op and a partial
run can simply be repeated.

    python tools/ingest_bars.py --dry-run
    python tools/ingest_bars.py --timeframe H1 --limit 5000
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD"]


async def run(symbols: list[str], timeframe: str, limit: int,
              dry_run: bool) -> int:
    from app.core.settings import get_settings
    from app.db.session import make_engine, make_session_factory
    from app.marketdata.providers.mt5 import MT5MarketData
    from app.marketdata.service import MarketDataService
    from app.marketdata.types import Provider, Timeframe

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)

    # A bare MarketDataService has an EMPTY provider registry -- the app wires
    # providers in at startup, and a script that skips that gets "no
    # market-data provider mt5; registered: none" rather than any data. Found
    # by dry-running before writing, which is the reason to have a dry run.
    service = MarketDataService()
    service.register(MT5MarketData())
    tf = Timeframe(timeframe)

    total_seen = total_stored = 0
    print(f"\n  ingesting {timeframe}, up to {limit} bars per symbol, "
          f"provider mt5{'  [DRY RUN]' if dry_run else ''}")
    print("  " + "-" * 62)
    print(f"  {'symbol':10}{'fetched':>9}{'stored':>9}   note")

    async with factory() as db:
        for sym in symbols:
            try:
                res = await service.get_bars(db, sym, tf, Provider.mt5,
                                             limit=limit)
            except Exception as exc:                  # noqa: BLE001
                print(f"  {sym:10}{'-':>9}{'-':>9}   "
                      f"{type(exc).__name__}: {exc}")
                continue
            # res.series.bars, NOT res.bars. The first version used
            # getattr(res, "bars", []) and the default silently produced zero
            # fetched with no error on every symbol -- the exact
            # reports-success-does-nothing shape this project keeps finding.
            # Indexed rather than getattr'd so a rename fails loudly.
            bars = list(res.series.bars)
            total_seen += len(bars)
            if dry_run:
                print(f"  {sym:10}{len(bars):>9}{'-':>9}   not written")
                continue
            # CHUNKED, because asyncpg refuses more than 32,767 query
            # arguments in one statement and market_bars has 13 columns --
            # 5,000 bars is 65,000 parameters and the whole insert fails.
            # 2,000 rows is 26,000 parameters, comfortably under. Committing
            # per chunk also means a failure part-way leaves the bars already
            # written on file, and store_bars is idempotent on its unique key
            # so the run is simply repeated.
            stored = 0
            CHUNK = 2000
            for i in range(0, len(bars), CHUNK):
                stored += await service.store_bars(db, bars[i:i + CHUNK])
                await db.commit()
            total_stored += stored
            note = "" if stored == len(bars) else f"{len(bars) - stored} already on file"
            print(f"  {sym:10}{len(bars):>9}{stored:>9}   {note}")

    await engine.dispose()
    print("  " + "-" * 62)
    print(f"  {'total':10}{total_seen:>9}{total_stored:>9}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=",".join(MAJORS))
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--limit", type=int, default=5000,
                    help="bars per symbol; the service caps this at 5000")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and count, write nothing")
    args = ap.parse_args()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(run(syms, args.timeframe, args.limit, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
