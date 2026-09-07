"""Symbol mapping: TradingView -> internal -> broker.

The theme of every test here is that a name is not a mapping. A provider
symbol that looks like a broker symbol still has to be looked up, and a miss
is an error rather than a fallback to the input string.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from app.db.base import Base
from app.models.market import Symbol, SymbolMapping
from app.symbols import service
from app.symbols.errors import (
    AmbiguousSourceSymbol,
    DuplicateMapping,
    IncompleteContractSpec,
    InvalidSymbolCode,
    NoProviderMapping,
    SymbolInactive,
    UnknownSourceSymbol,
    UnknownSymbol,
)
from app.symbols.seed import seed_symbols
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# A complete spec, shaped like what the terminal returns for a 5-digit pair.
FULL_SPEC = {
    "contract_size": Decimal("100000"),
    "tick_size": Decimal("0.00001"),
    "tick_value": Decimal("1.0"),
    "minimum_volume": Decimal("0.01"),
    "maximum_volume": Decimal("500"),
    "volume_step": Decimal("0.01"),
    "price_precision": 5,
    "volume_precision": 2,
    "trading_hours": {"timezone": "broker_server", "quote_sessions": {"monday": []}},
    "spec_source": "mt5_terminal",
}


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def seeded(db: AsyncSession) -> AsyncSession:
    await seed_symbols(db)
    return db


# ------------------------------------------------------------- normalisation


@pytest.mark.parametrize("bad", ["", "   ", "EUR USD", "A" * 33])
def test_invalid_codes_are_refused(bad: str) -> None:
    with pytest.raises(InvalidSymbolCode):
        service.normalise_code(bad)


def test_codes_are_trimmed_and_upper_cased() -> None:
    assert service.normalise_code("  eurusd ") == "EURUSD"


def test_non_string_code_is_refused() -> None:
    with pytest.raises(InvalidSymbolCode):
        service.normalise_code(None)  # type: ignore[arg-type]


def test_unknown_provider_is_refused() -> None:
    with pytest.raises(InvalidSymbolCode):
        service.check_provider("metatrader")


def test_exchange_prefix_stripping() -> None:
    assert service.strip_exchange_prefix("OANDA:EURUSD") == "EURUSD"
    assert service.strip_exchange_prefix("EURUSD") == "EURUSD"


# ---------------------------------------------------------------- resolution


async def test_seed_is_idempotent_and_maps_three_names(seeded: AsyncSession) -> None:
    counts = await seed_symbols(seeded)
    assert counts["symbols"] == 9
    symbols = (await seeded.scalars(select(Symbol))).all()
    assert len(symbols) == 9
    mappings = (await seeded.scalars(select(SymbolMapping))).all()
    assert len(mappings) == 18  # one mt5 and one tradingview row per symbol


async def test_tradingview_name_is_not_the_broker_name(seeded: AsyncSession) -> None:
    # The case the level exists for: DE40 at the broker, DAX on TradingView.
    assert await service.translate(seeded, "tradingview", "XETR:DAX", "mt5") == "DE40"
    assert await service.translate(seeded, "tradingview", "OANDA:XAUUSD", "mt5") == "XAUUSD"
    symbol = await service.resolve_source(seeded, "tradingview", "XETR:DAX")
    assert symbol.code == "DE40"
    assert symbol.asset_class == "index"


async def test_resolution_accepts_both_prefixed_and_bare_forms(seeded: AsyncSession) -> None:
    assert (await service.resolve_source(seeded, "tradingview", "OANDA:EURUSD")).code == "EURUSD"
    # A bare {{ticker}} still resolves through the prefix-stripped fallback.
    assert (await service.resolve_source(seeded, "tradingview", "eurusd")).code == "EURUSD"


async def test_prefixed_mapping_wins_over_stripped_fallback(db: AsyncSession) -> None:
    await service.upsert_symbol(db, "EURUSD", "fx")
    await service.upsert_symbol(db, "EURUSD_SYNTH", "fx")
    await service.upsert_mapping(db, "EURUSD_SYNTH", "tradingview", "EURUSD")
    await service.upsert_mapping(db, "EURUSD", "tradingview", "OANDA:EURUSD")
    await db.commit()
    # Exact match must win: the more specific row is the one that was stored.
    assert (await service.resolve_source(db, "tradingview", "OANDA:EURUSD")).code == "EURUSD"
    assert (await service.resolve_source(db, "tradingview", "EURUSD")).code == "EURUSD_SYNTH"


async def test_bare_ticker_matching_two_exchanges_is_refused_not_guessed(
    db: AsyncSession,
) -> None:
    await service.upsert_symbol(db, "AAPL_US", "equity")
    await service.upsert_symbol(db, "AAPL_XETRA", "equity")
    await service.upsert_mapping(db, "AAPL_US", "tradingview", "NASDAQ:AAPL")
    await service.upsert_mapping(db, "AAPL_XETRA", "tradingview", "XETR:AAPL")
    await db.commit()
    with pytest.raises(AmbiguousSourceSymbol) as exc:
        await service.resolve_source(db, "tradingview", "AAPL")
    assert "matches 2 instruments" in str(exc.value)
    # Qualified, it resolves cleanly.
    assert (await service.resolve_source(db, "tradingview", "NASDAQ:AAPL")).code == "AAPL_US"


async def test_unknown_source_symbol_raises_rather_than_passing_it_through(
    seeded: AsyncSession,
) -> None:
    with pytest.raises(UnknownSourceSymbol) as exc:
        await service.resolve_source(seeded, "tradingview", "BINANCE:DOGEUSDT")
    assert "no mapping" in str(exc.value)


async def test_unknown_internal_symbol_raises(seeded: AsyncSession) -> None:
    with pytest.raises(UnknownSymbol):
        await service.get_symbol(seeded, "NOTASYMBOL")


async def test_symbol_without_broker_mapping_raises(db: AsyncSession) -> None:
    await service.upsert_symbol(db, "BTCUSD", "crypto")
    await db.commit()
    with pytest.raises(NoProviderMapping):
        await service.broker_symbol_for(db, "BTCUSD", "mt5")


async def test_inactive_mapping_and_symbol_are_refused(seeded: AsyncSession) -> None:
    mapping = await service.mapping_for(seeded, "EURUSD", "tradingview")
    mapping.is_active = False
    await seeded.commit()
    with pytest.raises(SymbolInactive):
        await service.resolve_source(seeded, "tradingview", "OANDA:EURUSD")
    assert (
        await service.resolve_source(seeded, "tradingview", "OANDA:EURUSD", allow_inactive=True)
    ).code == "EURUSD"

    mapping.is_active = True
    symbol = await service.get_symbol(seeded, "EURUSD")
    symbol.is_active = False
    await seeded.commit()
    with pytest.raises(SymbolInactive):
        await service.resolve_source(seeded, "tradingview", "OANDA:EURUSD")


async def test_remapping_a_provider_symbol_to_another_instrument_is_refused(
    seeded: AsyncSession,
) -> None:
    with pytest.raises(DuplicateMapping) as exc:
        await service.upsert_mapping(seeded, "GBPUSD", "tradingview", "OANDA:EURUSD")
    assert "already mapped to EURUSD" in str(exc.value)


async def test_updating_the_same_mapping_is_allowed(seeded: AsyncSession) -> None:
    await service.upsert_mapping(seeded, "EURUSD", "mt5", "EURUSD.r")
    await seeded.commit()
    assert await service.broker_symbol_for(seeded, "EURUSD") == "EURUSD.R"
    # And the TradingView side is untouched by a broker-side rename.
    assert (await service.resolve_source(seeded, "tradingview", "OANDA:EURUSD")).code == "EURUSD"


async def test_unknown_mapping_field_is_refused(seeded: AsyncSession) -> None:
    with pytest.raises(InvalidSymbolCode):
        await service.upsert_mapping(seeded, "EURUSD", "mt5", "EURUSD", lot_size=1)


# -------------------------------------------------------------- contract spec


async def test_spec_is_refused_until_it_is_synced(seeded: AsyncSession) -> None:
    with pytest.raises(IncompleteContractSpec) as exc:
        await service.contract_spec(seeded, "EURUSD")
    message = str(exc.value)
    assert "tick_value" in message and "volume_step" in message
    assert "assuming a default" in message


async def test_partial_spec_names_only_what_is_missing(seeded: AsyncSession) -> None:
    partial = dict(FULL_SPEC)
    del partial["tick_value"]
    await service.upsert_mapping(seeded, "EURUSD", "mt5", "EURUSD", **partial)
    await seeded.commit()
    with pytest.raises(IncompleteContractSpec) as exc:
        await service.contract_spec(seeded, "EURUSD")
    assert "tick_value" in str(exc.value)
    assert "tick_size" not in str(exc.value)


async def test_complete_spec_round_trips_with_provenance(seeded: AsyncSession) -> None:
    await service.upsert_mapping(seeded, "EURUSD", "mt5", "EURUSD", **FULL_SPEC)
    await seeded.commit()
    spec = await service.contract_spec(seeded, "EURUSD")
    assert spec.internal_symbol == "EURUSD"
    assert spec.broker_symbol == "EURUSD"
    assert spec.contract_size == Decimal("100000")
    assert spec.tick_value == Decimal("1.0")
    assert spec.volume_step == Decimal("0.01")
    assert spec.price_precision == 5 and spec.volume_precision == 2
    assert spec.trading_hours is not None
    assert spec.spec_source == "mt5_terminal"
    assert spec.spec_updated_at is not None


async def test_spec_is_per_provider_not_per_instrument(seeded: AsyncSession) -> None:
    # The same instrument at a second venue carries its own terms.
    await service.upsert_mapping(seeded, "EURUSD", "mt5", "EURUSD", **FULL_SPEC)
    await service.upsert_mapping(
        seeded, "EURUSD", "ccxt", "EUR/USD", **{**FULL_SPEC, "minimum_volume": Decimal("1")}
    )
    await seeded.commit()
    assert (await service.contract_spec(seeded, "EURUSD", "mt5")).minimum_volume == Decimal("0.01")
    assert (await service.contract_spec(seeded, "EURUSD", "ccxt")).minimum_volume == Decimal("1")


# ------------------------------------------------------------ spec extraction


def test_volume_precision_is_derived_from_the_step() -> None:
    from app.symbols.sync_mt5 import _precision_of

    assert _precision_of("0.01") == 2
    assert _precision_of("0.001") == 3
    assert _precision_of("1") == 0
    assert _precision_of(None) is None


def test_absent_session_api_yields_no_hours_rather_than_a_default() -> None:
    from app.symbols.sync_mt5 import read_sessions, sessions_api_available

    class NoSessionApi:
        """Build 5.0.6090: no symbol_info_session_quote at all."""

    stub = NoSessionApi()
    assert sessions_api_available(stub) is False
    # None means "no schedule known", never "open around the clock".
    assert read_sessions(stub, "EURUSD") is None


def test_session_api_when_present_is_read_per_weekday() -> None:
    from app.symbols.sync_mt5 import read_sessions

    class Slot:
        def __init__(self, start: int, end: int) -> None:
            setattr(self, "from", start)
            self.to = end

    class WithSessions:
        def symbol_info_session_quote(self, symbol: str, day: int, slot: int):  # noqa: ANN202
            # Monday only, one session 09:00-17:30.
            if day == 1 and slot == 0:
                return Slot(9 * 3600, 17 * 3600 + 1800)
            return None

    hours = read_sessions(WithSessions(), "EURUSD")
    assert hours == {
        "timezone": "broker_server",
        "quote_sessions": {"monday": [{"from": "09:00", "to": "17:30"}]},
    }


def test_zero_spec_values_read_as_missing_not_as_zero() -> None:
    from app.symbols.sync_mt5 import _decimal

    # A terminal that returns 0 for tick value has told us nothing, and a
    # zero tick value would silently divide a position size to nothing.
    assert _decimal(0) is None
    assert _decimal(0.0) is None
    assert _decimal("1.5") == Decimal("1.5")
    assert _decimal(None) is None
