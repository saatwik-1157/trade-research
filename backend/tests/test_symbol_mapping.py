"""L11 verification: precision, status, the trading gate and the admin surface.

`tests/test_symbols.py` already covers three-name resolution, ambiguity and
contract specs, and those 25 cases are unchanged -- the resolution core was
correct and was not touched. This file covers what L11 added: quantity and
price normalization, the symbol status vocabulary, the single pre-trade gate,
and the admin mapping API.

The case that matters most is
`test_a_quantity_off_the_step_is_refused_by_default`. Silently moving a
quantity is how position sizing gets undone, and the whole point of
`lot_for_risk` is that the stop costs a fixed sum.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from app.auth.models import Role, User
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.market import SymbolMapping
from app.symbols import service
from app.symbols.errors import DuplicateMapping, IncompleteContractSpec, UnknownSourceSymbol
from app.symbols.precision import (
    Adjustment,
    PrecisionError,
    floor_to_step,
    is_step_multiple,
    normalize_price,
    normalize_quantity,
    snap_stop_loss,
    snap_take_profit,
    steps_in,
)
from app.symbols.status import NotTradable, SymbolStatus, status_of, validate_for_trading
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}

# Measured from this broker's terminal. Two genuinely different instruments:
# DE40 has contract size 1 and a 0.1 volume step where the FX pairs have
# 100,000 and 0.01. Assuming one spec for both is how an order goes out for
# the wrong amount.
FX_SPEC = {
    "contract_size": Decimal("100000"),
    "tick_size": Decimal("0.00001"),
    "tick_value": Decimal("1"),
    "minimum_volume": Decimal("0.01"),
    "maximum_volume": Decimal("100"),
    "volume_step": Decimal("0.01"),
    "price_precision": 5,
    "volume_precision": 2,
    "spec_source": "mt5_terminal",
}
INDEX_SPEC = {
    "contract_size": Decimal("1"),
    "tick_size": Decimal("0.01"),
    "tick_value": Decimal("0.0116"),
    "minimum_volume": Decimal("0.1"),
    "maximum_volume": Decimal("250"),
    "volume_step": Decimal("0.1"),
    "price_precision": 2,
    "volume_precision": 1,
    "spec_source": "mt5_terminal",
}


# ------------------------------------------------------- 1-3. step maths


def test_steps_in_guards_the_float_floor() -> None:
    """A budget worth exactly five steps arrives from a float ATR as
    4.999999999. A bare floor drops a whole step, so a 5-step order and a
    4-step order send identical volume with nothing to say which was meant."""
    step = Decimal("0.01")
    assert steps_in(Decimal("0.04999999999"), step) == 5
    # Genuinely below five steps: floored, not rounded up.
    assert steps_in(Decimal("0.0490"), step) == 4


def test_is_step_multiple_uses_decimal_not_float() -> None:
    """A float remainder on a 0.1 step gives 0.09999999999999998 and rejects a
    valid order."""
    assert is_step_multiple(Decimal("0.3"), Decimal("0.1")) is True
    assert is_step_multiple(Decimal("2.1"), Decimal("0.1")) is True
    assert is_step_multiple(Decimal("0.037"), Decimal("0.01")) is False


def test_floor_to_step_always_rounds_down() -> None:
    assert floor_to_step(Decimal("0.037"), Decimal("0.01")) == Decimal("0.03")
    assert floor_to_step(Decimal("0.35"), Decimal("0.1")) == Decimal("0.3")


def test_the_broker_layer_uses_this_one_implementation() -> None:
    """Two functions that round a lot size differently is how the same signal
    sends two different volumes depending on which path reached the venue."""
    from app.brokers.validation import floor_to_step as broker_floor

    assert broker_floor is floor_to_step


# ------------------------------------------------- 4-6. quantity rules


def test_a_valid_quantity_passes_unchanged() -> None:
    result = normalize_quantity(
        Decimal("0.05"),
        minimum=FX_SPEC["minimum_volume"],
        maximum=FX_SPEC["maximum_volume"],
        step=FX_SPEC["volume_step"],
    )
    assert result.value == Decimal("0.05")
    assert result.adjusted is False
    assert result.adjustment is Adjustment.none


def test_a_quantity_off_the_step_is_refused_by_default() -> None:
    """Silently moving it changes the risk the caller budgeted."""
    with pytest.raises(PrecisionError) as exc:
        normalize_quantity(
            Decimal("0.037"),
            minimum=FX_SPEC["minimum_volume"],
            maximum=FX_SPEC["maximum_volume"],
            step=FX_SPEC["volume_step"],
        )
    assert "not a multiple" in str(exc.value)
    assert "0.03" in str(exc.value)  # names the nearest valid value BELOW


def test_sizing_may_floor_explicitly_and_is_told_that_it_did() -> None:
    """Position sizing computes a raw figure from a risk budget and needs the
    largest tradable volume at or below it. The adjustment is never invisible."""
    result = normalize_quantity(
        Decimal("0.037"),
        minimum=FX_SPEC["minimum_volume"],
        maximum=FX_SPEC["maximum_volume"],
        step=FX_SPEC["volume_step"],
        allow_floor=True,
    )
    assert result.value == Decimal("0.03")
    assert result.adjustment is Adjustment.floored
    assert result.original == Decimal("0.037")
    assert "floored" in result.detail


def test_a_quantity_below_the_minimum_is_never_raised_to_it() -> None:
    """When the broker's minimum lot exceeds the budget the correct answer is
    'this trade cannot be taken at this size', not 'take a bigger one'."""
    with pytest.raises(PrecisionError) as exc:
        normalize_quantity(
            Decimal("0.005"),
            minimum=FX_SPEC["minimum_volume"],
            maximum=FX_SPEC["maximum_volume"],
            step=FX_SPEC["volume_step"],
        )
    assert "below the venue minimum" in str(exc.value)
    assert "not that it should be taken larger" in str(exc.value)


def test_flooring_below_the_minimum_is_reported_as_a_gap() -> None:
    with pytest.raises(PrecisionError, match="below the venue minimum"):
        normalize_quantity(
            Decimal("0.019"),
            minimum=Decimal("0.02"),
            maximum=FX_SPEC["maximum_volume"],
            step=FX_SPEC["volume_step"],
            allow_floor=True,
        )


def test_a_quantity_above_the_maximum_is_refused() -> None:
    with pytest.raises(PrecisionError, match="above the venue maximum"):
        normalize_quantity(
            Decimal("500"),
            minimum=FX_SPEC["minimum_volume"],
            maximum=FX_SPEC["maximum_volume"],
            step=FX_SPEC["volume_step"],
        )


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_a_non_positive_quantity_is_refused(bad: Decimal) -> None:
    with pytest.raises(PrecisionError, match="must be positive"):
        normalize_quantity(bad, minimum=None, maximum=None, step=None)


def test_the_rules_are_per_symbol_not_universal() -> None:
    """0.01 is a legal FX lot and below DE40's minimum. Blindly using '0.01
    lot' for every instrument is what this prevents."""
    ok = normalize_quantity(
        Decimal("0.01"),
        minimum=FX_SPEC["minimum_volume"],
        maximum=FX_SPEC["maximum_volume"],
        step=FX_SPEC["volume_step"],
    )
    assert ok.value == Decimal("0.01")
    with pytest.raises(PrecisionError):
        normalize_quantity(
            Decimal("0.01"),
            minimum=INDEX_SPEC["minimum_volume"],
            maximum=INDEX_SPEC["maximum_volume"],
            step=INDEX_SPEC["volume_step"],
        )


# -------------------------------------------------- 7-9. price rules


def test_a_price_on_the_tick_grid_passes_unchanged() -> None:
    result = normalize_price(Decimal("1.10500"), tick_size=Decimal("0.00001"))
    assert result.value == Decimal("1.10500")
    assert result.adjusted is False


def test_a_price_off_the_grid_is_snapped_and_says_so() -> None:
    result = normalize_price(Decimal("1.105004"), tick_size=Decimal("0.00001"))
    assert result.value == Decimal("1.10500")
    assert result.adjustment is Adjustment.quantized
    assert result.original == Decimal("1.105004")


def test_normalization_is_deterministic() -> None:
    """Required for backtesting, replay and reproducibility."""
    values = [
        normalize_price(Decimal("1.105004"), tick_size=Decimal("0.00001")).value for _ in range(5)
    ]
    assert len(set(values)) == 1


def test_a_non_decimal_tick_grid_is_handled() -> None:
    """Not every instrument ticks in powers of ten. An index quarter-point grid
    is a real thing and 0.01-rounding would produce prices the venue rejects."""
    result = normalize_price(Decimal("18000.30"), tick_size=Decimal("0.25"))
    assert result.value == Decimal("18000.25")


def test_stop_loss_snapping_tightens_risk_on_both_sides() -> None:
    """A long's stop sits below entry, so rounding UP moves it closer and risks
    less. A short's sits above, so rounding DOWN does the same. Tightening is
    the safe direction to be wrong in."""
    long_stop = snap_stop_loss(Decimal("1.09003"), side="buy", tick_size=Decimal("0.0001"))
    assert long_stop.value == Decimal("1.0901")  # up, closer to entry

    short_stop = snap_stop_loss(Decimal("1.11007"), side="sell", tick_size=Decimal("0.0001"))
    assert short_stop.value == Decimal("1.1100")  # down, closer to entry


def test_take_profit_snapping_never_flatters_a_result() -> None:
    """A target snapped closer would book a win the instrument did not reach."""
    long_tp = snap_take_profit(Decimal("1.12001"), side="buy", tick_size=Decimal("0.0001"))
    assert long_tp.value == Decimal("1.1201")  # further away

    short_tp = snap_take_profit(Decimal("1.08009"), side="sell", tick_size=Decimal("0.0001"))
    assert short_tp.value == Decimal("1.0800")  # further away


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1.1")])
def test_a_non_positive_price_is_refused(bad: Decimal) -> None:
    with pytest.raises(PrecisionError, match="must be positive"):
        normalize_price(bad, tick_size=Decimal("0.00001"))


def test_an_unparseable_value_is_refused_not_defaulted() -> None:
    with pytest.raises(PrecisionError, match="not a number"):
        normalize_price("about a dollar", tick_size=Decimal("0.00001"))


# ---------------------------------------------- database-backed fixtures


@pytest.fixture
async def engine() -> AsyncIterator[object]:
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncIterator[object]:  # noqa: ANN001
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await service.upsert_symbol(
            session, "EURUSD", "fx", base_currency="EUR", quote_currency="USD", digits=5
        )
        await service.upsert_mapping(session, "EURUSD", "tradingview", "OANDA:EURUSD")
        await service.upsert_mapping(session, "EURUSD", "mt5", "EURUSD.r", **FX_SPEC)
        # Mapped but never synced: the not_tradable case.
        await service.upsert_symbol(session, "DE40", "index", quote_currency="EUR", digits=1)
        await service.upsert_mapping(session, "DE40", "mt5", "GER40.cash")
        await session.commit()
        yield session


# ------------------------------------------- 10-14. status vocabulary


async def test_a_complete_symbol_is_active(db) -> None:  # noqa: ANN001
    report = await status_of(db, "EURUSD", "mt5")
    assert report.status is SymbolStatus.active
    assert report.tradable is True
    assert report.broker_symbol == "EURUSD.r".upper()
    assert report.spec_complete is True


async def test_a_symbol_without_a_synced_spec_is_not_tradable(db) -> None:  # noqa: ANN001
    """Resolvable is not the same as usable. Sizing from a missing tick value
    is a real order for the wrong amount."""
    report = await status_of(db, "DE40", "mt5")
    assert report.status is SymbolStatus.not_tradable
    assert report.tradable is False
    assert "tick_value" in report.missing_spec_fields


async def test_an_unknown_symbol_is_not_found_and_never_active(db) -> None:  # noqa: ANN001
    report = await status_of(db, "NOSUCH", "mt5")
    assert report.status is SymbolStatus.not_found
    assert report.tradable is False


async def test_a_symbol_with_no_mapping_at_this_provider_is_a_mapping_error(
    db,  # noqa: ANN001
) -> None:
    report = await status_of(db, "EURUSD", "ccxt")
    assert report.status is SymbolStatus.mapping_error
    assert report.tradable is False


async def test_an_inactive_mapping_reports_inactive(db) -> None:  # noqa: ANN001
    mapping = await service.mapping_for(db, "EURUSD", "mt5")
    mapping.is_active = False
    await db.flush()
    report = await status_of(db, "EURUSD", "mt5")
    assert report.status is SymbolStatus.inactive
    assert report.tradable is False


def test_only_active_is_tradable() -> None:
    """UNKNOWN is never read as ACTIVE."""
    for status in SymbolStatus:
        assert status.tradable is (status is SymbolStatus.active)
    assert SymbolStatus.unknown.tradable is False


async def test_status_never_raises(db) -> None:  # noqa: ANN001
    """A UI listing instruments gets a row for each one, including broken ones."""
    for code in ("EURUSD", "DE40", "NOSUCH", "", "  "):
        report = await status_of(db, code, "mt5")
        assert isinstance(report.status, SymbolStatus)


# ------------------------------------------- 15-17. the trading gate


async def test_the_gate_returns_the_spec_for_a_usable_symbol(db) -> None:  # noqa: ANN001
    """Every caller that needs the gate also needs the numbers behind it, and a
    second lookup would be a second chance to disagree."""
    spec = await validate_for_trading(db, "EURUSD", "mt5")
    assert spec.broker_symbol == "EURUSD.R"
    assert spec.volume_step == Decimal("0.01")


async def test_the_gate_refuses_a_symbol_without_a_spec(db) -> None:  # noqa: ANN001
    with pytest.raises(NotTradable) as exc:
        await validate_for_trading(db, "DE40", "mt5")
    assert "not_tradable" in str(exc.value)


async def test_the_gate_refuses_an_unknown_symbol(db) -> None:  # noqa: ANN001
    with pytest.raises(NotTradable):
        await validate_for_trading(db, "NOSUCH", "mt5")


async def test_the_gate_refuses_an_inactive_mapping(db) -> None:  # noqa: ANN001
    mapping = await service.mapping_for(db, "EURUSD", "mt5")
    mapping.is_active = False
    await db.flush()
    with pytest.raises(NotTradable):
        await validate_for_trading(db, "EURUSD", "mt5")


async def test_passing_the_gate_is_not_authorization() -> None:
    """It says the symbol is usable, not that a trade may happen. Nothing in
    the symbol layer reaches an order path."""
    import importlib
    import pkgutil

    import app.symbols as pkg

    forbidden = {"place_order", "OrderRequest", "BrokerAdapter", "RiskEngine", "order_send"}
    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.symbols."):
        loaded = importlib.import_module(module.name)
        assert not (set(dir(loaded)) & forbidden), f"{module.name} reaches execution"


# -------------------------------- 18-21. resolution, unchanged core


async def test_tradingview_to_internal_resolution(db) -> None:  # noqa: ANN001
    symbol = await service.resolve_source(db, "tradingview", "OANDA:EURUSD")
    assert symbol.code == "EURUSD"


async def test_internal_to_mt5_resolution_is_not_the_input_string(db) -> None:  # noqa: ANN001
    """'EURUSD' internally is 'EURUSD.R' at this broker. Never assume they
    match."""
    assert await service.broker_symbol_for(db, "EURUSD") == "EURUSD.R"


async def test_an_unknown_provider_symbol_is_refused_not_guessed(db) -> None:  # noqa: ANN001
    with pytest.raises(UnknownSourceSymbol):
        await service.resolve_source(db, "tradingview", "OANDA:NOTMAPPED")


async def test_no_suffix_is_ever_added_or_removed_to_find_a_broker_symbol(
    db,  # noqa: ANN001
) -> None:
    """EURUSD, EURUSDm, EURUSD.a and EURUSD.pro are four different symbols.
    The broker's is a table lookup, never a transformation of the input."""
    with pytest.raises(UnknownSourceSymbol):
        await service.resolve_source(db, "mt5", "EURUSD")  # the bare form is not mapped
    assert await service.broker_symbol_for(db, "EURUSD") == "EURUSD.R"


async def test_remapping_a_provider_symbol_to_another_instrument_is_a_conflict(
    db,  # noqa: ANN001
) -> None:
    await service.upsert_symbol(db, "GBPUSD", "fx")
    with pytest.raises(DuplicateMapping):
        await service.upsert_mapping(db, "GBPUSD", "tradingview", "OANDA:EURUSD")


async def test_an_incomplete_spec_names_what_is_missing(db) -> None:  # noqa: ANN001
    with pytest.raises(IncompleteContractSpec) as exc:
        await service.contract_spec(db, "DE40", "mt5")
    assert "tick_value" in str(exc.value)


# ------------------------------------------------------ admin surface


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=eng)
    async with application.state.session_factory() as session:
        await service.upsert_symbol(session, "EURUSD", "fx", digits=5)
        await service.upsert_mapping(session, "EURUSD", "tradingview", "OANDA:EURUSD")
        await service.upsert_mapping(session, "EURUSD", "mt5", "EURUSD.r", **FX_SPEC)
        await session.commit()
    yield application
    await eng.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _as(app: FastAPI, client: AsyncClient, role: Role) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    async with app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == ALICE["email"]))
        assert user is not None
        user.role = role.value
        await session.commit()
    return client


def _csrf(client: AsyncClient) -> dict[str, str]:
    from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER

    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def test_mapping_changes_need_an_admin(app: FastAPI, client: AsyncClient) -> None:
    """A mapping edit decides which instrument an order reaches."""
    body = {"provider": "mt5", "provider_symbol": "EURUSD.pro"}
    assert (await client.put("/v1/admin/symbols/EURUSD/mappings", json=body)).status_code == 401

    trader = await _as(app, client, Role.trader)
    r = await trader.put("/v1/admin/symbols/EURUSD/mappings", json=body, headers=_csrf(trader))
    assert r.status_code == 403
    assert "manage_system_settings" in r.json()["error"]["detail"]


async def test_an_admin_can_update_a_mapping_and_it_is_audited(
    app: FastAPI, client: AsyncClient
) -> None:
    from app.models.ops import AuditLog

    admin = await _as(app, client, Role.admin)
    r = await admin.put(
        "/v1/admin/symbols/EURUSD/mappings",
        json={"provider": "mt5", "provider_symbol": "EURUSD.pro"},
        headers=_csrf(admin),
    )
    assert r.status_code == 200
    assert r.json()["provider_symbol"] == "EURUSD.PRO"
    async with app.state.session_factory() as session:
        actions = [a.action for a in (await session.scalars(select(AuditLog))).all()]
    # "Who changed EURUSD to point at GBPUSD" has to be answerable.
    assert "symbol_mapping_upserted" in actions


async def test_repointing_a_provider_symbol_is_a_conflict_over_http(
    app: FastAPI, client: AsyncClient
) -> None:
    admin = await _as(app, client, Role.admin)
    await admin.post(
        "/v1/admin/symbols",
        json={"code": "GBPUSD", "asset_class": "fx"},
        headers=_csrf(admin),
    )
    r = await admin.put(
        "/v1/admin/symbols/GBPUSD/mappings",
        json={"provider": "tradingview", "provider_symbol": "OANDA:EURUSD"},
        headers=_csrf(admin),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_mapping"


async def test_an_unknown_provider_is_refused_by_the_route(
    app: FastAPI, client: AsyncClient
) -> None:
    admin = await _as(app, client, Role.admin)
    r = await admin.put(
        "/v1/admin/symbols/EURUSD/mappings",
        json={"provider": "bloomberg", "provider_symbol": "EURUSD"},
        headers=_csrf(admin),
    )
    assert r.status_code == 422


async def test_a_mapping_is_disabled_never_deleted(app: FastAPI, client: AsyncClient) -> None:
    """A mapping used to place an order explains why that order went where it
    did; removing it would make a historical trade unexplainable."""
    admin = await _as(app, client, Role.admin)
    r = await admin.patch(
        "/v1/admin/symbols/EURUSD/mappings/mt5/active",
        json={"is_active": False},
        headers=_csrf(admin),
    )
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    # Still on the table, and now refused by every trading path.
    async with app.state.session_factory() as session:
        row = await session.scalar(
            select(SymbolMapping).where(SymbolMapping.provider_symbol == "EURUSD.R")
        )
        assert row is not None
        status = await status_of(session, "EURUSD", "mt5")
    assert status.status is SymbolStatus.inactive


async def test_there_is_no_delete_route_for_a_mapping(app: FastAPI) -> None:
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/v1/admin/symbols"):
            assert "DELETE" not in (getattr(route, "methods", set()) or set()), path


async def test_the_status_route_is_readable_by_any_signed_in_user(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.get("/v1/admin/symbols/EURUSD/status")
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert r.json()["tradable"] is True


async def test_the_status_route_reports_a_broken_symbol_without_raising(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.get("/v1/admin/symbols/NOSUCH/status")
    assert r.status_code == 200
    assert r.json()["status"] == "not_found"
    assert r.json()["tradable"] is False


async def test_no_mapping_response_carries_a_credential(app: FastAPI, client: AsyncClient) -> None:
    admin = await _as(app, client, Role.admin)
    body = (await admin.get("/v1/symbols/EURUSD/mappings")).text.lower()
    for leak in ("password", "secret", "token", "api_key", "login"):
        assert leak not in body
