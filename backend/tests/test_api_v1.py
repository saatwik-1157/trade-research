"""The v1 API surface: versioning, validation, authorization, pagination, errors.

Runs against the real routers on an in-memory SQLite database seeded with a
small execution record, so every assertion here is about behaviour the
application actually has rather than about a mock.

The cases follow L06's own list: a valid request, an invalid one, no
authentication, insufficient permission, an invalid resource id, a duplicate
submission, error handling, pre-v1 compatibility, and the health endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import Role, User
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.execution import Execution, Order, OrderEvent, Trade
from app.models.market import Symbol, SymbolMapping
from app.models.ops import AuditLog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from tests.routes import api_routes

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}

BASE = datetime(2026, 8, 1, 12, 0, 0)
ORDER_COUNT = 7
TRADE_COUNT = 5


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    import app.auth.models  # noqa: F401 - register the auth tables
    import app.models  # noqa: F401 - register every platform table

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    async with application.state.session_factory() as db:
        await _seed(db)
    yield application
    await engine.dispose()


async def _seed(db) -> None:  # noqa: ANN001 - AsyncSession, kept loose for brevity
    eur = Symbol(
        id="sym-eur",
        code="EURUSD",
        asset_class="fx",
        base_currency="EUR",
        quote_currency="USD",
        digits=5,
        point_size=Decimal("0.00001"),
        unit_class="points",
    )
    de40 = Symbol(id="sym-de40", code="DE40", asset_class="index", unit_class="points")
    db.add_all([eur, de40])
    # Everything below references these symbols. SQLAlchemy sorts unrelated
    # mappers by its own key, not by the foreign key, so `orders` and the
    # mappings would otherwise be written before `symbols` exists.
    await db.flush()
    # A complete spec on EURUSD, a deliberately incomplete one on DE40: the
    # refusal path needs a row that exists and is missing fields, which is
    # exactly what an unsynced mapping looks like.
    db.add(
        SymbolMapping(
            id="map-eur-mt5",
            symbol_id="sym-eur",
            provider="mt5",
            provider_symbol="EURUSD.R",
            contract_size=Decimal("100000"),
            tick_size=Decimal("0.00001"),
            tick_value=Decimal("1"),
            minimum_volume=Decimal("0.01"),
            maximum_volume=Decimal("100"),
            volume_step=Decimal("0.01"),
            price_precision=5,
            volume_precision=2,
            spec_source="mt5_terminal",
        )
    )
    db.add(
        SymbolMapping(
            id="map-eur-tv",
            symbol_id="sym-eur",
            provider="tradingview",
            provider_symbol="OANDA:EURUSD",
        )
    )
    db.add(
        SymbolMapping(
            id="map-de40-mt5",
            symbol_id="sym-de40",
            provider="mt5",
            provider_symbol="GER40.CASH",
        )
    )
    for i in range(ORDER_COUNT):
        db.add(
            Order(
                id=f"ord-{i}",
                intent_id=f"jsonl:order:{1000 + i}",
                mode="demo",
                symbol_id="sym-eur" if i % 2 == 0 else "sym-de40",
                side="buy" if i % 2 == 0 else "sell",
                order_type="market",
                quantity=Decimal("0.01"),
                status="filled" if i < 5 else "rejected",
                source="jsonl_import",
                created_at=BASE + timedelta(minutes=i),
                updated_at=BASE + timedelta(minutes=i),
            )
        )
    # `executions` and `order_events` both sort ahead of `orders` for
    # SQLAlchemy, which has no relationship() telling it they depend on the
    # order -- so without this the children are written first. Invisible while
    # SQLite ignored foreign keys; 169 errors once it stopped.
    await db.flush()
    db.add(
        OrderEvent(
            id="oev-0", order_id="ord-0", event_type="filled", occurred_at=BASE, retcode=10009
        )
    )
    db.add(
        Execution(
            id="exe-0",
            order_id="ord-0",
            executed_at=BASE,
            price=Decimal("1.10000"),
            quantity=Decimal("0.01"),
            fill_source="broker",
        )
    )
    for i in range(TRADE_COUNT):
        db.add(
            Trade(
                id=f"trd-{i}",
                mode="demo",
                symbol_id="sym-eur",
                side="long" if i % 2 == 0 else "short",
                volume=Decimal("0.01"),
                entry_price=Decimal("1.10000"),
                exit_price=Decimal("1.10050"),
                opened_at=BASE + timedelta(hours=i),
                closed_at=BASE + timedelta(hours=i, minutes=30),
                gross_profit=Decimal("1.0") if i % 2 == 0 else Decimal("-1.0"),
                commission=Decimal("0"),
                swap=Decimal("0"),
                net_profit=Decimal("1.0") if i % 2 == 0 else Decimal("-1.0"),
                r_multiple=Decimal("0.5") if i % 2 == 0 else Decimal("-1.0"),
                source="jsonl_import",
            )
        )
    db.add(
        AuditLog(
            id="aud-0",
            action="login_succeeded",
            resource_type="user",
            occurred_at=BASE,
            details={"note": "seeded"},
        )
    )
    await db.commit()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    """The double-submit header L04 requires on any state-changing request.

    Sent explicitly rather than disabled: a test that turned CSRF off would
    stop covering the interaction between the gate and the middleware, and the
    ordering of those two is the thing most likely to break.
    """
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


@pytest.fixture
async def signed_in(client: AsyncClient) -> AsyncClient:
    """A plain USER: can read markets, portfolio, analytics and the journal."""
    await client.post("/auth/register", json=ALICE)
    return client


@pytest.fixture
async def admin(app: FastAPI, client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    return client


# ------------------------------------------------------- 1. valid requests


async def test_order_record_is_readable(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/orders")
    assert r.status_code == 200
    body = r.json()
    assert body["page"]["total"] == ORDER_COUNT
    assert body["page"]["returned"] == ORDER_COUNT
    assert body["page"]["has_more"] is False
    # The symbol is the internal code, resolved from symbol_id, not the id.
    assert {row["symbol"] for row in body["items"]} == {"EURUSD", "DE40"}
    assert all(row["mode"] == "demo" for row in body["items"])


async def test_trades_carry_the_poolable_figure(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/trades")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == TRADE_COUNT
    # r_multiple is the field that survives pooling across stop distances.
    assert all(row["r_multiple"] is not None for row in items)
    assert all(row["mode"] == "demo" for row in items)


async def test_nested_order_reads(signed_in: AsyncClient) -> None:
    events = await signed_in.get("/v1/orders/ord-0/events")
    assert events.status_code == 200
    assert [e["event_type"] for e in events.json()] == ["filled"]
    fills = await signed_in.get("/v1/orders/ord-0/executions")
    assert fills.status_code == 200
    # The fill states where it came from; a simulator fill is labelled as one.
    assert fills.json()[0]["fill_source"] == "broker"


async def test_symbol_mappings_are_listed_with_their_spec_completeness(
    signed_in: AsyncClient,
) -> None:
    """This route raised a 500 from L06 until L11 because `has_complete_spec`
    is filled in after validation and had no default. No test covered it."""
    r = await signed_in.get("/v1/symbols/EURUSD/mappings")
    assert r.status_code == 200
    by_provider = {m["provider"]: m for m in r.json()}
    assert by_provider["mt5"]["has_complete_spec"] is True
    # Mapped at TradingView, never synced: false, not absent, and not an error.
    assert by_provider["tradingview"]["has_complete_spec"] is False


async def test_symbol_spec_is_served_when_complete(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/symbols/EURUSD/spec")
    assert r.status_code == 200
    assert r.json()["broker_symbol"] == "EURUSD.R"
    assert Decimal(r.json()["contract_size"]) == Decimal("100000")


async def test_symbol_resolution_uses_the_mapping_table(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/symbols/resolve/tradingview/OANDA:EURUSD")
    assert r.status_code == 200
    assert r.json()["code"] == "EURUSD"


# ------------------------------------------- 2. invalid requests are refused


async def test_unknown_sort_field_is_refused_and_says_what_is_sortable(
    signed_in: AsyncClient,
) -> None:
    r = await signed_in.get("/v1/orders", params={"sort": "net_profit); drop table orders--"})
    assert r.status_code == 422
    detail = r.json()["error"]["detail"]
    assert "cannot sort by" in detail
    assert "created_at" in detail  # names the allowed fields


async def test_unknown_filter_value_is_refused(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/orders", params={"mode": "real"})
    assert r.status_code == 422
    assert "mode must be one of" in r.json()["error"]["detail"]


async def test_limit_above_the_ceiling_is_refused_rather_than_truncated(
    signed_in: AsyncClient,
) -> None:
    """A silently truncated page reads as 'that is all there is', which for a
    trade history is a false statement about the record."""
    r = await signed_in.get("/v1/orders", params={"limit": 10_000})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


async def test_reversed_time_window_is_refused(signed_in: AsyncClient) -> None:
    r = await signed_in.get(
        "/v1/trades", params={"from_time": "2026-09-01T00:00:00", "to_time": "2026-08-01T00:00:00"}
    )
    assert r.status_code == 422


async def test_incomplete_spec_is_refused_naming_the_missing_fields(
    signed_in: AsyncClient,
) -> None:
    r = await signed_in.get("/v1/symbols/DE40/spec")
    assert r.status_code == 409  # the status app.symbols.errors already chose
    assert r.json()["error"]["code"] == "incomplete_contract_spec"
    detail = r.json()["error"]["detail"]
    assert "missing" in detail
    assert "tick_value" in detail


async def test_unmapped_provider_symbol_is_not_guessed(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/symbols/resolve/mt5/NOTASYMBOL")
    assert r.status_code == 404


# ------------------------------------------------------ 3. no authentication


@pytest.mark.parametrize(
    "path",
    [
        "/v1/orders",
        "/v1/trades",
        "/v1/positions",
        "/v1/executions",
        "/v1/symbols",
        "/v1/accounts/broker",
        "/v1/accounts/paper",
        "/v1/admin/audit-logs",
        "/v1/system/safety",
        "/v1/webhooks/events",
    ],
)
async def test_every_data_route_refuses_anonymous_access(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


async def test_order_submission_refuses_anonymous_access(client: AsyncClient) -> None:
    # No session, so no CSRF cookie exists; the session-exempt path check runs
    # first and the request reaches the auth gate, which refuses it.
    assert (await client.post("/v1/orders")).status_code == 401


# --------------------------------------------------- 4. insufficient rights


async def test_audit_log_needs_administration(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/admin/audit-logs")
    assert r.status_code == 403
    assert "access_administration" in r.json()["error"]["detail"]


async def test_a_plain_user_cannot_reach_order_submission(signed_in: AsyncClient) -> None:
    r = await signed_in.post("/v1/orders", headers=_csrf(signed_in))
    assert r.status_code == 403
    assert "submit_orders" in r.json()["error"]["detail"]


async def test_admin_can_read_the_audit_trail(admin: AsyncClient) -> None:
    r = await admin.get("/v1/admin/audit-logs")
    assert r.status_code == 200
    assert r.json()["page"]["total"] >= 1


# -------------------------------------------------- 5. invalid resource ids


async def test_unknown_order_id_is_404(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/orders/not-an-order")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_unknown_trade_id_is_404(signed_in: AsyncClient) -> None:
    assert (await signed_in.get("/v1/trades/nope")).status_code == 404


async def test_filtering_on_an_unknown_symbol_is_404_not_an_empty_page(
    signed_in: AsyncClient,
) -> None:
    """'No trades on XYZUSD' and 'there is no such symbol' are different
    statements and the caller is told which one they hit."""
    r = await signed_in.get("/v1/trades", params={"symbol": "XYZUSD"})
    assert r.status_code == 404


# ------------------------------------------------- 6. idempotency contract


async def test_malformed_idempotency_key_is_refused(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/orders", headers={"Idempotency-Key": "short", **_csrf(client)})
    assert r.status_code == 422
    assert "Idempotency-Key" in r.json()["error"]["detail"]


def _submission(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "account_id": "acct-a",
        "symbol": "EURUSD",
        "side": "buy",
        "sizing_mode": "fixed_quantity",
        "quantity": "0.10",
        "entry_price": "1.10000",
        "stop_loss": "1.09500",
    }
    body.update(overrides)
    return body


async def _register_venue(app: FastAPI, account_id: str = "acct-a"):  # noqa: ANN202
    """Point the platform at a simulated venue, the way an operator would.

    Nothing in the application does this at startup, which is why the route
    refuses by default. A test that wants a venue asks for one explicitly.
    """
    from app.brokers.base import SymbolInfo
    from app.brokers.fake import FakeBroker

    venue = FakeBroker(mode="paper")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.symbols["EURUSD"] = SymbolInfo(
        symbol="EURUSD",
        digits=5,
        point=Decimal("0.00001"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
    )
    app.state.order_managers.register(account_id, venue, mode="paper", broker="fake")
    return venue


async def test_a_submission_refuses_when_no_venue_is_registered(
    app: FastAPI, client: AsyncClient
) -> None:
    """CHANGED AT L19: the route is built, and what it refuses is reaching a
    venue that nobody registered. That is the default state of a deployment
    which has not been pointed at one, and it is a conflict naming the reason
    rather than a 501 naming a level."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/orders",
        json=_submission(),
        headers={"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD0", **_csrf(client)},
    )
    assert r.status_code == 409
    assert "no order manager is registered" in r.json()["error"]["detail"]


async def test_no_order_was_created_by_a_refused_attempt(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await client.post(
        "/v1/orders",
        json=_submission(),
        headers={"Idempotency-Key": "abcdefgh12345678", **_csrf(client)},
    )
    async with app.state.session_factory() as db:
        assert len((await db.scalars(select(Order))).all()) == ORDER_COUNT


async def test_a_submission_runs_risk_and_sizing_and_reaches_the_venue(
    app: FastAPI, client: AsyncClient
) -> None:
    """The whole chain, through the same objects a bot signal uses."""
    await _register_venue(app)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/orders",
        json=_submission(),
        headers={"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD1", **_csrf(client)},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "filled"
    assert body["client_order_id"] == "01JB2K7Q9WZ8YV5N3M4XR6TSD1"
    assert body["broker_order_id"]
    assert body["filled_quantity"] == "0.10"
    assert Decimal(body["remaining_quantity"]) == 0
    # Traceable to the decision that authorised it and the sizing that
    # produced the number (brief 37).
    assert body["risk_decision_id"]
    assert body["sizing"]["final_quantity"] == "0.10"
    assert body["risk_decision"]["outcome"] == "APPROVED"


async def test_a_replayed_submission_creates_no_second_order(
    app: FastAPI, client: AsyncClient
) -> None:
    """The contract this route has advertised since L06."""
    await _register_venue(app)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    headers = {"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD2", **_csrf(client)}
    first = await client.post("/v1/orders", json=_submission(), headers=headers)
    assert first.status_code == 201
    second = await client.post("/v1/orders", json=_submission(), headers=headers)
    assert second.status_code == 409
    # The entrance guard fires first and is the stricter of the two: a
    # resolved intent may not be re-sent at all, which is a sharper statement
    # than "an order already exists".
    detail = second.json()["error"]["detail"]
    assert "a second order for one signal" in detail


async def test_a_replayed_submission_is_refused_after_a_restart(
    app: FastAPI, client: AsyncClient
) -> None:
    """The same contract, across a process restart. **L45 C-1 on this route.**

    `guard_resend` reads `OrderManager.by_intent`, which a new process starts
    empty, so on a fresh process it passes for an intent that already has an
    order. `orders.intent_id` is UNIQUE, so the write would then fail — but
    between `create` and `submit`, which is the least recoverable moment there
    is, and after the decision to send had already been taken.

    Clearing the manager's in-memory maps is exactly what a restart does to
    them: the durable row is all that is left, and it has to be enough.
    """
    await _register_venue(app)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    headers = {"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD3", **_csrf(client)}

    first = await client.post("/v1/orders", json=_submission(), headers=headers)
    assert first.status_code == 201
    order_id = first.json()["order_id"]

    # --- the restart ---
    manager = app.state.order_managers.get("acct-a")
    manager.by_intent.clear()
    manager.orders.clear()
    manager.by_broker_id.clear()

    second = await client.post("/v1/orders", json=_submission(), headers=headers)
    assert second.status_code == 409, (
        "a replayed submission was accepted after a restart emptied the in-memory guard"
    )
    detail = second.json()["error"]["detail"]
    assert order_id in detail, "the refusal does not name the order that already exists"
    assert "a second order for one signal" in detail

    # And no second row was written for this intent. Scoped to the intent
    # deliberately: this fixture's database carries rows from other tests, and
    # a total count would be measuring them.
    from app.models.execution import Order
    from sqlalchemy import func, select

    async with app.state.session_factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.intent_id == "01JB2K7Q9WZ8YV5N3M4XR6TSD3")
        )
    assert count == 1

    async with app.state.session_factory() as db:
        rows = (await db.scalars(select(Order).where(Order.source == "pipeline"))).all()
        assert len(rows) == 1


async def test_a_client_cannot_bypass_the_venue_volume_rules(
    app: FastAPI, client: AsyncClient
) -> None:
    """The quantity a client sends is an INPUT to sizing, not the quantity
    traded. Below the venue minimum it is refused, never raised to it."""
    await _register_venue(app)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/orders",
        json=_submission(quantity="0.001"),
        headers={"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD3", **_csrf(client)},
    )
    assert r.status_code == 422
    assert "below the venue minimum" in r.json()["error"]["detail"]


async def test_a_submission_without_an_idempotency_key_is_refused(
    app: FastAPI, client: AsyncClient
) -> None:
    await _register_venue(app)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/orders", json=_submission(), headers=_csrf(client))
    assert r.status_code == 422
    assert "Idempotency-Key" in r.json()["error"]["detail"]


async def test_the_order_and_its_audit_trail_are_on_disk(app: FastAPI, client: AsyncClient) -> None:
    """Never "submit, save later"."""
    from app.models.execution import OrderEvent

    await _register_venue(app)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/orders",
        json=_submission(),
        headers={"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD4", **_csrf(client)},
    )
    order_id = r.json()["order_id"]

    async with app.state.session_factory() as db:
        row = await db.get(Order, order_id)
        assert row is not None
        assert row.status == "filled"
        assert row.filled_quantity == Decimal("0.10")
        assert row.risk_decision_id
        events = (
            await db.scalars(
                select(OrderEvent)
                .where(OrderEvent.order_id == order_id)
                .order_by(OrderEvent.sequence)
            )
        ).all()
        assert [e.new_status for e in events] == [
            "submitting",
            "submitted",
            "accepted",
            "filled",
        ]


async def test_submission_still_needs_the_permission(app: FastAPI, client: AsyncClient) -> None:
    """A plain USER cannot submit, venue registered or not."""
    await _register_venue(app)
    await client.post("/auth/register", json=ALICE)
    r = await client.post(
        "/v1/orders",
        json=_submission(),
        headers={"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD5", **_csrf(client)},
    )
    assert r.status_code == 403


async def test_the_oms_status_route_reports_the_retry_policy(
    app: FastAPI, signed_in: AsyncClient
) -> None:
    body = (await signed_in.get("/v1/orders/oms/status")).json()
    assert body["accounts"] == []
    assert "not a distributed lock" in body["concurrency"]


# -------------------------------------------------------- 7. error handling


async def test_every_failure_carries_the_same_envelope_and_a_request_id(
    client: AsyncClient,
) -> None:
    r = await client.get("/v1/orders")
    assert r.status_code == 401
    error = r.json()["error"]
    assert set(error) >= {"code", "detail", "request_id"}
    assert error["request_id"] == r.headers["X-Request-ID"]


async def test_errors_never_carry_a_stack_trace_or_a_database_url(
    signed_in: AsyncClient,
) -> None:
    r = await signed_in.get("/v1/orders", params={"sort": "nope"})
    body = r.text.lower()
    for leak in ("traceback", "postgresql+asyncpg", "sqlite+aiosqlite", "password"):
        assert leak not in body


# ---------------------------------------------------------- 8. pagination


async def test_pages_do_not_overlap_and_cover_the_set(signed_in: AsyncClient) -> None:
    first = (await signed_in.get("/v1/orders", params={"limit": 3, "offset": 0})).json()
    second = (await signed_in.get("/v1/orders", params={"limit": 3, "offset": 3})).json()
    assert first["page"]["has_more"] is True
    ids = [row["id"] for row in first["items"]] + [row["id"] for row in second["items"]]
    assert len(ids) == len(set(ids)) == 6


async def test_total_describes_the_filtered_set_not_the_table(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/orders", params={"status": "rejected"})
    body = r.json()
    assert body["page"]["total"] == ORDER_COUNT - 5
    assert all(row["status"] == "rejected" for row in body["items"])


async def test_sorting_is_applied(signed_in: AsyncClient) -> None:
    asc = (await signed_in.get("/v1/orders", params={"sort": "created_at", "order": "asc"})).json()
    desc = (
        await signed_in.get("/v1/orders", params={"sort": "created_at", "order": "desc"})
    ).json()
    assert [r["id"] for r in asc["items"]] == list(reversed([r["id"] for r in desc["items"]]))


# ------------------------------------------- 9. pre-v1 compatibility, health


async def test_health_endpoints_stay_at_the_root(client: AsyncClient) -> None:
    """The Compose healthcheck and nginx probe these paths; moving them under
    a version prefix would break the infrastructure contract."""
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/health/live")).json() == {"status": "alive"}
    assert (await client.get("/health/ready")).status_code == 200


async def test_versioned_health_reads_the_same_checks(signed_in: AsyncClient) -> None:
    root = (await signed_in.get("/health/ready")).json()
    versioned = (await signed_in.get("/v1/system/health")).json()
    assert root["status"] == versioned["status"]
    assert set(root["checks"]) == set(versioned["checks"])


async def test_the_frontend_auth_calls_still_work_unprefixed(client: AsyncClient) -> None:
    assert (await client.post("/auth/register", json=ALICE)).status_code == 201
    assert (await client.get("/auth/me")).status_code == 200


async def test_openapi_documents_the_versioned_surface_only(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert "/v1/orders" in paths
    assert "/auth/me" not in paths  # legacy alias, deliberately undocumented
    assert "/health" in paths  # infrastructure contract, documented


async def test_no_duplicate_path_and_method_pairs(app: FastAPI) -> None:
    """Two handlers on one path and method is the duplicate-endpoint failure
    this level exists to avoid."""
    seen: set[tuple[str, str]] = set()
    for route in api_routes(app):
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            key = (method, path)
            assert key not in seen, f"duplicate route {method} {path}"
            seen.add(key)


# ---------------------------------------------------- 10. trading safety


async def test_safety_route_lists_every_unbuilt_gate(signed_in: AsyncClient) -> None:
    r = await signed_in.get("/v1/system/safety")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_mode"] == "paper"
    assert body["live_trading"] is False
    assert body["live_execution_allowed"] is False
    assert len(body["live_execution_blockers"]) >= 10
    assert not any(body["gates"].values())


@pytest.mark.parametrize(
    "path,level",
    [
        # /v1/market/* left this list at L08, /v1/strategies at L12 and
        # /v1/bots at L22: all three are built and serve real answers.
    ],
)
async def test_unbuilt_groups_name_their_level(
    app: FastAPI, client: AsyncClient, path: str, level: int
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    r = await client.get(path)
    assert r.status_code == 501
    assert f"level {level:02d}" in r.json()["error"]["detail"]


async def test_no_route_can_reach_a_broker(app: FastAPI) -> None:
    """No API route imports the MT5 terminal or the order sender. The path
    from a request to a broker does not exist, and this asserts the absence
    rather than trusting it."""
    import importlib
    import pkgutil

    import app.api as api_pkg

    forbidden = {"MetaTrader5", "mt5_paper", "tools.mt5_paper"}
    for module in pkgutil.walk_packages(api_pkg.__path__, prefix="app.api."):
        loaded = importlib.import_module(module.name)
        source_names = set(dir(loaded))
        assert not (source_names & forbidden), f"{module.name} reaches a broker"


# ============================================ POSITION SIZING (L18) surface


async def test_position_sizing_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/position-sizing/modes")).status_code == 401
    assert (await client.get("/v1/position-sizing/status")).status_code == 401
    r = await client.post(
        "/v1/position-sizing/calculate",
        json={"symbol": "EURUSD", "side": "buy"},
    )
    assert r.status_code == 401


async def test_position_sizing_calculates_against_the_stored_contract_spec(
    signed_in: AsyncClient,
) -> None:
    """EURUSD has a complete mt5 spec in the seed: 0.01 step, tick value 1 at a
    0.00001 tick. 10,000 at 1% is a 100 budget, a 0.005 stop costs 500 a lot,
    so the answer is 0.20 lots risking exactly 100."""
    r = await signed_in.post(
        "/v1/position-sizing/calculate",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "sizing_mode": "percent_equity",
            "entry_price": "1.10000",
            "stop_loss": "1.09500",
            "equity": "10000",
            "risk_percent": "1",
        },
        headers=_csrf(signed_in),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "VALID"
    assert body["final_quantity"] == "0.20"
    assert body["risk_amount"] == "100.0000"
    assert body["actual_risk"] == "100.0000"
    assert body["broker_constraints"]["broker_symbol"] == "EURUSD.R"
    # It is a quantity, not a permission.
    assert "not an approval" in body["authority"]


async def test_position_sizing_creates_no_order(signed_in: AsyncClient) -> None:
    """The endpoint that computes a size must not be able to place one."""
    before = (await signed_in.get("/v1/orders")).json()["page"]["total"]
    await signed_in.post(
        "/v1/position-sizing/calculate",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "sizing_mode": "fixed_risk",
            "entry_price": "1.10000",
            "stop_loss": "1.09500",
            "risk_amount": "100",
        },
        headers=_csrf(signed_in),
    )
    after = (await signed_in.get("/v1/orders")).json()["page"]["total"]
    assert after == before


async def test_position_sizing_refuses_an_unsynced_spec(signed_in: AsyncClient) -> None:
    """DE40's mapping exists with no contract terms, which is what an unsynced
    symbol looks like. Sizing from a defaulted tick value is the failure the
    module exists to prevent, so it is a refusal naming the missing fields."""
    r = await signed_in.post(
        "/v1/position-sizing/calculate",
        json={
            "symbol": "DE40",
            "side": "buy",
            "sizing_mode": "fixed_risk",
            "entry_price": "18000",
            "stop_loss": "17900",
            "risk_amount": "100",
        },
        headers=_csrf(signed_in),
    )
    assert r.status_code == 422
    assert "tick_value" in r.json()["error"]["detail"]


async def test_position_sizing_refuses_a_stop_on_the_wrong_side(
    signed_in: AsyncClient,
) -> None:
    r = await signed_in.post(
        "/v1/position-sizing/calculate",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "sizing_mode": "fixed_risk",
            "entry_price": "1.10000",
            "stop_loss": "1.10500",
            "risk_amount": "100",
        },
        headers=_csrf(signed_in),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "REFUSED"
    assert body["final_quantity"] is None
    assert "must sit below" in body["gap"]
    assert body["risk"]["evaluated"] is False


async def test_a_client_cannot_raise_its_own_risk_ceiling(signed_in: AsyncClient) -> None:
    """`max_risk_amount` is an input, not an instruction. A caller asking for
    more than it may have gets refused, never sized to the larger figure."""
    r = await signed_in.post(
        "/v1/position-sizing/calculate",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "sizing_mode": "fixed_risk",
            "entry_price": "1.10000",
            "stop_loss": "1.09500",
            "risk_amount": "5000",
            "max_risk_amount": "100",
        },
        headers=_csrf(signed_in),
    )
    assert r.json()["status"] == "REFUSED"
    assert "permitted maximum" in r.json()["gap"]


async def test_an_unknown_sizing_mode_is_refused(signed_in: AsyncClient) -> None:
    r = await signed_in.post(
        "/v1/position-sizing/calculate",
        json={"symbol": "EURUSD", "side": "buy", "sizing_mode": "kelly", "quantity": "1"},
        headers=_csrf(signed_in),
    )
    assert r.status_code == 422
    assert "kelly" in r.json()["error"]["detail"]


async def test_the_sizing_modes_are_documented_with_their_aliases(
    signed_in: AsyncClient,
) -> None:
    body = (await signed_in.get("/v1/position-sizing/modes")).json()
    assert {m["mode"] for m in body["modes"]} == {
        "fixed_quantity",
        "fixed_risk",
        "percent_equity",
    }
    assert body["aliases"]["fixed_lot"] == "fixed_quantity"
    assert body["aliases"]["monetary_risk"] == "fixed_risk"
    assert "not a separate mode" in body["atr_sizing"].lower()


async def test_the_sizing_status_reports_its_counters(signed_in: AsyncClient) -> None:
    body = (await signed_in.get("/v1/position-sizing/status")).json()
    for key in (
        "position_sizing_requests_total",
        "position_sizing_success_total",
        "position_sizing_rejections_total",
        "position_sizing_errors_total",
        "broker_metadata_failures",
        "quantity_calculation_latency_ms",
    ):
        assert key in body
    assert "app.risk decides" in body["authority"]


async def test_the_sizing_route_reaches_no_broker(app: FastAPI) -> None:
    """The sizing router must not hold an adapter. It receives normalized
    contract terms from the platform's own symbol mappings, which is what
    keeps the engine broker-independent."""
    import inspect

    from app.api.v1 import position_sizing

    source = inspect.getsource(position_sizing)
    for forbidden in ("MetaTrader5", "mt5_paper", "BrokerRegistry", "place_order"):
        assert forbidden not in source


# =========================================== AUTOMATED EXECUTION (L20)


async def test_the_execution_engine_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/execution/status")).status_code == 401
    assert (await client.post("/v1/execution/start")).status_code == 401


async def test_the_execution_worker_is_registered_and_not_started(
    app: FastAPI, signed_in: AsyncClient
) -> None:
    """Registered so it is supervised and visible; not started, because
    beginning to consume signals is an operator decision rather than a side
    effect of the process booting."""
    body = (await signed_in.get("/v1/execution/status")).json()
    assert body["worker"] == "execution"
    assert body["supervisor"]["running"] is False
    assert "app.risk, app.sizing and the OMS do" in body["authority"]
    assert "Closing a browser stops nothing" in body["browser_independent"]


async def test_starting_execution_needs_the_trading_permission(
    app: FastAPI, signed_in: AsyncClient
) -> None:
    """A plain USER can watch the engine and cannot start it."""
    assert (
        await signed_in.post("/v1/execution/start", headers=_csrf(signed_in))
    ).status_code == 403


async def test_the_engine_can_be_started_and_stopped(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)

    started = await client.post("/v1/execution/start", headers=_csrf(client))
    assert started.status_code == 200
    assert started.json()["running"] is True
    # Starting grants no permission the engine did not have.
    assert "still requires a risk Approval" in started.json()["note"]

    again = await client.post("/v1/execution/start", headers=_csrf(client))
    assert again.status_code == 409

    stopped = await client.post("/v1/execution/stop", headers=_csrf(client))
    assert stopped.status_code == 200
    # Stopping the consumer is not a way to unsend an order.
    assert "does not unsend anything" in stopped.json()["note"]


async def test_there_is_no_route_that_executes_a_payload_directly() -> None:
    """Signals reach the engine as recorded rows, never as a request body, so
    there is exactly one consumer of the execution path."""
    import inspect

    from app.api.v1 import execution

    source = inspect.getsource(execution)
    assert "IncomingSignal" not in source
    assert "pipeline.process" not in source


# ============================================ POSITION MANAGEMENT (L21)


async def _open_position(app: FastAPI, **over: object) -> str:
    from app.models.execution import Position

    fields: dict[str, object] = {
        "id": "pos-live-1",
        "mode": "paper",
        "symbol_id": "sym-eur",
        "side": "long",
        "quantity": Decimal("0.10"),
        "initial_quantity": Decimal("0.10"),
        "entry_price": Decimal("1.10000"),
        "stop_loss": Decimal("1.09000"),
        "take_profit": Decimal("1.11000"),
        "status": "open",
        "opened_at": BASE,
        "source": "simulator",
    }
    fields.update(over)
    async with app.state.session_factory() as db:
        db.add(Position(**fields))
        await db.commit()
    return str(fields["id"])


async def test_closing_a_position_needs_the_permission(
    app: FastAPI, signed_in: AsyncClient
) -> None:
    position_id = await _open_position(app)
    r = await signed_in.post(
        f"/v1/positions/{position_id}/close", json={}, headers=_csrf(signed_in)
    )
    assert r.status_code == 403


async def test_a_position_that_does_not_exist_is_a_404(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/positions/nope/close", json={}, headers=_csrf(client))
    assert r.status_code == 404


async def test_a_close_larger_than_the_position_is_refused_not_clamped(
    app: FastAPI, client: AsyncClient
) -> None:
    position_id = await _open_position(app)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        f"/v1/positions/{position_id}/close",
        json={"quantity": "5.00"},
        headers=_csrf(client),
    )
    assert r.status_code == 422
    assert "refusing rather than clamping" in r.json()["error"]["detail"]


@pytest.mark.parametrize("status", ["unknown", "reconciling", "closing", "opening"])
async def test_an_unsettled_position_cannot_be_closed_by_hand(
    app: FastAPI, client: AsyncClient, status: str
) -> None:
    """Each of these means the venue's answer is not settled. Closing could
    double-close a position that is still open."""
    position_id = await _open_position(app, id=f"pos-{status}", status=status)
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(f"/v1/positions/{position_id}/close", json={}, headers=_csrf(client))
    assert r.status_code == 409


async def test_a_stop_on_the_wrong_side_of_the_entry_is_refused(
    app: FastAPI, client: AsyncClient
) -> None:
    """That is a target, not a wide stop. Refused rather than corrected — the
    same rule position sizing applies at L18."""
    position_id = await _open_position(app, id="pos-protect")
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        f"/v1/positions/{position_id}/protect",
        json={"stop_loss": "1.20000"},
        headers=_csrf(client),
    )
    assert r.status_code == 422
    assert "that is a target" in r.json()["error"]["detail"]


async def test_setting_a_stop_records_who_did_it_and_says_what_it_means(
    app: FastAPI, client: AsyncClient
) -> None:
    from app.models.execution import PositionEvent

    position_id = await _open_position(app, id="pos-protect-2")
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        f"/v1/positions/{position_id}/protect",
        json={"stop_loss": "1.09500", "reason": "tightened by hand"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stop_loss"] == "1.09500"
    # What we intend and what the venue holds are separate facts, and the
    # response says so rather than implying the venue agreed.
    assert body["broker_stop_loss"] is None
    assert "what the platform INTENDS" in body["note"] or "intends" in body["note"].lower()

    async with app.state.session_factory() as db:
        events = (
            await db.scalars(select(PositionEvent).where(PositionEvent.position_id == position_id))
        ).all()
        assert [e.event_type for e in events] == ["protection_set"]
        assert events[0].payload["by_user"]
        assert events[0].payload["reason"] == "tightened by hand"


async def test_protect_needs_at_least_one_level(app: FastAPI, client: AsyncClient) -> None:
    position_id = await _open_position(app, id="pos-protect-3")
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(f"/v1/positions/{position_id}/protect", json={}, headers=_csrf(client))
    assert r.status_code == 422


async def test_a_null_field_does_not_remove_a_stop(app: FastAPI, client: AsyncClient) -> None:
    """An API where a missing field deletes a stop is an API where a typo
    does."""
    from app.models.execution import Position

    position_id = await _open_position(app, id="pos-protect-4")
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await client.post(
        f"/v1/positions/{position_id}/protect",
        json={"take_profit": "1.12000"},
        headers=_csrf(client),
    )
    async with app.state.session_factory() as db:
        row = await db.get(Position, position_id)
        assert row is not None
        assert row.stop_loss == Decimal("1.09000")  # untouched
        assert row.take_profit == Decimal("1.12000")


async def test_reconcile_refuses_without_a_registered_venue(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/positions/reconcile?account_id=acct-a", headers=_csrf(client))
    assert r.status_code == 409
    assert "no order manager is registered" in r.json()["error"]["detail"]


# ================================================ BOT MANAGER (L22)


async def _make_bot(app: FastAPI, user_email: str = ALICE["email"], **over: object) -> str:
    from app.auth.models import User
    from app.models.bots import Bot

    async with app.state.session_factory() as db:
        owner = await db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        fields: dict[str, object] = {
            "id": "bot-1",
            "user_id": owner.id,
            "name": "EURUSD test bot",
            "mode": "paper",
            "is_enabled": True,
        }
        fields.update(over)
        db.add(Bot(**fields))
        await db.commit()
    return str(fields["id"])


async def test_the_bot_manager_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/bots")).status_code == 401
    assert (await client.post("/v1/bots/supervise")).status_code == 401


async def test_a_bot_belonging_to_someone_else_is_reported_absent(
    app: FastAPI, client: AsyncClient
) -> None:
    """ "This id exists but is not yours" is itself information.

    TRADER, deliberately: a USER is refused before ownership is ever consulted,
    so only a caller who could legitimately hold a bot proves the point."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.get("/v1/bots/no-such-bot")
    assert r.status_code == 404


async def test_the_bot_list_reports_measured_health(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _make_bot(app)
    body = (await client.get("/v1/bots")).json()
    assert body["counts"]["total"] == 1
    item = body["items"][0]
    assert item["status"] is None  # never run
    assert item["heartbeat_stale"] is False
    assert item["limits"]["max_positions"] is None


async def test_the_whole_bot_group_needs_the_permission(
    app: FastAPI, signed_in: AsyncClient
) -> None:
    """A plain USER is refused on reads as well as writes.

    `RESOURCE_MIN_ROLE["bots"]` is TRADER and the 501 stub this group replaced
    enforced that, so building it must not have opened it. Reading a bot means
    reading its mode, its limits and why it last stopped -- the operational
    state of an automated trader, not the read-only view of results a new
    account gets."""
    bot_id = await _make_bot(app)
    assert (await signed_in.get("/v1/bots")).status_code == 403
    assert (await signed_in.get(f"/v1/bots/{bot_id}")).status_code == 403
    assert (await signed_in.get(f"/v1/bots/{bot_id}/events")).status_code == 403
    r = await signed_in.patch(
        f"/v1/bots/{bot_id}/limits", json={"max_positions": 3}, headers=_csrf(signed_in)
    )
    assert r.status_code == 403
    r = await signed_in.post(f"/v1/bots/{bot_id}/preflight", headers=_csrf(signed_in))
    assert r.status_code == 403


async def test_setting_bot_limits_stores_them(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    bot_id = await _make_bot(app)
    r = await client.patch(
        f"/v1/bots/{bot_id}/limits",
        json={"max_positions": 3, "max_daily_trades": 10, "cooldown_seconds": 600},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    assert r.json()["limits"]["max_positions"] == 3
    assert r.json()["limits"]["cooldown_seconds"] == 600


async def test_disabling_a_bot_closes_no_position(app: FastAPI, client: AsyncClient) -> None:
    """§24 and §32: stopping or disabling a bot stops new trades and nothing
    else. Everything it holds stays under the position manager."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    bot_id = await _make_bot(app)

    r = await client.post(
        f"/v1/bots/{bot_id}/disable",
        json={"reason": "incident review"},
        headers=_csrf(client),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["disabled"] is True
    assert "No position was closed" in body["note"]

    detail = (await client.get(f"/v1/bots/{bot_id}")).json()
    assert detail["disabled"] is True
    assert detail["disabled_reason"] == "incident review"


async def test_re_enabling_does_not_start_the_bot(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    bot_id = await _make_bot(app, is_disabled=True, is_enabled=False)

    r = await client.post(f"/v1/bots/{bot_id}/enable", headers=_csrf(client))
    assert r.status_code == 200
    assert r.json()["running"] is False
    assert "NOT started" in r.json()["note"]
    # And enabling one that is not disabled is a conflict, not a silent no-op.
    again = await client.post(f"/v1/bots/{bot_id}/enable", headers=_csrf(client))
    assert again.status_code == 409


async def test_a_live_bot_cannot_pass_preflight_while_live_is_off(
    app: FastAPI, client: AsyncClient
) -> None:
    """§57. The refusal names the SETTING, not the request: a caller retrying
    with different JSON should learn the answer will not change."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    bot_id = await _make_bot(app, mode="live")

    r = await client.post(f"/v1/bots/{bot_id}/preflight", headers=_csrf(client))
    assert r.status_code == 200
    body = r.json()
    assert body["would_start"] is False
    mode = next(c for c in body["checks"] if c["check"] == "trading_mode")
    assert mode["passed"] is False
    assert "LIVE_TRADING is false" in mode["detail"]


async def test_preflight_starts_nothing(app: FastAPI, client: AsyncClient) -> None:
    from app.models.bots import BotRun

    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    bot_id = await _make_bot(app)
    await client.post(f"/v1/bots/{bot_id}/preflight", headers=_csrf(client))
    async with app.state.session_factory() as db:
        assert (await db.scalars(select(BotRun))).all() == []


async def test_the_restart_plan_starts_nothing(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _make_bot(app)
    r = await client.get("/v1/bots/supervise/restart-plan", headers=_csrf(client))
    assert r.status_code == 200
    body = r.json()
    assert "Nothing was started" in body["note"]
    assert body["plans"][0]["action"] == "leave"


async def test_the_supervisor_sweep_reports_what_it_measured(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/bots/supervise", headers=_csrf(client))
    assert r.status_code == 200
    body = r.json()
    assert body["checked"] == 0
    assert "A heartbeat is measured" in body["note"]


async def test_there_is_no_bot_route_that_trades() -> None:
    """§59: the bot manager is a control plane. It evaluates no strategy,
    sizes no position and submits no order."""
    import inspect

    from app.api.v1 import bots as router_module

    source = inspect.getsource(router_module)
    for forbidden in ("OrderProposal", "place_order", "SizingRequest", "Approval"):
        assert forbidden not in source, f"the bot router mentions {forbidden}"


# ---------------------------------------------- 12. the data pipeline (L23)


async def _seed_bars(app: FastAPI, count: int = 400) -> None:
    """Enough stored history for a dataset to be buildable."""
    import math
    import random

    from app.models.market import MarketBar

    rng = random.Random(11)
    price = 1.10
    async with app.state.session_factory() as db:
        for i in range(count):
            price = max(0.5, price * (1 + 0.00002 * math.sin(i / 17.0) + rng.gauss(0, 0.0008)))
            high = price * (1 + abs(rng.gauss(0, 0.0006)))
            low = price * (1 - abs(rng.gauss(0, 0.0006)))
            open_ = min(max(price * (1 + rng.gauss(0, 0.0004)), low), high)
            db.add(
                MarketBar(
                    provider="mt5",
                    provider_symbol="EURUSD.R",
                    symbol_id="sym-eur",
                    timeframe="H1",
                    bar_time=BASE + timedelta(hours=i),
                    open=Decimal(f"{open_:.5f}"),
                    high=Decimal(f"{high:.5f}"),
                    low=Decimal(f"{low:.5f}"),
                    close=Decimal(f"{price:.5f}"),
                    volume=Decimal(rng.randint(100, 900)),
                    spread=Decimal("0.00002"),
                    spread_availability="available",
                    ingested_at=BASE,
                )
            )
        await db.commit()


BUILD = {
    "key": "eurusd-h1",
    "symbol": "EURUSD",
    "provider_symbol": "EURUSD.R",
    "provider": "mt5",
    "timeframe": "H1",
    "horizon": 12,
    "spread_points": "0.00002",
}


async def test_the_data_pipeline_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/datasets")).status_code == 401
    assert (await client.get("/v1/datasets/features")).status_code == 401
    assert (await client.post("/v1/datasets/build", json=BUILD)).status_code == 401


async def test_a_plain_user_cannot_read_training_data(signed_in: AsyncClient) -> None:
    """Training data is the input a trading model is fitted on, not the
    read-only view of results a new account is given."""
    assert (await signed_in.get("/v1/datasets")).status_code == 403
    assert (await signed_in.get("/v1/datasets/features")).status_code == 403
    r = await signed_in.post("/v1/datasets/build", json=BUILD, headers=_csrf(signed_in))
    assert r.status_code == 403


async def test_the_feature_registry_states_its_units_and_timing(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/datasets/features")).json()
    assert body["feature_set_version"]
    assert body["features"]
    assert all(f["timestamp_policy"] == "at_or_before_T" for f in body["features"])
    # No raw price level is offered, which is the metals-points lesson applied.
    assert all(f["unit"] != "price" for f in body["features"])
    assert "no second indicator engine" in body["indicator_source"]


async def test_the_label_registry_states_the_separation(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/datasets/labels")).json()
    assert all("after T" in label["uses"] for label in body["labels"])
    assert "strictly after T" in body["separation"]
    assert "never" in body["tail_policy"]


async def test_the_engine_route_says_what_it_will_not_do(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/datasets/engine")).json()
    joined = " ".join(body["does_not"])
    assert "train, deploy or replace a model" in joined
    assert "write to market_bars" in joined
    assert "READY when a leakage check failed" in joined


async def test_building_a_dataset_records_it_and_trains_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _seed_bars(app)

    r = await client.post("/v1/datasets/build", json=BUILD, headers=_csrf(client))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "READY"
    assert body["ready"] is True
    assert body["rows"] > 0
    assert body["leakage"]["passed"] is True
    assert body["fingerprint"]
    assert "Nothing was trained" in body["note"]

    listing = (await client.get("/v1/datasets")).json()
    assert listing["counts"]["total"] == 1
    assert listing["counts"]["ready"] == 1


async def test_the_manifest_explains_the_dataset_without_the_code(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _seed_bars(app)
    built = (await client.post("/v1/datasets/build", json=BUILD, headers=_csrf(client))).json()

    manifest = (await client.get(f"/v1/datasets/{built['id']}/manifest")).json()
    assert manifest["fingerprint"] == built["fingerprint"]
    assert manifest["alignment"].startswith("row i holds features")
    assert manifest["split"]["ordering"].startswith("chronological")
    assert manifest["scaler"]["fitted_on"] == "train"
    assert manifest["leakage"]["passed"] is True


async def test_every_check_is_recorded_including_the_ones_that_passed(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _seed_bars(app)
    built = (await client.post("/v1/datasets/build", json=BUILD, headers=_csrf(client))).json()

    body = (await client.get(f"/v1/datasets/{built['id']}/checks")).json()
    assert {item["category"] for item in body["items"]} == {"leakage", "quality", "split"}
    assert all(item["passed"] for item in body["items"])
    assert "has been looked at" in body["note"]


async def test_building_without_stored_bars_is_refused_with_the_reason(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/datasets/build", json=BUILD, headers=_csrf(client))
    assert r.status_code == 422
    assert "no stored bars" in r.json()["error"]["detail"]


async def test_a_build_without_a_stated_cost_is_refused(app: FastAPI, client: AsyncClient) -> None:
    """`spread_points` has no default. The one effect this repository has
    measured to significance is cost drag."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = {k: v for k, v in BUILD.items() if k != "spread_points"}
    r = await client.post("/v1/datasets/build", json=body, headers=_csrf(client))
    assert r.status_code == 422


async def test_an_unknown_timeframe_or_provider_is_refused_not_guessed(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/datasets/build", json={**BUILD, "timeframe": "H3"}, headers=_csrf(client)
    )
    assert r.status_code == 422
    r = await client.post(
        "/v1/datasets/build", json={**BUILD, "provider": "bloomberg"}, headers=_csrf(client)
    )
    assert r.status_code == 422


async def test_an_unknown_dataset_is_a_404(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    assert (await client.get("/v1/datasets/no-such-id")).status_code == 404
    assert (await client.get("/v1/datasets/no-such-id/manifest")).status_code == 404
    assert (await client.get("/v1/datasets/no-such-id/checks")).status_code == 404


async def test_there_is_no_route_that_marks_a_dataset_ready(app: FastAPI) -> None:
    """The builder decides. An operator who disagrees has to fix the data."""
    methods = {
        (getattr(route, "path", ""), method)
        for route in api_routes(app)
        for method in getattr(route, "methods", set())
        if getattr(route, "path", "").startswith("/v1/datasets")
    }
    assert methods
    assert not any(m in {"PATCH", "PUT", "DELETE"} for _, m in methods)


async def test_the_dataset_router_reaches_no_model_and_no_venue() -> None:
    import inspect

    from app.api.v1 import datasets as router_module

    source = inspect.getsource(router_module)
    for forbidden in ("BrokerAdapter", "OrderManager", "RiskEngine", "sklearn", "torch"):
        assert forbidden not in source, f"the dataset router mentions {forbidden}"


# ------------------------------------------------- 13. the AI layer (L24)


def _fit_models(app: FastAPI) -> None:
    """Register a fitted regime model on the running app.

    Fitted here rather than at startup because §36 is explicit: nothing loads
    or trains a model as a side effect of the level existing.
    """
    import math
    import random

    from app.ai import RegimeModel, fit_cuts
    from app.datasets.features import FEATURE_SET_VERSION

    rng = random.Random(5)
    rows: list[dict[str, float | None]] = []
    for i in range(200):
        rows.append(
            {
                "ema_spread_10_50": rng.gauss(0, 0.002) + 0.0005 * math.sin(i / 11.0),
                "atr_pct_14": abs(rng.gauss(0.0012, 0.0004)) + 1e-6,
            }
        )
    app.state.ai_models.register(
        RegimeModel(version="1.0", feature_version=FEATURE_SET_VERSION, cuts=fit_cuts(rows))
    )


def _predict_body(**over: object) -> dict[str, object]:
    from app.datasets.features import FEATURE_SET_VERSION

    body: dict[str, object] = {
        "model_key": "regime",
        "model_version": "1.0",
        "symbol": "EURUSD",
        "timeframe": "H1",
        "feature_version": FEATURE_SET_VERSION,
        "features": {"ema_spread_10_50": 0.001, "atr_pct_14": 0.0012},
    }
    body.update(over)
    return body


async def test_the_ai_layer_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/ai/models")).status_code == 401
    assert (await client.get("/v1/ai/vocabulary")).status_code == 401
    assert (await client.post("/v1/ai/predict", json=_predict_body())).status_code == 401


async def test_a_plain_user_cannot_reach_the_ai_layer(signed_in: AsyncClient) -> None:
    assert (await signed_in.get("/v1/ai/models")).status_code == 403
    r = await signed_in.post("/v1/ai/predict", json=_predict_body(), headers=_csrf(signed_in))
    assert r.status_code == 403


async def test_no_model_is_loaded_by_default(app: FastAPI, client: AsyncClient) -> None:
    """§36: level 24 does not load or train anything as a side effect."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/models")).json()
    assert body["counts"]["loaded"] == 0
    assert "advisory" in body["authority"]


async def test_asking_an_unregistered_model_is_a_404_not_a_substitution(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/ai/predict", json=_predict_body(), headers=_csrf(client))
    assert r.status_code == 404
    assert "Nothing is substituted" in r.json()["error"]["detail"]


async def test_a_registered_model_answers_and_places_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    _fit_models(app)

    r = await client.post("/v1/ai/predict", json=_predict_body(), headers=_csrf(client))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "OK"
    assert body["prediction"] in {
        "TRENDING_UP",
        "TRENDING_DOWN",
        "RANGING",
        "HIGH_VOLATILITY",
        "LOW_VOLATILITY",
        "UNKNOWN",
    }
    assert body["prediction_id"]
    assert body["explanation"]
    assert "places nothing" in body["note"]


async def test_a_wrong_feature_version_blocks_through_the_api(
    app: FastAPI, client: AsyncClient
) -> None:
    """§51 end to end: incompatible feature version -> BLOCK, not a guess."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    _fit_models(app)

    r = await client.post(
        "/v1/ai/predict",
        json=_predict_body(feature_version="9.9"),
        headers=_csrf(client),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "FEATURE_VERSION_MISMATCH"
    assert body["prediction"] is None
    assert body["explanation"] == []


async def test_a_feature_the_model_does_not_read_is_refused_not_ignored(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    _fit_models(app)

    r = await client.post(
        "/v1/ai/predict",
        json=_predict_body(
            features={"ema_spread_10_50": 0.001, "atr_pct_14": 0.0012, "moon_phase": 1.0}
        ),
        headers=_csrf(client),
    )
    assert r.status_code == 422
    assert "moon_phase" in r.json()["error"]["detail"]


async def test_the_vocabulary_states_what_a_probability_is_not(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/vocabulary")).json()
    joined = " ".join(body["rules"])
    assert "never P(profit)" in joined
    assert "calibrated is false until calibration has been measured" in joined
    assert "labels and never vetoes" in joined
    assert body["prediction_statuses"]["MODEL_UNAVAILABLE"]


async def test_one_model_can_be_described_with_its_contract(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    _fit_models(app)

    body = (await client.get("/v1/ai/models/regime?version=1.0")).json()
    assert body["fitted"] is True
    assert body["contract"]["features"] == ["ema_spread_10_50", "atr_pct_14"]
    assert "cannot place, size or approve" in body["authority"]
    assert body["versions"] == ["1.0"]


async def test_there_is_no_route_that_promotes_or_deletes_a_model(app: FastAPI) -> None:
    """§36 and §32: promotion is level 28's, and nothing here replaces a model."""
    methods = {
        (getattr(route, "path", ""), method)
        for route in api_routes(app)
        for method in getattr(route, "methods", set())
        if getattr(route, "path", "").startswith("/v1/ai")
    }
    assert methods
    assert not any(m in {"PATCH", "PUT", "DELETE"} for _, m in methods)


async def test_the_ai_router_reaches_no_venue_and_no_execution_path() -> None:
    import inspect

    from app.api.v1 import ai as router_module

    source = inspect.getsource(router_module)
    for forbidden in ("BrokerAdapter", "OrderManager", "RiskEngine", "SizingRequest", "Approval"):
        assert forbidden not in source, f"the AI router mentions {forbidden}"


# ------------------------------------------------ 14. the training engine (L25)


TRAIN = {
    "family": "trade_probability",
    "dataset_key": "eurusd-h1",
    "dataset_version": "1",
    "model_version": "1.0",
    "features": ["rsi_14", "atr_pct_14"],
    "iterations": 20,
    "minimum_rows": 100,
}


async def _ready_dataset(app: FastAPI, client: AsyncClient) -> dict[str, object]:
    await _seed_bars(app, 600)
    built = await client.post(
        "/v1/datasets/build",
        json={**BUILD, "key": "eurusd-h1", "horizon": 12},
        headers=_csrf(client),
    )
    assert built.status_code == 200, built.text
    return built.json()


async def test_the_training_engine_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/ai/training/engine")).status_code == 401
    assert (await client.get("/v1/ai/training/jobs")).status_code == 401
    assert (await client.post("/v1/ai/training/jobs", json=TRAIN)).status_code == 401


async def test_a_plain_user_cannot_start_training(signed_in: AsyncClient) -> None:
    r = await signed_in.post("/v1/ai/training/jobs", json=TRAIN, headers=_csrf(signed_in))
    assert r.status_code == 403
    assert (await signed_in.get("/v1/ai/training/jobs")).status_code == 403


async def test_the_engine_route_says_what_training_will_not_do(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/training/engine")).json()
    joined = " ".join(body["does_not"])
    assert "promote a model" in joined
    assert "reach a strategy" in joined
    assert "arbitrary code" in joined
    assert "L26 validates it and L28 promotes it" in body["handoff"]
    assert body["limits"]["max_concurrent"] >= 1


async def test_training_cannot_name_a_dataset_that_does_not_exist(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post("/v1/ai/training/jobs", json=TRAIN, headers=_csrf(client))
    assert r.status_code == 404
    assert "no option to use whatever is current" in r.json()["error"]["detail"]


async def test_a_dataset_that_is_not_ready_cannot_be_trained_on_through_the_api(
    app: FastAPI, client: AsyncClient
) -> None:
    """§7 end to end: L23's verdict is the gate, not a second opinion."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    # 70 bars, a 50-bar warm-up and a 12-bar horizon leave 8 usable rows, which
    # the builder refuses rather than pads. A CLEAN dataset, not a READY one.
    await _seed_bars(app, 70)
    built = await client.post(
        "/v1/datasets/build",
        json={**BUILD, "key": "eurusd-h1", "horizon": 12},
        headers=_csrf(client),
    )
    assert built.json()["status"] != "READY"

    r = await client.post("/v1/ai/training/jobs", json=TRAIN, headers=_csrf(client))
    assert r.status_code == 422
    assert "would make the whole check ceremonial" in r.json()["error"]["detail"]


async def test_an_unknown_model_family_is_refused_not_guessed(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/ai/training/jobs", json={**TRAIN, "family": "oracle"}, headers=_csrf(client)
    )
    assert r.status_code == 422
    assert "refused rather than guessed" in r.json()["error"]["detail"]


async def test_a_split_with_no_holdout_is_refused_through_the_api(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/ai/training/jobs",
        json={**TRAIN, "train_fraction": 0.9, "validation_fraction": 0.1},
        headers=_csrf(client),
    )
    assert r.status_code == 422


async def test_queueing_a_job_returns_immediately_and_promotes_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _ready_dataset(app, client)

    r = await client.post("/v1/ai/training/jobs", json=TRAIN, headers=_csrf(client))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("queued", "running")
    assert body["model_version_id"] is None
    assert "Nothing is promoted" in body["note"]

    job_id = body["id"]
    assert await app.state.training.wait_for(job_id)
    await app.state.training.shutdown()

    detail = (await client.get(f"/v1/ai/training/jobs/{job_id}")).json()
    assert detail["status"] == "validation_pending"
    assert detail["model_version_id"]
    assert detail["config"]["family"] == "trade_probability"

    versions = (await client.get("/v1/ai/models/versions")).json()
    assert versions["items"]
    # DRAFT, always. Promotion is level 28's.
    assert all(v["status"] == "draft" for v in versions["items"])


async def test_the_metrics_route_keeps_the_two_kinds_apart(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _ready_dataset(app, client)
    job_id = (await client.post("/v1/ai/training/jobs", json=TRAIN, headers=_csrf(client))).json()[
        "id"
    ]
    assert await app.state.training.wait_for(job_id)
    await app.state.training.shutdown()

    body = (await client.get(f"/v1/ai/training/jobs/{job_id}/metrics")).json()
    assert "classification" in body["metrics"]
    assert "economic" in body["metrics"]
    assert "baseline" in body["metrics"]
    assert "never merged" in body["note"]


async def test_cancelling_a_job_that_is_not_running_says_so(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _ready_dataset(app, client)
    job_id = (await client.post("/v1/ai/training/jobs", json=TRAIN, headers=_csrf(client))).json()[
        "id"
    ]
    assert await app.state.training.wait_for(job_id)
    await app.state.training.shutdown()

    r = await client.post(f"/v1/ai/training/jobs/{job_id}/cancel", headers=_csrf(client))
    assert r.status_code == 200
    assert r.json()["cancelling"] is False
    assert "nothing to stop" in r.json()["note"]


async def test_an_unknown_training_job_is_a_404(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    assert (await client.get("/v1/ai/training/jobs/no-such-job")).status_code == 404
    assert (await client.get("/v1/ai/training/jobs/no-such-job/metrics")).status_code == 404
    r = await client.post("/v1/ai/training/jobs/no-such-job/cancel", headers=_csrf(client))
    assert r.status_code == 404


async def test_there_is_no_route_that_promotes_a_trained_model(app: FastAPI) -> None:
    """§26: promotion is a separate, controlled process and not this level's."""
    methods = {
        (getattr(route, "path", ""), method)
        for route in api_routes(app)
        for method in getattr(route, "methods", set())
        if getattr(route, "path", "").startswith("/v1/ai/training")
    }
    assert methods
    assert not any(m in {"PATCH", "PUT", "DELETE"} for _, m in methods)
    assert not any("promote" in path for path, _ in methods)


# ---------------------------------------------- 15. the validation engine (L26)


async def _a_candidate(app: FastAPI, client: AsyncClient) -> str:
    """One real training run through the API, so validation has a real artifact."""
    await _ready_dataset(app, client)
    job_id = (await client.post("/v1/ai/training/jobs", json=TRAIN, headers=_csrf(client))).json()[
        "id"
    ]
    assert await app.state.training.wait_for(job_id)
    await app.state.training.shutdown()
    detail = (await client.get(f"/v1/ai/training/jobs/{job_id}")).json()
    assert detail["model_version_id"], detail
    return detail["model_version_id"]


def _validation_body(version_id: str, **over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model_version_id": version_id,
        "dataset_key": "eurusd-h1",
        "dataset_version": "1",
        "thresholds": {"minimum_samples": 50, "minimum_trades": 5, "permutations": 50},
    }
    body.update(over)
    return body


async def test_the_validation_engine_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/ai/validation/engine")).status_code == 401
    assert (await client.get("/v1/ai/validation/runs")).status_code == 401
    assert (await client.post("/v1/ai/validation/runs", json={})).status_code == 401


async def test_a_plain_user_cannot_request_a_validation(signed_in: AsyncClient) -> None:
    r = await signed_in.post(
        "/v1/ai/validation/runs", json=_validation_body("x"), headers=_csrf(signed_in)
    )
    assert r.status_code == 403
    assert (await signed_in.get("/v1/ai/validation/runs")).status_code == 403


async def test_the_engine_route_says_what_a_pass_does_not_authorise(
    app: FastAPI, client: AsyncClient
) -> None:
    """§34, served rather than only documented."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/validation/engine")).json()
    joined = " ".join(body["does_not"])
    assert "promote, activate, deploy or retire a model" in joined
    assert "enable live trading" in joined
    assert "fabricate a metric" in joined
    assert "CONSIDERATION" in body["handoff"]
    assert "no score" in " ".join(body["guarantees"])
    assert body["verdicts"]["PASS"]
    assert "not FAIL" in body["verdicts"]["BLOCKED"]
    # Every threshold is served, so a caller can see the bar before choosing one.
    assert body["default_thresholds"]["minimum_profit_factor"]


async def test_validation_cannot_name_a_candidate_that_does_not_exist(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/ai/validation/runs", json=_validation_body("nope"), headers=_csrf(client)
    )
    assert r.status_code == 404
    assert "no option to validate whatever is current" in r.json()["error"]["detail"]


async def test_validation_refuses_a_dataset_that_is_not_ready(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    version_id = await _a_candidate(app, client)
    r = await client.post(
        "/v1/ai/validation/runs",
        json=_validation_body(version_id, dataset_key="nothing-here"),
        headers=_csrf(client),
    )
    assert r.status_code == 404
    assert "not re-checkable" in r.json()["error"]["detail"]


async def test_a_validation_run_produces_a_report_and_promotes_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    """The end-to-end path, and the guarantee the whole level rests on."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    version_id = await _a_candidate(app, client)

    queued = await client.post(
        "/v1/ai/validation/runs", json=_validation_body(version_id), headers=_csrf(client)
    )
    assert queued.status_code == 200, queued.text
    body = queued.json()
    assert body["status"] in ("queued", "running")
    assert body["verdict"] is None
    assert "changes no model's status" in body["note"]

    run_id = body["id"]
    assert await app.state.validation.wait_for(run_id)
    await app.state.validation.shutdown()

    detail = (await client.get(f"/v1/ai/validation/runs/{run_id}")).json()
    assert detail["status"] == "completed"
    assert detail["verdict"] in ("PASS", "FAIL", "CONDITIONAL", "BLOCKED")
    assert "CONSIDERATION" in detail["authority"]

    report = (await client.get(f"/v1/ai/validation/runs/{run_id}/report")).json()["report"]
    assert report["verdict"] == detail["verdict"]
    assert "score" not in report
    assert {c["check"] for c in report["checks"]} >= {"leakage", "economic", "significance"}

    # And the candidate is still a draft.
    versions = (await client.get("/v1/ai/models/versions")).json()
    assert all(v["status"] == "draft" for v in versions["items"])


async def test_an_absent_report_is_not_a_passing_one(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    version_id = await _a_candidate(app, client)
    run_id = (
        await client.post(
            "/v1/ai/validation/runs", json=_validation_body(version_id), headers=_csrf(client)
        )
    ).json()["id"]
    body = (await client.get(f"/v1/ai/validation/runs/{run_id}/report")).json()
    if body["report"] is None:
        assert "not a passing one" in body["note"]
    await app.state.validation.wait_for(run_id)
    await app.state.validation.shutdown()


async def test_a_threshold_outside_its_range_is_refused_by_the_schema(
    app: FastAPI, client: AsyncClient
) -> None:
    """§41. A caller cannot set a bar the engine would not stand behind."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    version_id = await _a_candidate(app, client)
    r = await client.post(
        "/v1/ai/validation/runs",
        json=_validation_body(version_id, thresholds={"permutations": 2}),
        headers=_csrf(client),
    )
    assert r.status_code == 422
    r = await client.post(
        "/v1/ai/validation/runs",
        json=_validation_body(version_id, decision_threshold=1.4),
        headers=_csrf(client),
    )
    assert r.status_code == 422


async def test_an_unknown_validation_run_is_a_404(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    assert (await client.get("/v1/ai/validation/runs/no-such-run")).status_code == 404
    assert (await client.get("/v1/ai/validation/runs/no-such-run/report")).status_code == 404
    r = await client.post("/v1/ai/validation/runs/no-such-run/cancel", headers=_csrf(client))
    assert r.status_code == 404


async def test_there_is_no_route_that_promotes_a_validated_model(app: FastAPI) -> None:
    """§34: a PASS is not a promotion, and no verb here could make it one."""
    methods = {
        (getattr(route, "path", ""), method)
        for route in api_routes(app)
        for method in getattr(route, "methods", set())
        if getattr(route, "path", "").startswith("/v1/ai/validation")
    }
    assert methods
    assert not any(m in {"PATCH", "PUT", "DELETE"} for _, m in methods)
    for word in ("promote", "activate", "deploy", "approve"):
        assert not any(word in path for path, _ in methods)


# ------------------------------------------- 16. AI strategy integration (L27)


AI_CONFIG = {
    "mode": "AI_FILTER",
    "policy": "AI_OPTIONAL",
    "required_models": [{"key": "trade_probability", "version": "1.0"}],
    "minimum_probability": 0.6,
}


async def _validated_candidate(app: FastAPI, client: AsyncClient) -> str:
    """A candidate that has been trained AND validated, through the API."""
    version_id = await _a_candidate(app, client)
    run_id = (
        await client.post(
            "/v1/ai/validation/runs", json=_validation_body(version_id), headers=_csrf(client)
        )
    ).json()["id"]
    assert await app.state.validation.wait_for(run_id)
    await app.state.validation.shutdown()
    return version_id


async def test_the_integration_contract_needs_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/ai/integration")).status_code == 401
    assert (await client.get("/v1/ai/integration/decisions")).status_code == 401
    r = await client.post("/v1/ai/integration/strategies/x", json=AI_CONFIG)
    assert r.status_code == 401


async def test_a_plain_user_cannot_configure_a_strategys_ai(signed_in: AsyncClient) -> None:
    r = await signed_in.post(
        "/v1/ai/integration/strategies/rsi_reversion", json=AI_CONFIG, headers=_csrf(signed_in)
    )
    assert r.status_code == 403


async def test_the_contract_route_states_the_pipeline_and_the_risk_authority(
    app: FastAPI, client: AsyncClient
) -> None:
    """§2, §20 and §50, served rather than only documented."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/integration")).json()

    assert body["pipeline"].index("AI STRATEGY FILTER") < body["pipeline"].index("RISK ENGINE")
    assert body["pipeline"].index("RISK ENGINE") < body["pipeline"].index("OMS")
    assert body["pipeline"][-1] == "MT5"
    joined = " ".join(body["does_not"])
    assert "place, modify or cancel an order" in joined
    assert "enable live trading" in joined
    assert "turn a flat bar into a trade" in joined
    assert "absolute and unchanged" in body["risk_authority"]
    assert set(body["modes"]) == {"AI_DISABLED", "AI_ADVISORY", "AI_FILTER", "AI_SCORING"}
    assert "exactly as the deterministic strategy" in body["default"]


async def test_a_strategy_cannot_name_an_unvalidated_model(
    app: FastAPI, client: AsyncClient
) -> None:
    """§12, end to end."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _a_candidate(app, client)  # trained, NOT validated

    r = await client.post(
        "/v1/ai/integration/strategies/rsi_reversion", json=AI_CONFIG, headers=_csrf(client)
    )
    assert r.status_code == 422
    detail = r.json()["error"]["detail"]
    assert "may not be used" in detail
    assert "never been validated" in detail


async def test_a_validated_model_can_be_named_and_round_trips(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _validated_candidate(app, client)

    saved = await client.post(
        "/v1/ai/integration/strategies/rsi_reversion", json=AI_CONFIG, headers=_csrf(client)
    )
    if saved.status_code == 422:
        # The candidate's validation concluded FAIL or BLOCKED on synthetic
        # noise, which is the honest outcome and is itself the §12 guarantee.
        assert "may not be used" in saved.json()["error"]["detail"]
        return

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["mode"] == "AI_FILTER"
    assert body["thresholds"]["minimum_probability"] == 0.6
    assert "grants the AI layer no ability" in body["note"]

    loaded = (await client.get("/v1/ai/integration/strategies/rsi_reversion")).json()
    assert loaded["config"]["mode"] == "AI_FILTER"
    assert loaded["config"]["required_models"][0]["version"] == "1.0"


async def test_an_unconfigured_strategy_is_ai_disabled(app: FastAPI, client: AsyncClient) -> None:
    """§7. The baseline, served as such."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/integration/strategies/never-configured")).json()
    assert body["config"]["mode"] == "AI_DISABLED"
    assert "runs exactly as the deterministic strategy" in body["config"]["notes"]


async def test_an_active_mode_naming_no_model_is_refused(app: FastAPI, client: AsyncClient) -> None:
    """§28. An AI_FILTER with no model would silently be AI_DISABLED."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/ai/integration/strategies/rsi_reversion",
        json={**AI_CONFIG, "required_models": []},
        headers=_csrf(client),
    )
    assert r.status_code == 422
    assert "if the intent is no AI, say AI_DISABLED" in r.json()["error"]["detail"]


async def test_an_ai_threshold_outside_its_range_is_refused_by_the_schema(
    app: FastAPI, client: AsyncClient
) -> None:
    """§39. And §41: there is no field for a path, a formula or code."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    for bad in (
        {"minimum_probability": 1.4},
        {"maximum_latency_ms": 0},
        {"ai_weight": -0.2},
        {"scoring_method": "eval(input())"},
        {"mode": "AI_AUTONOMOUS"},
    ):
        r = await client.post(
            "/v1/ai/integration/strategies/rsi_reversion",
            json={**AI_CONFIG, **bad},
            headers=_csrf(client),
        )
        assert r.status_code == 422, bad


async def test_a_model_reference_cannot_carry_a_path_or_a_payload(
    app: FastAPI, client: AsyncClient
) -> None:
    """§41. The one field where a caller could try to name something else."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.post(
        "/v1/ai/integration/strategies/rsi_reversion",
        json={
            **AI_CONFIG,
            "required_models": [{"key": "../../etc/passwd", "version": {"exec": "x"}}],
        },
        headers=_csrf(client),
    )
    assert r.status_code == 422


async def test_the_decision_journal_is_readable_and_empty_by_default(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/integration/decisions")).json()
    assert body["items"] == []
    assert "risk_vetoed is a common and correct value" in body["note"]


async def test_the_eligible_models_route_shows_why_each_one_is_or_is_not(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _a_candidate(app, client)
    body = (await client.get("/v1/ai/integration/models")).json()
    assert body["items"]
    assert all("reason" in item for item in body["items"])
    assert all(not item["eligible"] for item in body["items"])
    assert "nothing has been established about it" in body["note"]


async def test_there_is_no_route_by_which_ai_reaches_a_venue(app: FastAPI) -> None:
    """§50. No verb under /v1/ai names an execution action."""
    paths = {
        (getattr(route, "path", ""), method)
        for route in api_routes(app)
        for method in getattr(route, "methods", set())
        if getattr(route, "path", "").startswith("/v1/ai")
    }
    assert paths
    for word in ("order", "execute", "place", "trade", "position", "live"):
        assert not any(word in path for path, _ in paths)
    assert not any(m in {"PUT", "DELETE"} for _, m in paths)


async def test_the_ai_router_still_reaches_no_execution_module() -> None:
    """§48 and §50, re-asserted after L27 added the integration routes."""
    import inspect

    from app.api.v1 import ai as router_module

    source = inspect.getsource(router_module)
    for forbidden in ("BrokerAdapter", "OrderManager", "RiskEngine", "SizingRequest", "Approval"):
        assert forbidden not in source, f"the AI router mentions {forbidden}"


# ------------------------------------------------- 17. the model registry (L28)


async def _registered_candidate(app: FastAPI, client: AsyncClient) -> tuple[str, str]:
    """A trained, validated candidate, and the verdict its validation returned."""
    version_id = await _a_candidate(app, client)
    run_id = (
        await client.post(
            "/v1/ai/validation/runs", json=_validation_body(version_id), headers=_csrf(client)
        )
    ).json()["id"]
    assert await app.state.validation.wait_for(run_id)
    await app.state.validation.shutdown()
    verdict = (await client.get(f"/v1/ai/validation/runs/{run_id}")).json()["verdict"]
    return version_id, verdict


async def test_the_registry_routes_need_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/ai/registry")).status_code == 401
    assert (await client.get("/v1/ai/models/trade_probability/versions")).status_code == 401
    r = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/promote", json={"reason": "x"}
    )
    assert r.status_code == 401


async def test_the_registry_contract_states_the_lifecycle_and_its_limits(
    app: FastAPI, client: AsyncClient
) -> None:
    """§10, §12 and §49, served rather than only documented."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    body = (await client.get("/v1/ai/registry")).json()

    assert set(body["statuses"]) == {
        "draft",
        "validated",
        "registered",
        "paper",
        "promoted",
        "rejected",
        "rolled_back",
        "retired",
    }
    # The single edge into `promoted` is §12, and it is visible in the payload
    # rather than only asserted in prose.
    sources = [k for k, v in body["transitions"].items() if "promoted" in v]
    assert sources == ["paper"]
    assert body["serving"] == ["paper", "promoted", "registered"]
    assert set(body["declined"]) == {"CANDIDATE", "VALIDATING", "FAILED", "ACTIVE"}
    joined = " ".join(body["does_not"])
    assert "enable live trading" in joined
    assert "delete a model version" in joined
    assert "no upload route" in body["artifact"]["security"]
    assert body["authorization"]["promote"].endswith("(administrator)")


async def test_a_trader_cannot_promote_rollback_or_retire(
    app: FastAPI, client: AsyncClient
) -> None:
    """§24. Deciding which model a scope resolves to is an administrator's."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    for verb in ("promote", "rollback", "stop", "retire"):
        r = await client.post(
            f"/v1/ai/models/trade_probability/versions/1.0/{verb}",
            json={"reason": "trying it on"},
            headers=_csrf(client),
        )
        assert r.status_code == 403, verb


async def test_a_plain_user_cannot_register_a_model(signed_in: AsyncClient) -> None:
    r = await signed_in.post(
        "/v1/ai/models/trade_probability/versions/1.0/register",
        json={},
        headers=_csrf(signed_in),
    )
    assert r.status_code == 403


async def test_an_unknown_model_or_version_is_a_404(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    assert (await client.get("/v1/ai/models/nope/versions")).status_code == 404
    r = await client.get("/v1/ai/models/nope/versions/1.0")
    assert r.status_code == 404


async def test_registration_through_the_api_is_gated_on_validation(
    app: FastAPI, client: AsyncClient
) -> None:
    """§9, end to end."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _a_candidate(app, client)  # trained, NOT validated

    r = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/register",
        json={},
        headers=_csrf(client),
    )
    assert r.status_code == 422
    assert "never completed a validation run" in r.json()["error"]["detail"]


async def test_the_full_lifecycle_through_the_api(app: FastAPI, client: AsyncClient) -> None:
    """§43. Training -> validation -> register -> paper -> promote -> rollback."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    _, verdict = await _registered_candidate(app, client)

    registered = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/register",
        json={},
        headers=_csrf(client),
    )
    if verdict not in ("PASS", "CONDITIONAL"):
        # Synthetic noise legitimately fails validation, and the registry
        # refusing it IS the §9 guarantee. Nothing further is assertable.
        assert registered.status_code == 422
        assert "validation" in registered.json()["error"]["detail"].lower()
        return

    assert registered.status_code == 200, registered.text
    assert registered.json()["to"] == "registered"

    detail = (await client.get("/v1/ai/models/trade_probability/versions/1.0")).json()
    assert detail["artifact"]["intact"] is True
    assert detail["lineage"]["complete"] is True
    assert detail["lineage"]["validation_verdict"] == verdict
    assert detail["serves_inference"] is True

    deployed = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/deploy",
        json={"environment": "paper", "reason": "first paper run"},
        headers=_csrf(client),
    )
    assert deployed.status_code == 200, deployed.text
    assert deployed.json()["status"] == "active"
    assert "TRADING_MODE and LIVE_TRADING are unchanged" in deployed.json()["note"]

    resolved = (await client.get("/v1/ai/models/trade_probability/resolve")).json()
    assert resolved["resolved"]["version"] == "1.0"
    assert set(resolved["resolved"]["checks"]) == {
        "status",
        "validation",
        "artifact",
        "features",
        "scope",
    }

    promoted = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/promote",
        json={"reason": "two weeks of paper, 140 trades"},
        headers=_csrf(client),
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["to"] == "promoted"
    assert "does NOT enable live trading" in promoted.json()["note"]

    history = (await client.get("/v1/ai/models/trade_probability/history")).json()
    assert [e["to"] for e in history["items"]][:1] == ["promoted"]
    assert all(e["reason"] for e in history["items"])

    rolled = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/rollback",
        json={"reason": "outputs looked wrong"},
        headers=_csrf(client),
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["restored"] is None

    # And the version still exists, with its whole history.
    after = (await client.get("/v1/ai/models/trade_probability/versions/1.0")).json()
    assert after["status"] == "rolled_back"
    assert len(after["history"]) >= 4


async def test_promotion_without_a_paper_deployment_is_refused(
    app: FastAPI, client: AsyncClient
) -> None:
    """§12, through the API."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    _, verdict = await _registered_candidate(app, client)
    r = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/register", json={}, headers=_csrf(client)
    )
    if r.status_code != 200:
        return  # validation did not pass; covered by its own test

    promoted = await client.post(
        "/v1/ai/models/trade_probability/versions/1.0/promote",
        json={"reason": "skip the paper bit"},
        headers=_csrf(client),
    )
    assert promoted.status_code == 422
    assert "no shortcut" in promoted.json()["error"]["detail"]


async def test_a_lifecycle_verb_without_a_reason_is_refused_by_the_schema(
    app: FastAPI, client: AsyncClient
) -> None:
    """§19. An audit entry that does not say why is a timestamp."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    for verb in ("promote", "rollback", "retire"):
        r = await client.post(
            f"/v1/ai/models/trade_probability/versions/1.0/{verb}",
            json={"reason": ""},
            headers=_csrf(client),
        )
        assert r.status_code == 422, verb


async def test_resolution_says_why_nothing_resolves(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    await _a_candidate(app, client)
    body = (await client.get("/v1/ai/models/trade_probability/resolve")).json()
    assert body["resolved"] is None
    assert "no active deployment" in body["reason"]
    assert "no trade, which is the safe reading" in body["note"]


async def test_there_is_no_route_that_deletes_a_model(app: FastAPI) -> None:
    """§22. Nothing is ever physically deleted."""
    methods = {
        (getattr(route, "path", ""), method)
        for route in api_routes(app)
        for method in getattr(route, "methods", set())
        if getattr(route, "path", "").startswith("/v1/ai")
    }
    assert methods
    assert not any(m == "DELETE" for _, m in methods)
    assert not any("delete" in path for path, _ in methods)


async def test_the_registry_never_uploads_an_artifact(app: FastAPI) -> None:
    """§33. There is no upload route, so there is no path to traverse."""
    paths = {getattr(route, "path", "") for route in api_routes(app)}
    for word in ("upload", "artifact/file", "import-model"):
        assert not any(word in path for path in paths)


# ------------------------------- L45 F-1: the bracket opt-in is gated and real


async def _a_bot(app: FastAPI, **over: object) -> str:
    """A bot owned by ALICE, so the route's ownership check passes."""
    from decimal import Decimal as D

    from app.models.bots import Bot

    async with app.state.session_factory() as db:
        owner = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert owner is not None
        fields: dict[str, object] = {
            "id": "bot-bracket",
            "user_id": owner.id,
            "name": "bracket bot",
            "mode": "paper",
            "is_enabled": True,
            "max_risk_per_trade": D("100"),
        }
        fields.update(over)
        db.add(Bot(**fields))
        await db.commit()
    return str(fields["id"])


async def _step_up_bracket(client: AsyncClient, subject: str) -> None:
    r = await client.post(
        "/v1/security/step-up",
        headers=_csrf(client),
        json={
            "password": ALICE["password"],
            "scope": "BOT_BRACKET_SOURCE",
            "subject": subject,
        },
    )
    assert r.status_code == 201, r.text


async def test_the_bracket_opt_in_requires_step_up(app: FastAPI, client: AsyncClient) -> None:
    """**A control that lifts a safety quarantine must cost the password.**

    Turning this on promotes an external sender's suggested stop to the
    platform's own bracket, and under fixed-risk sizing a tighter stop makes a
    LARGER position. A session cookie is not enough for that.
    """
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    bot_id = await _a_bot(app)

    r = await client.post(
        f"/v1/bots/{bot_id}/bracket-source",
        headers=_csrf(client),
        json={"use_alert_bracket": True, "reason": "operator approved for pilot"},
    )
    assert r.status_code in (401, 403), r.text

    async with app.state.session_factory() as db:
        from app.models.bots import Bot

        bot = await db.get(Bot, bot_id)
    assert bot is not None
    assert bot.use_alert_bracket is False, "the opt-in was applied without step-up"


async def test_the_bracket_opt_in_works_for_a_legitimate_operator(
    app: FastAPI, client: AsyncClient
) -> None:
    """The gate must let a real operator through, or it is not a gate but a
    wall — and the control would be unreachable, which is worse than absent."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    bot_id = await _a_bot(app)

    await _step_up_bracket(client, bot_id)
    r = await client.post(
        f"/v1/bots/{bot_id}/bracket-source",
        headers=_csrf(client),
        json={"use_alert_bracket": True, "reason": "operator approved for pilot"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["use_alert_bracket"] is True
    assert r.json()["bracket_source"] == "alert"

    async with app.state.session_factory() as db:
        from app.models.bots import Bot

        bot = await db.get(Bot, bot_id)
    assert bot is not None
    assert bot.use_alert_bracket is True


async def test_turning_the_bracket_opt_in_off_is_gated_too(
    app: FastAPI, client: AsyncClient
) -> None:
    """Both directions, deliberately: otherwise somebody could flip it back and
    forth below the audit trail."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    bot_id = await _a_bot(app, use_alert_bracket=True)

    r = await client.post(
        f"/v1/bots/{bot_id}/bracket-source",
        headers=_csrf(client),
        json={"use_alert_bracket": False, "reason": "reverting after the pilot"},
    )
    assert r.status_code in (401, 403)

    await _step_up_bracket(client, bot_id)
    r = await client.post(
        f"/v1/bots/{bot_id}/bracket-source",
        headers=_csrf(client),
        json={"use_alert_bracket": False, "reason": "reverting after the pilot"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["bracket_source"] == "none"


async def test_the_bracket_change_is_audited_with_its_reason(
    app: FastAPI, client: AsyncClient
) -> None:
    """An operator turning this on must leave a record naming why, and what it
    was before."""
    from app.models.ops import AuditLog

    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    bot_id = await _a_bot(app)

    await _step_up_bracket(client, bot_id)
    r = await client.post(
        f"/v1/bots/{bot_id}/bracket-source",
        headers=_csrf(client),
        json={"use_alert_bracket": True, "reason": "operator approved for pilot"},
    )
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        rows = list((await db.scalars(select(AuditLog))).all())
    entries = [r for r in rows if (r.details or {}).get("action") == "set_bracket_source"]
    assert entries, "the bracket change left no audit record"
    assert (entries[0].details or {})["reason"] == "operator approved for pilot"
    assert (entries[0].details or {})["was"] == "none"
    assert (entries[0].details or {})["now"] == "alert"


async def test_a_bracket_change_needs_a_real_reason(app: FastAPI, client: AsyncClient) -> None:
    """Eight characters, the same floor every other administrative write uses.
    A reason field that accepts "x" is a field nobody fills in honestly."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    bot_id = await _a_bot(app)

    await _step_up_bracket(client, bot_id)
    r = await client.post(
        f"/v1/bots/{bot_id}/bracket-source",
        headers=_csrf(client),
        json={"use_alert_bracket": True, "reason": "x"},
    )
    assert r.status_code == 422


# ============================ L51: registering a venue, the write that was owed


async def _step_up_broker(client: AsyncClient, subject: str) -> None:
    r = await client.post(
        "/v1/security/step-up",
        headers=_csrf(client),
        json={
            "password": ALICE["password"],
            "scope": "BROKER_CREDENTIALS",
            "subject": subject,
        },
    )
    assert r.status_code == 201, r.text


async def test_registering_a_venue_requires_step_up(app: FastAPI, client: AsyncClient) -> None:
    """It creates the path to a venue. A session cookie is not enough."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)

    r = await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "simulator", "reason": "pilot setup"},
    )
    assert r.status_code in (401, 403), r.text
    assert "acct-v" not in app.state.order_managers.managers
    assert "acct-v" not in app.state.brokers.adapters


async def test_registering_a_venue_fills_both_registries(app: FastAPI, client: AsyncClient) -> None:
    """**The point of the route.**

    The execution path reads `OrderManagerRegistry`; the observation routes read
    `BrokerRegistry`. Filling one and not the other would give a platform that
    reports a healthy venue and refuses every signal with `no_venue`, or the
    reverse.
    """
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)

    await _step_up_broker(client, "acct-v")
    r = await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "simulator", "reason": "pilot setup"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["mode"] == "paper"

    assert "acct-v" in app.state.brokers.adapters
    assert "acct-v" in app.state.order_managers.managers
    assert app.state.order_managers.get("acct-v").mode == "paper"


async def test_no_registrable_adapter_reaches_a_live_venue(
    app: FastAPI, client: AsyncClient
) -> None:
    """A real venue needs credentials, and credentials are a seat this route
    does not open. There must be no adapter name that reaches one.

    L70b widened this from "the simulator only" to "the simulator and the MT5
    DEMO terminal". The demo venue needs no credentials stored here -- the
    terminal is already logged in and the adapter reads the account it finds --
    which is exactly why it could be added and a live one still cannot.

    The assertion is on the VALUES, not the keys: an innocently named key that
    mapped to `live` would slip past a check on the keys, and that is the
    mistake worth catching. `tests/test_mt5_demo_venue.py` covers the rest.
    """
    from app.api.v1.brokers import _ADAPTERS

    assert set(_ADAPTERS) == {"simulator", "mt5_demo"}
    assert set(_ADAPTERS.values()) == {"paper", "demo"}
    assert "live" not in _ADAPTERS.values()

    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    await _step_up_broker(client, "acct-v")

    r = await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "mt5", "reason": "pilot setup"},
    )
    assert r.status_code == 422
    assert "acct-v" not in app.state.order_managers.managers


async def test_a_venue_is_never_silently_replaced(app: FastAPI, client: AsyncClient) -> None:
    """An adapter swapped underneath a manager holding orders is a manager
    whose orders belong to a venue it can no longer ask about."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)

    await _step_up_broker(client, "acct-v")
    first = await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "simulator", "reason": "pilot setup"},
    )
    assert first.status_code == 201

    await _step_up_broker(client, "acct-v")
    second = await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "simulator", "reason": "pilot setup"},
    )
    assert second.status_code == 409
    assert "already has a venue" in second.json()["error"]["detail"]


async def test_a_venue_holding_an_unresolved_order_cannot_be_removed(
    app: FastAPI, client: AsyncClient
) -> None:
    """**The guard that matters on the way out.**

    Removing the adapter over an unknown order discards the only thing that can
    reconcile it: the platform keeps the record and loses the ability to ask.
    """
    from datetime import UTC, datetime
    from decimal import Decimal as D

    from app.oms.order import ManagedOrder
    from app.oms.state import OrderStatus

    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    await _step_up_broker(client, "acct-v")
    await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "simulator", "reason": "pilot setup"},
    )

    # An order the venue never settled.
    manager = app.state.order_managers.get("acct-v")
    manager.resume(
        [
            ManagedOrder(
                id="o-unknown",
                client_order_id="intent-unknown",
                account_id="acct-v",
                symbol="EURUSD",
                side="buy",
                quantity=D("0.10"),
                mode="paper",
                created_at=datetime.now(UTC),
                status=OrderStatus.unknown,
            )
        ]
    )

    await _step_up_broker(client, "acct-v")
    r = await client.request(
        "DELETE",
        "/v1/brokers/adapters/acct-v",
        headers=_csrf(client),
        json={"reason": "removing the pilot venue"},
    )
    assert r.status_code == 409, r.text
    assert "never established" in r.json()["error"]["detail"]
    assert "acct-v" in app.state.order_managers.managers


async def test_a_clean_venue_can_be_removed(app: FastAPI, client: AsyncClient) -> None:
    """The guard refuses what is unsafe, not everything."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    await _step_up_broker(client, "acct-v")
    await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "simulator", "reason": "pilot setup"},
    )

    await _step_up_broker(client, "acct-v")
    r = await client.request(
        "DELETE",
        "/v1/brokers/adapters/acct-v",
        headers=_csrf(client),
        json={"reason": "removing the pilot venue"},
    )
    assert r.status_code == 200, r.text
    assert "acct-v" not in app.state.brokers.adapters
    assert "acct-v" not in app.state.order_managers.managers


async def test_registering_a_venue_sends_nothing(app: FastAPI, client: AsyncClient) -> None:
    """Registering a venue means the platform CAN reach one, not that anything
    was sent. `POST /v1/orders` remains the one submission door."""
    from app.models.execution import Order
    from sqlalchemy import func

    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)

    # The DELTA across the registration, not an absolute count: this fixture's
    # database carries rows from other tests and a total would measure them.
    async with app.state.session_factory() as db:
        before = await db.scalar(select(func.count()).select_from(Order))

    await _step_up_broker(client, "acct-v")
    r = await client.post(
        "/v1/brokers/adapters",
        headers=_csrf(client),
        json={"account_id": "acct-v", "adapter": "simulator", "reason": "pilot setup"},
    )
    assert r.status_code == 201, r.text

    async with app.state.session_factory() as db:
        after = await db.scalar(select(func.count()).select_from(Order))
    assert after == before, "registering a venue created an order"
    assert app.state.order_managers.get("acct-v").orders == {}
