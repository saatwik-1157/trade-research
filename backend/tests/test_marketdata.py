"""Market data: normalization, validation, staleness, idempotency, routes.

The provider adapters wrap network and terminal dependencies, so the ones
exercised here are the deterministic simulator and hand-built bars. What is
tested is the layer that decides whether data is usable -- which is the part
that has to be right, because a corrupt bar that reaches a strategy is a
finding that never happened.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.auth.models import Role, User
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.marketdata.base import HistoricalProvider, ProviderStatus, ProviderUnavailable
from app.marketdata.providers.simulator import SimulatorMarketData
from app.marketdata.service import MarketDataService
from app.marketdata.types import (
    Availability,
    Bar,
    Provider,
    Quote,
    Timeframe,
    TimeframeError,
    parse_timeframe,
    seconds_of,
)
from app.marketdata.validation import (
    check_bar,
    check_quote,
    deduplicate,
    find_duplicates,
    find_gaps,
    find_out_of_order,
    inspect_series,
    is_stale,
    ohlc_is_consistent,
    open_equals_close_rate,
    staleness_limit,
)
from app.models.market import MarketBar, Symbol, SymbolMapping
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def bar(
    *,
    o: str = "1.1000",
    h: str = "1.1020",
    low: str = "1.0990",
    c: str = "1.1010",
    at: datetime | None = None,
    provider: Provider = Provider.simulator,
    timeframe: Timeframe = Timeframe.H1,
    spread: str | None = None,
    complete: bool = True,
) -> Bar:
    return Bar(
        symbol="EURUSD",
        provider=provider,
        timeframe=timeframe,
        bar_time=at or T0,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("100"),
        spread=Decimal(spread) if spread is not None else None,
        spread_availability=(
            Availability.available if spread is not None else Availability.not_available
        ),
        complete=complete,
    )


# ------------------------------------------------------------- timeframes


@pytest.mark.parametrize("raw,expected", [("h1", Timeframe.H1), ("D1", Timeframe.D1)])
def test_timeframes_normalize(raw: str, expected: Timeframe) -> None:
    assert parse_timeframe(raw) is expected


@pytest.mark.parametrize("raw", ["H2", "", "1h", "daily"])
def test_an_unsupported_timeframe_is_refused_not_mapped_to_a_nearby_one(raw: str) -> None:
    with pytest.raises(TimeframeError):
        parse_timeframe(raw)


def test_timeframe_seconds_are_defined_for_every_timeframe() -> None:
    for timeframe in Timeframe:
        assert seconds_of(timeframe) > 0


# ------------------------------------------------------- absent vs. zero


def test_an_absent_spread_stays_absent() -> None:
    """A recorded 0 is an UNRECORDED spread, not a free trade. Averaging those
    zeros in halves the apparent cost of trading -- measured, in
    cost_profile.json."""
    without = bar()
    assert without.spread is None
    assert without.spread_availability is Availability.not_available

    with_spread = bar(spread="0.00012")
    assert with_spread.spread == Decimal("0.00012")
    assert with_spread.spread_availability is Availability.available


def test_a_quote_spread_is_derived_and_null_when_a_side_is_missing() -> None:
    both = Quote(
        symbol="EURUSD",
        provider=Provider.mt5,
        at=T0,
        received_at=T0,
        bid=Decimal("1.1000"),
        ask=Decimal("1.1002"),
    )
    assert both.spread == Decimal("0.0002")
    assert both.as_dict()["spread_availability"] == "derived"

    one_sided = Quote(
        symbol="EURUSD", provider=Provider.mt5, at=T0, received_at=T0, bid=Decimal("1.1000")
    )
    assert one_sided.spread is None
    assert one_sided.mid is None


# --------------------------------------------------------- OHLC validation


def test_a_consistent_bar_passes() -> None:
    assert ohlc_is_consistent(bar())
    assert check_bar(bar(), now=T0 + timedelta(hours=1)) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"c": "1.1050"},  # close above high
        {"c": "1.0900"},  # close below low
        {"o": "1.1050"},  # open above high
        {"h": "1.0980", "low": "1.0990"},  # high below low
    ],
)
def test_an_impossible_bar_is_flagged(kwargs: dict) -> None:
    """This is the Yahoo FX defect. Left unvalidated it produced a t-statistic
    of 28 in this repository's own forex pattern study -- an artefact that
    reads exactly like a spectacular edge."""
    assert not ohlc_is_consistent(bar(**kwargs))
    assert any("OHLC inconsistent" in p for p in check_bar(bar(**kwargs)))


def test_non_positive_prices_are_flagged() -> None:
    problems = check_bar(bar(low="0", o="0"))
    assert any("not a positive price" in p for p in problems)


def test_a_future_stamped_bar_is_flagged() -> None:
    problems = check_bar(bar(at=T0 + timedelta(hours=2)), now=T0)
    assert any("in the future" in p for p in problems)


def test_a_crossed_quote_is_flagged() -> None:
    crossed = Quote(
        symbol="EURUSD",
        provider=Provider.mt5,
        at=T0,
        received_at=T0,
        bid=Decimal("1.1005"),
        ask=Decimal("1.1000"),
    )
    assert any("crossed quote" in p for p in check_quote(crossed))


def test_validation_does_not_modify_the_bar() -> None:
    """Flag, never silently repair. A quietly repaired bar is a repair nobody
    can find later."""
    broken = bar(c="1.1050")
    check_bar(broken)
    assert broken.close == Decimal("1.1050")
    assert broken.high == Decimal("1.1020")


def test_clamping_is_explicit_and_only_moves_the_extremes() -> None:
    from app.marketdata.validation import clamp_ohlc

    repaired = clamp_ohlc(bar(c="1.1050"))
    assert repaired.high == Decimal("1.1050")
    assert repaired.close == Decimal("1.1050")  # the better-sourced value is kept
    assert repaired.open == Decimal("1.1000")


# ---------------------------------------------- duplicates and ordering


def test_duplicates_are_found_not_raised() -> None:
    bars = [bar(at=T0), bar(at=T0), bar(at=T0 + timedelta(hours=1))]
    assert find_duplicates(bars) == [T0]


def test_deduplicate_keeps_the_last_and_sorts() -> None:
    """Last wins: a provider re-sending a bar is usually correcting it."""
    first = bar(at=T0, c="1.1010")
    corrected = bar(at=T0, c="1.1030")
    later = bar(at=T0 + timedelta(hours=1))
    out = deduplicate([later, first, corrected])
    assert [b.bar_time for b in out] == [T0, T0 + timedelta(hours=1)]
    assert out[0].close == Decimal("1.1030")


def test_out_of_order_bars_are_reported() -> None:
    bars = [bar(at=T0 + timedelta(hours=1)), bar(at=T0)]
    assert find_out_of_order(bars) == [T0]


def test_gaps_are_counted_never_filled() -> None:
    """A synthesised bar is indistinguishable from a real one by eye."""
    bars = [bar(at=T0), bar(at=T0 + timedelta(hours=4))]
    assert find_gaps(bars, Timeframe.H1) == 3


def test_open_equals_close_rate_detects_a_synthesised_series() -> None:
    """Real bars almost never do it; Yahoo's FX series does on ~40% of bars,
    because those opens are filled in rather than observed."""
    real = [bar(at=T0 + timedelta(hours=i)) for i in range(10)]
    assert open_equals_close_rate(real) == 0.0
    synthetic = [bar(at=T0 + timedelta(hours=i), o="1.1000", c="1.1000") for i in range(10)]
    assert open_equals_close_rate(synthetic) == 1.0


def test_a_series_report_marks_untrustworthy_ohlc_and_says_what_to_do() -> None:
    synthetic = [bar(at=T0 + timedelta(hours=i), o="1.1000", c="1.1000") for i in range(10)]
    quality = inspect_series(synthetic, Timeframe.H1)
    assert quality.ohlc_trustworthy is False
    assert any("Close-based analysis is fine" in n for n in quality.notes)


def test_a_clean_series_is_reported_clean() -> None:
    bars = [bar(at=T0 + timedelta(hours=i)) for i in range(24)]
    quality = inspect_series(bars, Timeframe.H1, now=T0 + timedelta(days=2))
    assert quality.ohlc_trustworthy is True
    assert quality.duplicates == quality.out_of_order == quality.gaps == 0
    assert quality.notes == []


# ------------------------------------------------------------- staleness


def test_the_stale_threshold_scales_with_the_timeframe() -> None:
    """One universal threshold would be wrong for every instrument but one."""
    assert staleness_limit(Timeframe.D1) > staleness_limit(Timeframe.H1)
    assert staleness_limit(Timeframe.H1) > staleness_limit(Timeframe.M1)


def test_a_fresh_series_is_not_stale_and_an_old_one_is() -> None:
    now = T0 + timedelta(hours=1)
    assert is_stale(T0, Timeframe.H1, now) is False
    assert is_stale(T0, Timeframe.H1, T0 + timedelta(hours=5)) is True
    # A D1 bar an hour old is fresh.
    assert is_stale(T0, Timeframe.D1, now) is False


def test_a_closed_market_is_not_a_stale_feed() -> None:
    """Reporting a quiet weekend FX feed as a fault would train an operator to
    ignore the alert that matters."""
    assert is_stale(T0, Timeframe.M1, T0 + timedelta(days=2), market_open=False) is False
    # Unknown session is treated as open: an unnoticed dead feed is worse.
    assert is_stale(T0, Timeframe.M1, T0 + timedelta(days=2), market_open=None) is True


# ------------------------------------------------------------- simulator


async def test_the_simulator_refuses_to_run_in_production() -> None:
    with pytest.raises(RuntimeError, match="refuses to run in production"):
        SimulatorMarketData(environment="production")


async def test_the_simulator_labels_everything_it_produces() -> None:
    sim = SimulatorMarketData()
    bars = await sim.get_bars("EURUSD", Timeframe.H1, limit=10)
    assert all(b.provider is Provider.simulator for b in bars)
    quote = await sim.get_quote("EURUSD")
    assert quote.provider is Provider.simulator
    status = await sim.status()
    assert "SIMULATED DATA" in status.detail


async def test_the_simulator_is_deterministic() -> None:
    """A simulator that sometimes produced different data would make a test's
    outcome depend on a coin flip."""
    a = await SimulatorMarketData().get_bars("EURUSD", Timeframe.H1, limit=20, end=T0)
    b = await SimulatorMarketData().get_bars("EURUSD", Timeframe.H1, limit=20, end=T0)
    assert [x.close for x in a] == [x.close for x in b]


async def test_the_simulator_produces_valid_bars() -> None:
    bars = await SimulatorMarketData().get_bars("EURUSD", Timeframe.H1, limit=200, end=T0)
    assert all(ohlc_is_consistent(b) for b in bars)
    assert inspect_series(bars, Timeframe.H1).ohlc_trustworthy is True


async def test_the_simulator_reports_no_spread_rather_than_zero() -> None:
    """A fabricated cost is the one number a backtest must never be handed."""
    bars = await SimulatorMarketData().get_bars("EURUSD", Timeframe.H1, limit=5, end=T0)
    assert all(b.spread is None for b in bars)
    assert all(b.spread_availability is Availability.not_available for b in bars)


# ------------------------------------------------- service and persistence


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    async with application.state.session_factory() as db:
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        db.add(
            SymbolMapping(
                id="map-sim",
                symbol_id="sym-eur",
                provider="simulator",
                provider_symbol="EURUSD",
            )
        )
        db.add(
            SymbolMapping(
                id="map-mt5", symbol_id="sym-eur", provider="mt5", provider_symbol="EURUSD.R"
            )
        )
        await db.commit()
    # Only the simulator: a test that reached a terminal or a network would be
    # testing the terminal or the network.
    application.state.market_data = MarketDataService({Provider.simulator: SimulatorMarketData()})
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def signed_in(client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    return client


async def _admin(app: FastAPI, client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert user is not None
        user.role = Role.admin.value
        await db.commit()
    return client


async def test_the_service_translates_the_symbol_and_never_passes_it_through(
    app: FastAPI,
) -> None:
    service = app.state.market_data
    async with app.state.session_factory() as db:
        assert await service.provider_symbol(db, "EURUSD", Provider.mt5) == "EURUSD.R"


async def test_an_unmapped_symbol_is_refused(app: FastAPI) -> None:
    from app.symbols.errors import UnknownSymbol

    service = app.state.market_data
    async with app.state.session_factory() as db:
        with pytest.raises(UnknownSymbol):
            await service.provider_symbol(db, "NOTASYMBOL", Provider.simulator)


async def test_storing_bars_twice_writes_them_once(app: FastAPI) -> None:
    """A provider retry or a reconnect replays bars as a matter of course."""
    service = app.state.market_data
    bars = await SimulatorMarketData().get_bars("EURUSD", Timeframe.H1, limit=50, end=T0)
    async with app.state.session_factory() as db:
        first = await service.store_bars(db, bars, symbol_id="sym-eur")
        await db.commit()
        second = await service.store_bars(db, bars, symbol_id="sym-eur")
        await db.commit()
        total = await db.scalar(select(func.count()).select_from(MarketBar))
    assert first == 50
    assert second == 0
    assert total == 50


async def test_an_invalid_bar_is_not_stored_and_not_repaired(app: FastAPI) -> None:
    service = app.state.market_data
    async with app.state.session_factory() as db:
        stored = await service.store_bars(db, [bar(c="1.1050")], symbol_id="sym-eur")
        await db.commit()
        total = await db.scalar(select(func.count()).select_from(MarketBar))
    assert stored == 0
    assert total == 0


async def test_a_forming_bar_is_not_stored(app: FastAPI) -> None:
    """A forming bar changes under the reader; a strategy that saw it would be
    reading the future."""
    service = app.state.market_data
    async with app.state.session_factory() as db:
        stored = await service.store_bars(db, [bar(complete=False)], symbol_id="sym-eur")
        await db.commit()
    assert stored == 0


async def test_stored_bars_come_back_oldest_first(app: FastAPI) -> None:
    service = app.state.market_data
    bars = await SimulatorMarketData().get_bars("EURUSD", Timeframe.H1, limit=10, end=T0)
    async with app.state.session_factory() as db:
        await service.store_bars(db, bars, symbol_id="sym-eur")
        await db.commit()
        rows = await service.stored_bars(db, Provider.simulator, "EURUSD", Timeframe.H1)
    times = [r.bar_time for r in rows]
    assert times == sorted(times)


async def test_a_failing_provider_falls_back_only_when_asked_and_says_so(
    app: FastAPI,
) -> None:
    """Failover is controlled: the substitute is named in the response and the
    two providers' bars are never merged."""

    class Broken(HistoricalProvider):
        name = Provider.mt5
        timeframes = (Timeframe.H1,)

        async def status(self) -> ProviderStatus:
            return ProviderStatus(self.name, False, "down", False, True, self.timeframes)

        async def get_bars(self, provider_symbol, timeframe, *, limit=500, end=None):  # noqa: ANN001, ANN201
            raise ProviderUnavailable("terminal not running")

    service = MarketDataService({Provider.mt5: Broken(), Provider.simulator: SimulatorMarketData()})
    async with app.state.session_factory() as db:
        # No fallbacks passed: the failure is surfaced, not papered over.
        with pytest.raises(ProviderUnavailable):
            await service.get_bars(db, "EURUSD", Timeframe.H1, Provider.mt5, limit=5)
        # Asked for explicitly: it works and the answer says which book it is.
        result = await service.get_bars(
            db, "EURUSD", Timeframe.H1, Provider.mt5, limit=5, fallbacks=(Provider.simulator,)
        )
    assert result.provider_used is Provider.simulator
    assert result.fell_back is True
    assert all(b.provider is Provider.simulator for b in result.series.bars)


async def test_a_historical_only_provider_will_not_serve_a_quote(app: FastAPI) -> None:
    """A delayed daily close dressed up as a live price is the substitution the
    normalization layer exists to prevent."""
    from app.core.errors import ValidationFailed

    class HistoryOnly(HistoricalProvider):
        name = Provider.yfinance
        timeframes = (Timeframe.D1,)

        async def status(self) -> ProviderStatus:
            return ProviderStatus(self.name, True, "ok", False, True, self.timeframes)

        async def get_bars(self, provider_symbol, timeframe, *, limit=500, end=None):  # noqa: ANN001, ANN201
            return []

    service = MarketDataService({Provider.yfinance: HistoryOnly()})
    async with app.state.session_factory() as db:
        with pytest.raises(ValidationFailed, match="does not serve quotes"):
            await service.get_quote(db, "EURUSD", Provider.yfinance)


# ----------------------------------------------------------------- routes


async def test_market_routes_need_a_session(client: AsyncClient) -> None:
    for path in ("/v1/market/providers", "/v1/market/quotes", "/v1/market/candles"):
        assert (await client.get(path, params={"symbol": "EURUSD"})).status_code == 401


async def test_providers_route_reports_usability(signed_in: AsyncClient) -> None:
    body = (await signed_in.get("/v1/market/providers")).json()
    assert body["providers"][0]["provider"] == "simulator"
    assert body["providers"][0]["usable"] is True


async def test_candles_route_returns_bars_with_a_quality_report(
    signed_in: AsyncClient,
) -> None:
    r = await signed_in.get(
        "/v1/market/candles",
        params={"symbol": "EURUSD", "provider": "simulator", "timeframe": "H1", "limit": 20},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["bars"]) == 20
    assert body["provider_used"] == "simulator"
    assert body["fell_back"] is False
    assert body["quality"]["ohlc_trustworthy"] is True
    # The label travels all the way to the response.
    assert all(b["provider"] == "simulator" for b in body["bars"])


async def test_candles_route_stores_and_never_duplicates_a_bar_time(
    app: FastAPI, signed_in: AsyncClient
) -> None:
    """Two calls a moment apart are two *different* windows -- the simulator
    anchors on now, as a live feed does -- so the second call storing new rows
    is correct. What must hold is that no bar time is ever stored twice, which
    is what the unique key guarantees and what this asserts."""
    params: dict[str, str | int] = {
        "symbol": "EURUSD",
        "provider": "simulator",
        "limit": 10,
        "store": "true",
    }
    first = (await signed_in.get("/v1/market/candles", params=params)).json()
    second = (await signed_in.get("/v1/market/candles", params=params)).json()
    assert first["stored"] == 10
    assert second["stored"] <= 10

    async with app.state.session_factory() as db:
        rows = (await db.scalars(select(MarketBar.bar_time))).all()
    assert len(rows) == len(set(rows))


async def test_stored_route_says_it_is_a_cache(signed_in: AsyncClient) -> None:
    await signed_in.get(
        "/v1/market/candles",
        params={"symbol": "EURUSD", "provider": "simulator", "limit": 5, "store": "true"},
    )
    body = (
        await signed_in.get(
            "/v1/market/candles/stored", params={"symbol": "EURUSD", "provider": "simulator"}
        )
    ).json()
    assert body["count"] == 5
    assert "cache" in body["source"]
    assert body["newest_bar_time"] is not None


async def test_an_unknown_timeframe_is_refused_by_the_route(signed_in: AsyncClient) -> None:
    r = await signed_in.get(
        "/v1/market/candles",
        params={"symbol": "EURUSD", "provider": "simulator", "timeframe": "H2"},
    )
    assert r.status_code == 422
    assert "unknown timeframe" in r.json()["error"]["detail"]


async def test_an_unknown_provider_is_refused_by_the_route(signed_in: AsyncClient) -> None:
    r = await signed_in.get(
        "/v1/market/candles", params={"symbol": "EURUSD", "provider": "bloomberg"}
    )
    assert r.status_code == 422


async def test_an_unmapped_symbol_is_a_404_from_the_route(signed_in: AsyncClient) -> None:
    r = await signed_in.get(
        "/v1/market/candles", params={"symbol": "NOSUCH", "provider": "simulator"}
    )
    assert r.status_code == 404


async def test_quality_route_needs_system_settings(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    assert (
        await client.get("/v1/market/quality", params={"symbol": "EURUSD", "provider": "simulator"})
    ).status_code == 403
    admin = await _admin(app, client)
    r = await admin.get("/v1/market/quality", params={"symbol": "EURUSD", "provider": "simulator"})
    assert r.status_code == 200
    assert "quality" in r.json()


# ---------------------------------------------------------------- safety


async def test_nothing_in_marketdata_can_place_an_order() -> None:
    """Market data is an input to a decision, never a decision."""
    import importlib
    import pkgutil

    import app.marketdata as pkg

    forbidden = {"order_send", "place_order", "OrderRequest", "BrokerAdapter", "RiskEngine"}
    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.marketdata."):
        loaded = importlib.import_module(module.name)
        assert not (set(dir(loaded)) & forbidden), f"{module.name} reaches execution"


async def test_no_credential_appears_in_a_market_response(signed_in: AsyncClient) -> None:
    body = (
        await signed_in.get(
            "/v1/market/candles", params={"symbol": "EURUSD", "provider": "simulator", "limit": 3}
        )
    ).text.lower()
    for leak in ("password", "secret", "api_key", "token", "postgresql", "redis://"):
        assert leak not in body


# ------------------------------------------------------- the broker clock


def test_a_server_stamp_is_converted_to_true_utc() -> None:
    """MT5 renders the broker's WALL CLOCK as a UTC epoch. Reading it back as
    UTC puts every timestamp ahead by the broker's offset -- three hours on
    this broker -- and every staleness check with it.

    Caught live at L08: a real quote came back flagged "stamped in the future"
    by exactly 3h00m.
    """
    from app.marketdata.providers.mt5 import MT5MarketData

    adapter = MT5MarketData()
    true_utc = datetime(2026, 9, 3, 4, 18, 22, tzinfo=UTC)
    offset = 3 * 3600
    server_epoch = true_utc.timestamp() + offset  # what the terminal reports
    assert adapter._to_utc(server_epoch, offset) == true_utc


def test_the_offset_is_rounded_to_a_half_hour_so_a_late_tick_is_not_absorbed() -> None:
    """Broker offsets are whole or half hours. Rounding is what separates the
    offset from a stale tick: 3h07m is a 3h offset plus seven minutes of age,
    not a 3h07m offset."""
    from app.marketdata.providers.mt5 import MT5MarketData

    quantum = MT5MarketData.OFFSET_QUANTUM_SECONDS
    for raw, expected in [
        (3 * 3600, 3 * 3600),
        (3 * 3600 + 7 * 60, 3 * 3600),  # a seven-minute-old tick
        (2 * 3600 + 1800, 2 * 3600 + 1800),  # a half-hour broker
        (-5 * 3600 - 60, -5 * 3600),
    ]:
        assert int(round(raw / quantum) * quantum) == expected


def test_a_zero_spread_quote_is_flagged() -> None:
    """Not a free trade -- a book that is not two-sided. Costing from it
    understates the spread to zero, which flatters every result."""
    frozen = Quote(
        symbol="EURUSD",
        provider=Provider.mt5,
        at=T0,
        received_at=T0,
        bid=Decimal("1.15966"),
        ask=Decimal("1.15966"),
    )
    assert any("zero spread" in p for p in check_quote(frozen))
