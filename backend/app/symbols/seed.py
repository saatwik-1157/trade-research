"""Seed internal symbols and their provider mappings.

The TradingView names below are deliberately not the broker names. That is
the point of the level: `DE40` at this broker is `GER40` on TradingView, and
gold is `XAUUSD` at the broker but `OANDA:XAUUSD` in a typical alert. Seeding
them identical would hide the very failure the mapping table exists to catch.

No contract spec is seeded. Specs come from the terminal via
`app.symbols.sync_mt5`, and until they do, `contract_spec()` refuses.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.symbols.service import upsert_mapping, upsert_symbol

# internal, asset_class, base, quote, digits, point, tradingview symbol
SEED: list[tuple[str, str, str | None, str | None, int, str, str]] = [
    ("EURUSD", "fx", "EUR", "USD", 5, "0.00001", "OANDA:EURUSD"),
    ("GBPUSD", "fx", "GBP", "USD", 5, "0.00001", "OANDA:GBPUSD"),
    ("USDJPY", "fx", "USD", "JPY", 3, "0.001", "OANDA:USDJPY"),
    ("USDCAD", "fx", "USD", "CAD", 5, "0.00001", "OANDA:USDCAD"),
    ("AUDUSD", "fx", "AUD", "USD", 5, "0.00001", "OANDA:AUDUSD"),
    ("USDCHF", "fx", "USD", "CHF", 5, "0.00001", "OANDA:USDCHF"),
    ("NZDUSD", "fx", "NZD", "USD", 5, "0.00001", "OANDA:NZDUSD"),
    # Index and metal: the two the order log already contains, and the two
    # whose provider names differ most from the broker's.
    ("DE40", "index", None, "EUR", 1, "0.1", "XETR:DAX"),
    ("XAUUSD", "metal", "XAU", "USD", 2, "0.01", "OANDA:XAUUSD"),
]


async def _main() -> int:
    from app.core.settings import get_settings
    from app.db.session import make_engine, make_session_factory

    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as db:
            print(await seed_symbols(db))
        print("Specs are not seeded. Read them from a terminal:")
        print("    python -m app.symbols.sync_mt5")
        return 0
    finally:
        await engine.dispose()


async def seed_symbols(db: AsyncSession) -> dict[str, int]:
    """Idempotent. Returns how many symbols and mappings were touched."""
    counts = {"symbols": 0, "mt5_mappings": 0, "tradingview_mappings": 0}
    for code, asset_class, base, quote, digits, point, tv_symbol in SEED:
        await upsert_symbol(
            db,
            code,
            asset_class,
            base_currency=base,
            quote_currency=quote,
            digits=digits,
            point_size=Decimal(point),
        )
        counts["symbols"] += 1
        # The broker symbol happens to equal the internal code at this broker.
        # It is stored as a row rather than assumed, so a broker that appends
        # a suffix needs a row change and not a code change.
        await upsert_mapping(db, code, "mt5", code)
        counts["mt5_mappings"] += 1
        await upsert_mapping(db, code, "tradingview", tv_symbol)
        counts["tradingview_mappings"] += 1
    await db.commit()
    return counts


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(_main()))
