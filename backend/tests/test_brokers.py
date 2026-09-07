"""Broker adapters: the interface, the fault paths, validation and reconciliation.

This package was 963 lines of execution-critical code with no tests. These are
they. Every case runs against `FakeBroker`, which is the PAPER venue and the
fault injector; none touches a terminal, and none sends a real order.

The three that matter most:

  * `test_an_unknown_send_is_not_a_rejection` -- an IPC timeout after
    `order_send` looks exactly like a rejection from the caller's side, and
    treating it as one is how a retry becomes two positions.
  * `test_a_volume_off_the_step_is_refused_rather_than_rounded` -- rounding up
    silently risks more than was budgeted, which undoes position sizing.
  * `test_reconciliation_reports_and_never_repairs` -- the instinct to close an
    unexpected position or re-send a missing one is wrong in both directions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.auth.models import Role, User
from app.brokers.base import (
    AccountMode,
    BrokerHealth,
    ConnectionState,
    NotConnected,
    OrderRequest,
    OrderStatus,
    SymbolInfo,
)
from app.brokers.fake import FakeBroker
from app.brokers.reconcile import Finding, InternalPosition, reconcile_positions
from app.brokers.registry import BrokerRegistry, UnknownAccount
from app.brokers.validation import OrderRejected, floor_to_step, validate_order
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
T0 = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)

SPEC = SymbolInfo(
    symbol="EURUSD",
    digits=5,
    point=Decimal("0.00001"),
    contract_size=Decimal("100000"),
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("100"),
    volume_step=Decimal("0.01"),
)
# Measured from this broker's terminal: an index is not an FX pair, and
# assuming one spec for both is how an order goes out for the wrong amount.
DE40 = SymbolInfo(
    symbol="DE40",
    digits=2,
    point=Decimal("0.01"),
    contract_size=Decimal("1"),
    tick_size=Decimal("0.01"),
    tick_value=Decimal("0.0116"),
    volume_min=Decimal("0.1"),
    volume_max=Decimal("250"),
    volume_step=Decimal("0.1"),
)


def request(**overrides: object) -> OrderRequest:
    base: dict[str, object] = {
        "symbol": "EURUSD",
        "side": "buy",
        "volume": Decimal("0.01"),
        "intent_id": "intent-1",
    }
    base.update(overrides)
    return OrderRequest(**base)  # type: ignore[arg-type]


@pytest.fixture
async def broker() -> AsyncIterator[FakeBroker]:
    fake = FakeBroker()
    await fake.connect()
    # The simulator refuses to fill at a price it does not have, which is the
    # right behaviour and is asserted below. Seeding one is part of setting up
    # a venue, not a workaround.
    fake.set_quote("EURUSD", "1.10000", "1.10002")
    fake.symbols["EURUSD"] = SPEC
    yield fake
    await fake.disconnect()


async def test_the_simulator_refuses_to_fill_at_a_price_it_does_not_have() -> None:
    """It has no quote for the symbol, so it cannot report a fill. Inventing
    one would be the fabricated-price failure the whole project guards."""
    fake = FakeBroker()
    await fake.connect()
    with pytest.raises(NotConnected, match="no quote"):
        await fake.place_order(request())


# ----------------------------------------------------------- connection


async def test_a_new_adapter_is_disconnected_and_says_so() -> None:
    """`connected` is never the starting assumption."""
    assert FakeBroker().state is ConnectionState.disconnected


async def test_reads_refuse_before_connect() -> None:
    fake = FakeBroker()
    with pytest.raises(NotConnected):
        await fake.get_account()
    with pytest.raises(NotConnected):
        await fake.get_positions()


async def test_connect_reports_the_account_the_venue_describes(broker: FakeBroker) -> None:
    account = await broker.get_account()
    assert broker.state is ConnectionState.connected
    # The simulator is PAPER. It is never described as a real account.
    assert account.mode is not AccountMode.real


async def test_disconnect_returns_to_disconnected(broker: FakeBroker) -> None:
    await broker.disconnect()
    assert broker.state is ConnectionState.disconnected
    with pytest.raises(NotConnected):
        await broker.get_positions()


async def test_a_mid_run_disconnect_stops_reads(broker: FakeBroker) -> None:
    broker.force_disconnect()
    assert broker.state is not ConnectionState.connected
    with pytest.raises(NotConnected):
        await broker.get_account()


# --------------------------------------------------------------- health


async def test_health_never_raises_when_disconnected() -> None:
    reported = await FakeBroker().health()
    assert isinstance(reported, BrokerHealth)
    assert reported.usable is False
    assert reported.state is ConnectionState.disconnected


async def test_health_reports_usable_when_the_account_reads(broker: FakeBroker) -> None:
    reported = await broker.health()
    assert reported.usable is True
    assert reported.state is ConnectionState.connected
    assert reported.latency_ms is not None


async def test_health_is_degraded_when_the_account_cannot_be_read(
    broker: FakeBroker,
) -> None:
    """Connected and unreadable is degraded, not healthy and not down."""

    async def boom():  # noqa: ANN202
        raise RuntimeError("terminal stopped answering")

    broker.get_account = boom  # type: ignore[method-assign]
    reported = await broker.health()
    assert reported.state is ConnectionState.degraded
    assert reported.usable is False
    assert "could not be read" in reported.detail


async def test_health_is_degraded_when_trading_is_not_allowed(
    broker: FakeBroker,
) -> None:
    """An order attempted on an account with trading disabled cannot succeed,
    so reporting it healthy would be a lie the OMS acts on."""
    original = await broker.get_account()

    async def no_trading():  # noqa: ANN202
        from dataclasses import replace

        return replace(original, trade_allowed=False)

    broker.get_account = no_trading  # type: ignore[method-assign]
    reported = await broker.health()
    assert reported.state is ConnectionState.degraded
    assert reported.trade_allowed is False


def test_degraded_and_reconnecting_are_distinct_states() -> None:
    """Collapsing them would let a caller treat a half-working venue as a dead
    one, or the reverse."""
    assert ConnectionState.degraded is not ConnectionState.reconnecting
    assert {"degraded", "reconnecting"} <= {str(s) for s in ConnectionState}


# ------------------------------------------------------------ execution


async def test_a_fill_is_what_the_venue_reported(broker: FakeBroker) -> None:
    result = await broker.place_order(request())
    assert result.status is OrderStatus.accepted
    assert result.fill_price is not None
    # The label travels with the row, so a simulated fill can never be pooled
    # with a broker fill downstream.
    assert result.fill_source == "simulator"


async def test_a_rejection_is_reported_as_a_rejection(broker: FakeBroker) -> None:
    broker.fail_next = "insufficient margin"
    result = await broker.place_order(request())
    assert result.status is OrderStatus.rejected
    assert "insufficient margin" in result.detail
    assert result.fill_price is None


async def test_an_unknown_send_is_not_a_rejection(broker: FakeBroker) -> None:
    """This is the case the whole three-state design exists for.

    An IPC timeout after `order_send` looks exactly like a rejection from the
    caller's side. Retrying a rejection is safe; retrying this would
    double-send. So UNKNOWN is its own outcome and carries no fill.
    """
    broker.unknown_next = True
    result = await broker.place_order(request())
    assert result.status is OrderStatus.unknown
    assert result.is_unknown is True
    assert result.is_accepted is False
    assert result.fill_price is None


async def test_an_unknown_result_never_claims_a_fill(broker: FakeBroker) -> None:
    broker.unknown_next = True
    result = await broker.place_order(request())
    for field in (result.fill_price, result.filled_volume, result.filled_at):
        assert field is None


async def test_a_disconnect_mid_run_is_driven_by_the_injector(broker: FakeBroker) -> None:
    broker.disconnect_after = 1
    await broker.place_order(request())
    assert broker.state is not ConnectionState.connected


async def test_closing_a_position_reduces_what_the_venue_holds(
    broker: FakeBroker,
) -> None:
    opened = await broker.place_order(request())
    assert opened.position_id is not None
    before = await broker.get_positions()
    assert len(before) == 1
    closed = await broker.close_position(opened.position_id)
    assert closed.status is OrderStatus.accepted
    assert await broker.get_positions() == []


async def test_closing_an_unknown_position_is_rejected_not_ignored(
    broker: FakeBroker,
) -> None:
    result = await broker.close_position("no-such-position")
    assert result.status is OrderStatus.rejected


# ------------------------------------------------------------ validation


def test_a_valid_request_passes() -> None:
    assert validate_order(request(), SPEC).ok is True


def test_a_missing_specification_is_itself_a_refusal() -> None:
    """Sending an order for a symbol whose terms we could not read is how an
    order goes out for the wrong amount."""
    report = validate_order(request(), None)
    assert report.ok is False
    assert "no symbol specification" in report.problems[0]


def test_a_volume_off_the_step_is_refused_rather_than_rounded() -> None:
    """Rounding up would risk more than was budgeted, silently undoing
    `lot_for_risk`. The refusal names the nearest valid volume BELOW."""
    report = validate_order(request(volume=Decimal("0.037")), SPEC)
    assert report.ok is False
    assert "not a multiple of the step" in report.problems[0]
    assert "0.03" in report.problems[0]


def test_the_suggested_volume_always_rounds_down() -> None:
    assert floor_to_step(Decimal("0.037"), Decimal("0.01")) == Decimal("0.03")
    assert floor_to_step(Decimal("0.35"), Decimal("0.1")) == Decimal("0.3")


def test_step_checking_uses_decimal_not_float() -> None:
    """A float remainder on 0.1 steps gives 0.09999999999999998 and rejects a
    valid order."""
    assert validate_order(request(symbol="DE40", volume=Decimal("0.3")), DE40).ok is True
    assert validate_order(request(symbol="DE40", volume=Decimal("2.1")), DE40).ok is True


def test_volume_below_the_venue_minimum_is_refused() -> None:
    report = validate_order(request(volume=Decimal("0.001")), SPEC)
    assert report.ok is False
    assert "below the venue minimum" in report.problems[0]


def test_volume_above_the_venue_maximum_is_refused() -> None:
    assert validate_order(request(volume=Decimal("500")), SPEC).ok is False


def test_the_specs_are_per_symbol_not_universal() -> None:
    """DE40 has contract size 1 and minimum volume 0.1 where the FX pairs have
    100,000 and 0.01. Both measured from the terminal."""
    # Legal on DE40, illegal on EURUSD's step.
    assert validate_order(request(symbol="DE40", volume=Decimal("0.1")), DE40).ok is True
    # Legal on EURUSD, below DE40's minimum.
    assert validate_order(request(symbol="DE40", volume=Decimal("0.01")), DE40).ok is False


def test_a_zero_or_negative_volume_is_refused() -> None:
    assert validate_order(request(volume=Decimal("0")), SPEC).ok is False
    assert validate_order(request(volume=Decimal("-1")), SPEC).ok is False


def test_an_unknown_side_is_refused() -> None:
    assert validate_order(request(side="hodl"), SPEC).ok is False


def test_a_bracket_that_does_not_straddle_the_quote_is_refused() -> None:
    """The authoritative check is against the FILL, not the quote -- NZDUSD
    10200315596 filled 279 points away and every exit branch was a loss -- but
    a bracket that fails even against the quote never needed to be sent."""
    bad = request(stop_loss=Decimal("1.20"), take_profit=Decimal("1.30"))
    report = validate_order(bad, SPEC, quote_bid=Decimal("1.10"))
    assert report.ok is False
    assert "does not straddle" in report.problems[0]


def test_a_correct_buy_bracket_passes() -> None:
    good = request(stop_loss=Decimal("1.09"), take_profit=Decimal("1.12"))
    assert validate_order(good, SPEC, quote_bid=Decimal("1.10")).ok is True


def test_a_correct_sell_bracket_passes() -> None:
    good = request(side="sell", stop_loss=Decimal("1.12"), take_profit=Decimal("1.09"))
    assert validate_order(good, SPEC, quote_bid=Decimal("1.10")).ok is True


def test_raise_if_bad_raises_with_every_reason() -> None:
    with pytest.raises(OrderRejected) as exc:
        validate_order(request(side="nope", volume=Decimal("0.037")), SPEC).raise_if_bad()
    assert "side" in str(exc.value)
    assert "step" in str(exc.value)


# -------------------------------------------------------- reconciliation


def _internal(**overrides: object) -> InternalPosition:
    base: dict[str, object] = {
        "position_id": "p1",
        "symbol": "EURUSD",
        "side": "long",
        "volume": Decimal("0.01"),
        "entry_price": Decimal("1.10000"),
    }
    base.update(overrides)
    return InternalPosition(**base)  # type: ignore[arg-type]


async def test_matching_views_reconcile_clean(broker: FakeBroker) -> None:
    opened = await broker.place_order(request())
    held = await broker.get_positions()
    ours = [
        _internal(
            position_id=held[0].position_id,
            volume=held[0].volume,
            entry_price=held[0].entry_price,
            side=held[0].side,
        )
    ]
    report = await broker.reconcile(ours)
    assert opened.status is OrderStatus.accepted
    assert report.clean is True
    assert report.safe_to_trade is True


async def test_a_position_the_platform_does_not_know_about_is_reported(
    broker: FakeBroker,
) -> None:
    await broker.place_order(request())
    report = await broker.reconcile([])
    assert not report.clean
    findings = {m.finding for m in report.mismatches}
    assert Finding.unexpected_at_broker in findings
    # A fact to investigate, not a reason to halt: a rule that stopped the
    # platform on any mismatch would be switched off the first time somebody
    # opened a manual trade.
    assert report.safe_to_trade is True


def test_a_position_missing_at_the_venue_is_reported_and_not_resent() -> None:
    report = reconcile_positions([], [_internal()], now=T0)
    assert [m.finding for m in report.mismatches] == [Finding.missing_at_broker]
    assert "never by re-sending" in report.mismatches[0].detail


def test_a_volume_disagreement_is_reported() -> None:
    from app.brokers.base import BrokerPosition

    theirs = [
        BrokerPosition(
            position_id="p1",
            symbol="EURUSD",
            side="long",
            volume=Decimal("0.02"),
            entry_price=Decimal("1.10000"),
            opened_at=T0,
        )
    ]
    report = reconcile_positions(theirs, [_internal()], now=T0)
    findings = {m.finding for m in report.mismatches}
    assert Finding.volume_mismatch in findings


def test_a_bracket_disagreement_is_reported() -> None:
    from app.brokers.base import BrokerPosition

    theirs = [
        BrokerPosition(
            position_id="p1",
            symbol="EURUSD",
            side="long",
            volume=Decimal("0.01"),
            entry_price=Decimal("1.10000"),
            opened_at=T0,
            stop_loss=Decimal("1.09000"),
        )
    ]
    report = reconcile_positions(theirs, [_internal()], now=T0)
    assert Finding.bracket_mismatch in {m.finding for m in report.mismatches}


def test_a_closed_internal_record_is_agreement_not_a_mismatch() -> None:
    report = reconcile_positions([], [_internal(status="closed")], now=T0)
    assert report.clean is True


def test_an_unresolved_unknown_blocks_trading() -> None:
    """It must be settled against the venue's deal history before another
    order is sent for the same intent -- and never by retrying."""
    report = reconcile_positions([], [], now=T0, unresolved_unknowns=["intent-9"])
    assert report.safe_to_trade is False
    assert report.clean is False
    assert "never settled by retrying" in str(report.as_dict()["note"])


def test_reconciliation_reports_and_never_repairs() -> None:
    """The report is data. Nothing in it is an instruction, and nothing in the
    module writes."""
    import inspect

    import app.brokers.reconcile as module

    source = inspect.getsource(module)
    for verb in ("db.add", "db.commit", "close_position(", "place_order(", "cancel_order("):
        assert verb not in source


async def test_reconcile_is_a_read_and_changes_nothing_at_the_venue(
    broker: FakeBroker,
) -> None:
    await broker.place_order(request())
    before = await broker.get_positions()
    await broker.reconcile([])
    after = await broker.get_positions()
    assert [p.position_id for p in before] == [p.position_id for p in after]


# ------------------------------------------------------------- registry


async def test_the_registry_is_per_account_with_no_default() -> None:
    """A module-level 'the MT5 connection' is what makes two accounts share
    credentials, positions and risk state by accident."""
    registry = BrokerRegistry()
    registry.register("acct-a", FakeBroker())
    registry.register("acct-b", FakeBroker())
    assert registry.get("acct-a") is not registry.get("acct-b")
    with pytest.raises(UnknownAccount):
        registry.get("acct-c")


async def test_the_registry_refuses_a_duplicate_registration() -> None:
    registry = BrokerRegistry()
    registry.register("acct-a", FakeBroker())
    with pytest.raises(ValueError, match="already has an adapter"):
        registry.register("acct-a", FakeBroker())


async def test_the_registry_locks_per_account() -> None:
    registry = BrokerRegistry()
    registry.register("acct-a", FakeBroker())
    registry.register("acct-b", FakeBroker())
    assert registry.lock("acct-a") is not registry.lock("acct-b")


async def test_the_registry_describes_without_a_credential() -> None:
    registry = BrokerRegistry()
    registry.register("acct-a", FakeBroker())
    described = registry.describe()[0]
    assert set(described) == {"account_id", "adapter", "mode", "state", "reconnects"}
    for leak in ("password", "login", "secret", "token"):
        assert leak not in str(described).lower()


async def test_registry_health_covers_every_account() -> None:
    registry = BrokerRegistry()
    connected = FakeBroker()
    await connected.connect()
    registry.register("live-ish", connected)
    registry.register("cold", FakeBroker())
    reported = await registry.health()
    assert reported["live-ish"].usable is True
    assert reported["cold"].usable is False


# ----------------------------------------------------------------- routes


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
    fake = FakeBroker()
    await fake.connect()
    fake.set_quote("EURUSD", "1.10000", "1.10002")
    fake.symbols["EURUSD"] = SPEC
    application.state.brokers.register("acct-1", fake)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def trader(app: FastAPI, client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert user is not None
        user.role = Role.trader.value
        await db.commit()
    return client


@pytest.mark.parametrize(
    "path",
    [
        "/v1/brokers",
        "/v1/brokers/health",
        "/v1/brokers/acct-1/account",
        "/v1/brokers/acct-1/positions",
        "/v1/brokers/acct-1/reconcile",
    ],
)
async def test_broker_routes_refuse_anonymous_access(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


async def test_a_plain_user_cannot_reach_broker_routes(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.get("/v1/brokers")
    assert r.status_code == 403
    assert "manage_brokers" in r.json()["error"]["detail"]


async def test_the_adapter_listing_carries_no_credential(trader: AsyncClient) -> None:
    body = (await trader.get("/v1/brokers")).json()
    assert body["adapters"][0]["account_id"] == "acct-1"
    for leak in ("password", "login", "secret", "token"):
        assert leak not in str(body).lower()


async def test_the_account_route_omits_the_account_number(trader: AsyncClient) -> None:
    body = (await trader.get("/v1/brokers/acct-1/account")).json()
    assert "login" not in body
    assert body["account_mode"] != "real"


async def test_an_unknown_account_is_404_not_another_account(trader: AsyncClient) -> None:
    assert (await trader.get("/v1/brokers/nope/account")).status_code == 404


async def test_the_reconcile_route_says_it_repaired_nothing(trader: AsyncClient) -> None:
    body = (await trader.get("/v1/brokers/acct-1/reconcile")).json()
    assert "not a repair" in body["note"]
    assert body["safe_to_trade"] is True


#: The only writes this surface is allowed to carry. **CHANGED AT L51.**
#:
#: This test asserted `methods <= {GET, HEAD}` for eleven levels, and the
#: module docstring said why: execution must run Risk -> Sizing -> OMS ->
#: BrokerAdapter, three of those did not exist, and "the write has to be added
#: deliberately in the level that also adds the veto in front of it".
#:
#: All four now exist and are verified, and the write that was owed is venue
#: REGISTRATION -- filling the registries so execution has somewhere to go.
#: Before it, the registries were empty by design and no operator action could
#: fill them, so every routed signal stopped at `no_venue`.
#:
#: The list is exact rather than a prefix or a pattern. A new write on this
#: surface has to be added here, by name, which is the same deliberateness the
#: original rule was protecting.
_ALLOWED_BROKER_WRITES = {
    ("/v1/brokers/adapters", "POST"),
    ("/v1/brokers/adapters/{account_id}", "DELETE"),
}


async def test_the_broker_surface_carries_no_write_but_registration(
    app: FastAPI,
) -> None:
    """The surface may register a venue. It may not reach one to trade.

    Registration is a CONTROL-PLANE write: it says where orders may go, and
    sends nothing. `POST /v1/orders` remains the one submission door, and the
    RiskEngine still stands in front of it.
    """
    unexpected: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/v1/brokers"):
            continue
        for method in getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}:
            if (path, method) not in _ALLOWED_BROKER_WRITES:
                unexpected.append(f"{method} {path}")

    assert unexpected == [], (
        "a write appeared on the broker surface that nobody added deliberately: "
        + ", ".join(sorted(unexpected))
    )


async def test_the_broker_surface_cannot_reach_a_venue_to_trade(app: FastAPI) -> None:
    """The property the read-only rule was really protecting, asserted directly.

    "No writes" was a proxy for "this surface cannot send anything to a venue".
    Now that one write exists, the real rule is stated: no route here places,
    closes, cancels or modifies an order.
    """
    import ast
    from pathlib import Path

    TRANSMITS = {"place_order", "close_position", "cancel_order", "modify_order"}
    source = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "brokers.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    reached = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TRANSMITS
    ]
    assert reached == [], f"the broker surface transmits to a venue: {reached}"


async def test_order_submission_is_still_the_only_door_and_no_venue_is_behind_it(
    app: FastAPI, trader: AsyncClient
) -> None:
    """CHANGED AT L19: the door is built. What it still refuses is reaching a
    venue nobody registered, which is the default state of this deployment —
    the broker registry and the order-manager registry are both empty until an
    operator puts an adapter in one."""
    from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER

    headers = {
        CSRF_HEADER: trader.cookies.get(CSRF_COOKIE) or "",
        "Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSDX",
    }
    r = await trader.post(
        "/v1/orders",
        json={
            "account_id": "no-such-account",
            "symbol": "EURUSD",
            "side": "buy",
            "quantity": "0.10",
        },
        headers=headers,
    )
    assert r.status_code == 409
    assert "no order manager is registered" in r.json()["error"]["detail"]
    assert app.state.order_managers.managers == {}


# ------------------------------------------------------------ safety


async def test_the_fake_broker_is_never_described_as_live() -> None:
    fake = FakeBroker()
    assert fake.mode == "paper"
    await fake.connect()
    account = await fake.get_account()
    assert account.mode is not AccountMode.real


async def test_every_fake_fill_is_labelled_a_simulation(broker: FakeBroker) -> None:
    """Downstream code that pools a simulated fill with a broker fill has to
    do so deliberately, because the label travels with the row."""
    for _ in range(3):
        result = await broker.place_order(request())
        if result.is_accepted:
            assert result.fill_source == "simulator"


async def test_the_mt5_adapter_calls_the_demo_fence_on_connect() -> None:
    """`assert_demo` is not reimplemented here; it is called. A code-level
    fence no setting can override."""
    import inspect

    import app.brokers.mt5 as module

    source = inspect.getsource(module)
    assert "assert_demo" in source
    assert "def assert_demo" not in source  # called, never redefined


async def test_no_api_module_reaches_a_broker_write_method() -> None:
    """The API layer may read a venue. It may not send to one."""
    import importlib
    import pkgutil

    import app.api as pkg

    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.api."):
        loaded = importlib.import_module(module.name)
        names = set(dir(loaded))
        assert "place_order" not in names, f"{module.name} imports place_order"
        assert "OrderRequest" not in names, f"{module.name} imports OrderRequest"


def test_live_gates_are_all_still_false() -> None:
    """L10 builds the adapter. It does not open the door."""
    from app.core.settings import LIVE_GATES

    assert not any(LIVE_GATES.values())
    assert LIVE_GATES["live_broker_adapter"] is False
    assert LIVE_GATES["broker_sync_on_connect"] is False


async def test_a_default_settings_object_cannot_execute_live() -> None:
    from app.core.settings import Settings as S

    settings = S(_env_file=None)
    assert settings.trading_mode.value == "paper"
    assert settings.live_trading is False
    assert settings.live_execution_allowed is False
    assert len(settings.live_execution_blockers()) >= 10
