"""The TradingView gateway: auth, validation, idempotency, and what it cannot do.

The case that matters most is `test_the_same_alert_twice_creates_one_signal`.
TradingView retries, and a retry that produced a second signal would, once the
OMS exists, produce a second order. Everything else here is in service of that
one not happening.

The other load-bearing case is `test_nothing_in_the_webhook_package_can_trade`.
An alert receiver wired to an execution endpoint is how a research tool becomes
an unattended trading bot, and the absence of that wiring is asserted rather
than assumed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.auth.models import Role, User
from app.brokers.base import SymbolInfo
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.market import Symbol, SymbolMapping
from app.models.signals import Signal, WebhookEvent
from app.models.strategies import Strategy, StrategyVersion
from app.webhooks.gateway import TRADINGVIEW_IPS, Outcome, Unauthorized, WebhookGateway
from app.webhooks.schema import (
    PayloadError,
    check_age,
    parse_action,
    parse_alert,
    parse_timestamp,
    redact,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

# The venue's own contract terms. Required since OrderManager.submit
# began validating against them: FakeBroker.symbols defaults EMPTY, and
# an absent spec is a refusal by design -- giving the simulator a
# built-in default would make it the one path where an unspecced symbol
# passes, which is the fail-open the check removes.
VENUE_SPEC = SymbolInfo(
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

SECRET = "a-long-random-shared-secret-value"
ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}


def alert_body(**overrides: object) -> dict:
    body: dict[str, object] = {
        "secret": SECRET,
        "ticker": "OANDA:EURUSD",
        "action": "BUY",
        "price": "1.10500",
        "time": datetime.now(UTC).isoformat(),
        "timeframe": "60",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- schema


@pytest.mark.parametrize(
    "raw,direction",
    [
        ("BUY", "buy"),
        ("long", "buy"),
        ("SELL", "sell"),
        ("Short", "sell"),
        ("CLOSE", "flat"),
        ("EXIT", "flat"),
        ("ALERT", "flat"),
    ],
)
def test_the_action_vocabulary_normalizes(raw: str, direction: str) -> None:
    alert = parse_alert(alert_body(action=raw))
    assert alert.direction == direction


def test_close_is_flat_not_sell() -> None:
    """CLOSE says 'be out', not 'sell'. Turning one into the other would open
    a short on a flat account."""
    assert parse_alert(alert_body(action="CLOSE")).direction == "flat"


@pytest.mark.parametrize("raw", ["", "  ", "PANIC", "buy now", "DELETE", None, 7])
def test_an_unknown_action_is_rejected_never_interpreted(raw: object) -> None:
    """Arbitrary text must never be read as an executable trading command."""
    with pytest.raises(PayloadError):
        parse_action(raw)


def test_a_missing_ticker_is_refused() -> None:
    body = alert_body()
    del body["ticker"]
    with pytest.raises(PayloadError, match="ticker"):
        parse_alert(body)


def test_a_missing_timestamp_is_refused_not_defaulted_to_now() -> None:
    """Defaulting would make every replayed alert look fresh, which is exactly
    what the age check exists to catch."""
    body = alert_body()
    del body["time"]
    with pytest.raises(PayloadError, match="time is required"):
        parse_alert(body)


@pytest.mark.parametrize(
    "raw", ["2026-09-03T04:00:00Z", "2026-09-03T04:00:00+00:00", "2026-09-03T04:00:00"]
)
def test_timestamps_parse_to_aware_utc(raw: str) -> None:
    parsed = parse_timestamp(raw)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_tradingview_millisecond_epochs_parse() -> None:
    """`{{timenow}}` is milliseconds since the epoch."""
    ms = 1_788_000_000_000
    assert parse_timestamp(ms) == datetime.fromtimestamp(ms / 1000, tz=UTC)


def test_an_unparseable_timestamp_is_refused() -> None:
    with pytest.raises(PayloadError):
        parse_timestamp("last tuesday")


def test_a_stale_alert_is_refused() -> None:
    """The bar it referred to has closed and the price it named is gone."""
    now = datetime.now(UTC)
    with pytest.raises(PayloadError, match="old"):
        check_age(
            now - timedelta(minutes=30), now, max_age_seconds=120, future_tolerance_seconds=30
        )


def test_a_future_stamped_alert_is_refused() -> None:
    now = datetime.now(UTC)
    with pytest.raises(PayloadError, match="future"):
        check_age(now + timedelta(minutes=5), now, max_age_seconds=120, future_tolerance_seconds=30)


def test_advisory_quantity_is_recorded_but_kept_out_of_the_signal_fields() -> None:
    """An alert that could set its own lot size is an alert that can set its
    own risk limit."""
    alert = parse_alert(alert_body(quantity="5", stop_loss="1.09", take_profit="1.12"))
    assert alert.advisory == {"quantity": "5", "stop_loss": "1.09", "take_profit": "1.12"}


def test_the_secret_never_survives_redaction_at_any_depth() -> None:
    payload = {
        "secret": SECRET,
        "nested": {"passphrase": SECRET, "note": f"use {SECRET} here"},
        "list": [{"token": SECRET}, f"inline {SECRET}"],
    }
    cleaned = json.dumps(redact(payload, SECRET))
    assert SECRET not in cleaned
    assert "secret" not in cleaned
    assert "[redacted]" in cleaned


def test_the_fingerprint_ignores_arrival_time_so_a_retry_collapses() -> None:
    """Including the received time would make every retry unique, which is the
    same as having no idempotency at all."""
    body = alert_body(time="2026-09-03T04:00:00Z")
    assert parse_alert(body).fingerprint() == parse_alert(body).fingerprint()


def test_a_new_bar_is_a_different_alert() -> None:
    first = parse_alert(alert_body(time="2026-09-03T04:00:00Z"))
    later = parse_alert(alert_body(time="2026-09-03T05:00:00Z"))
    assert first.fingerprint() != later.fingerprint()


def test_a_sender_supplied_id_wins_over_the_fingerprint() -> None:
    alert = parse_alert(alert_body(id="tv-alert-42"))
    assert alert.idempotency_key() == "tv:id:tv-alert-42"


# --------------------------------------------------------- authentication


def test_an_unconfigured_secret_refuses_everything() -> None:
    """The CLI tool warns and continues, which is right for something an
    operator is watching. A server endpoint doing that would be an open write
    path into the signal table."""
    from app.webhooks.gateway import authenticate

    with pytest.raises(Unauthorized, match="no webhook secret"):
        authenticate({"secret": "anything"}, "{}", "")


def test_a_matching_secret_in_a_field_is_strong() -> None:
    from app.webhooks.gateway import authenticate

    assert authenticate({"secret": SECRET}, "{}", SECRET) == "strong"
    assert authenticate({"passphrase": SECRET}, "{}", SECRET) == "strong"


def test_an_inline_secret_is_accepted_but_marked_weak() -> None:
    """A plain-text alert cannot carry a field. Matched by substring, so it is
    recorded as weaker evidence rather than treated as equal."""
    from app.webhooks.gateway import authenticate

    assert authenticate({"message": f"buy {SECRET}"}, f"buy {SECRET}", SECRET) == "weak"


def test_a_wrong_secret_is_refused() -> None:
    from app.webhooks.gateway import authenticate

    with pytest.raises(Unauthorized):
        authenticate({"secret": "wrong"}, '{"secret":"wrong"}', SECRET)


# ------------------------------------------------------------- app fixture


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
    application.state.webhook_gateway = WebhookGateway(secret=SECRET, mode="paper")
    async with application.state.session_factory() as db:
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        db.add(
            SymbolMapping(
                id="map-tv",
                symbol_id="sym-eur",
                provider="tradingview",
                provider_symbol="OANDA:EURUSD",
            )
        )
        db.add(Strategy(id="strat-1", key="rsi_reversion", name="RSI reversion"))
        db.add(
            StrategyVersion(
                id="sv-1", strategy_id="strat-1", version=1, code_ref="tools.mt5_paper:rsi"
            )
        )
        await db.commit()
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _post(client: AsyncClient, body: dict | str):  # noqa: ANN202
    if isinstance(body, str):
        return await client.post("/v1/webhooks/tradingview", content=body)
    return await client.post("/v1/webhooks/tradingview", json=body)


async def _count(app: FastAPI, model) -> int:  # noqa: ANN001
    async with app.state.session_factory() as db:
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)


# ------------------------------------------------------------ the endpoint


async def test_a_valid_alert_is_accepted_and_recorded(app: FastAPI, client: AsyncClient) -> None:
    r = await _post(client, alert_body())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["signal_id"]
    assert "not a trade" in body["note"]
    assert await _count(app, Signal) == 1


async def test_the_response_never_claims_a_trade(client: AsyncClient) -> None:
    body = (await _post(client, alert_body())).json()
    # No field may carry an execution-shaped value. The note deliberately
    # contains the word "executed" -- in the sentence saying nothing was --
    # so the assertion is about claims, not about substrings.
    assert body["status"] in {"accepted", "duplicate", "rejected"}
    for absent in ("order_id", "ticket", "fill_price", "position_id", "filled_quantity"):
        assert absent not in body
    assert "nothing was executed" in body["note"]


async def test_the_signal_is_stored_new_and_unapproved(app: FastAPI, client: AsyncClient) -> None:
    await _post(client, alert_body())
    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    assert signal.status == "new"
    assert signal.source == "tradingview"
    assert signal.mode == "paper"
    # Nothing here may invent evidence the alert did not carry.
    assert signal.confidence is None


async def test_the_same_alert_twice_creates_one_signal(app: FastAPI, client: AsyncClient) -> None:
    """TradingView retries. A retry that produced a second signal would, once
    the OMS exists, produce a second order."""
    body = alert_body(id="tv-42")
    first = await _post(client, body)
    second = await _post(client, body)
    third = await _post(client, body)
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == third.json()["status"] == "duplicate"
    assert second.json()["signal_id"] == first.json()["signal_id"]
    assert await _count(app, Signal) == 1
    assert await _count(app, WebhookEvent) == 1


async def test_duplicates_collapse_without_a_sender_supplied_id(
    app: FastAPI, client: AsyncClient
) -> None:
    # A fixed stamp would age past the 120s window as the suite runs, so the
    # freshness comes from `now` and the *identity* from the stamp being equal
    # across both posts.
    body = alert_body()
    await _post(client, body)
    await _post(client, body)
    assert await _count(app, Signal) == 1


async def test_a_later_bar_is_a_new_signal(app: FastAPI, client: AsyncClient) -> None:
    await _post(client, alert_body(time=datetime.now(UTC).isoformat()))
    await _post(client, alert_body(time=(datetime.now(UTC) - timedelta(seconds=61)).isoformat()))
    assert await _count(app, Signal) == 2


async def test_a_bad_secret_is_401_and_records_nothing(app: FastAPI, client: AsyncClient) -> None:
    r = await _post(client, alert_body(secret="wrong"))
    assert r.status_code == 401
    assert r.json()["status"] == "unauthorized"
    assert await _count(app, WebhookEvent) == 0
    assert await _count(app, Signal) == 0


async def test_a_missing_secret_is_401(client: AsyncClient) -> None:
    body = alert_body()
    del body["secret"]
    assert (await _post(client, body)).status_code == 401


async def test_an_unauthorized_response_does_not_describe_the_schema(client: AsyncClient) -> None:
    """A caller without the secret learns nothing about what a valid alert
    looks like."""
    body = (await _post(client, alert_body(secret="wrong"))).json()
    assert body["detail"] == "unauthorized"
    assert "ticker" not in json.dumps(body)


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"action": "LIQUIDATE"}, "unsupported action"),
        ({"ticker": ""}, "ticker"),
        ({"time": "not a date"}, "ISO-8601"),
    ],
)
async def test_an_invalid_payload_is_422_and_the_reason_is_recorded(
    app: FastAPI, client: AsyncClient, override: dict, fragment: str
) -> None:
    r = await _post(client, alert_body(**override))
    assert r.status_code == 422
    assert fragment in r.json()["detail"]
    async with app.state.session_factory() as db:
        row = await db.scalar(select(WebhookEvent))
    # Recorded: "never received" and "received and refused" are different
    # answers, and only one means the sender should check its own config.
    assert row is not None and row.status == "rejected"
    assert await _count(app, Signal) == 0


async def test_a_stale_alert_is_refused_over_http(app: FastAPI, client: AsyncClient) -> None:
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    r = await _post(client, alert_body(time=old))
    assert r.status_code == 422
    assert "old" in r.json()["detail"]
    assert await _count(app, Signal) == 0


async def test_an_unmapped_ticker_is_refused_never_guessed(
    app: FastAPI, client: AsyncClient
) -> None:
    """'EURUSD' on TradingView and 'EURUSD' at the broker are two strings that
    happen to look alike."""
    r = await _post(client, alert_body(ticker="BINANCE:NOTMAPPED"))
    assert r.status_code == 422
    assert await _count(app, Signal) == 0


async def test_a_known_strategy_is_mapped_to_a_version(app: FastAPI, client: AsyncClient) -> None:
    await _post(client, alert_body(strategy="rsi_reversion"))
    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    assert signal.strategy_version_id == "sv-1"


async def test_an_unknown_strategy_is_refused(app: FastAPI, client: AsyncClient) -> None:
    """A payload that could select an arbitrary strategy could select a
    privileged one."""
    r = await _post(client, alert_body(strategy="not_registered"))
    assert r.status_code == 422
    assert "unknown strategy" in r.json()["detail"]
    assert await _count(app, Signal) == 0


async def test_an_external_alert_without_a_strategy_is_supported_explicitly(
    app: FastAPI, client: AsyncClient
) -> None:
    await _post(client, alert_body())
    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    assert signal.strategy_version_id is None
    assert signal.source == "tradingview"


async def test_a_plain_text_body_is_handled_without_crashing(client: AsyncClient) -> None:
    """Pine's `alert()` can send a bare string. It fails validation for want of
    a ticker, which is the correct outcome stated rather than guessed around."""
    r = await _post(client, f"buy eurusd {SECRET}")
    assert r.status_code == 422
    assert r.json()["status"] == "rejected"


async def test_an_oversized_body_is_refused(client: AsyncClient) -> None:
    r = await _post(client, json.dumps(alert_body(note="x" * 70_000)))
    assert r.status_code == 413


async def test_the_secret_is_never_stored(app: FastAPI, client: AsyncClient) -> None:
    await _post(client, alert_body())
    async with app.state.session_factory() as db:
        rows = (await db.scalars(select(WebhookEvent))).all()
    stored = json.dumps([r.payload for r in rows])
    assert SECRET not in stored
    assert "secret" not in stored


async def test_the_signal_metadata_keeps_advisory_values_out_of_the_way(
    app: FastAPI, client: AsyncClient
) -> None:
    await _post(client, alert_body(quantity="99", stop_loss="1.00"))
    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    # Recorded under a name that says what it is, so no consumer picks it up
    # by accident.
    assert signal.meta["advisory_ignored"] == {"quantity": "99", "stop_loss": "1.00"}


async def test_an_accepted_alert_publishes_signal_created(
    app: FastAPI, client: AsyncClient
) -> None:
    from app.core.events import InMemoryEventBus
    from app.realtime.hub import Hub

    bus = InMemoryEventBus()
    app.state.hub = Hub(bus)
    await _post(client, alert_body())
    assert [e.type for e in bus.published] == ["SIGNAL_CREATED"]
    published = bus.published[0]
    assert published.payload["status"] == "new"
    assert published.payload["symbol"] == "EURUSD"
    # No fill, no order, no price the platform did not observe.
    assert "fill_price" not in published.payload


async def test_a_publish_failure_still_leaves_the_signal_durable(
    app: FastAPI, client: AsyncClient
) -> None:
    """A lost event costs a live update, not a signal. Reporting failure for a
    stored alert would invite a retry of something that already succeeded."""

    class Broken:
        kind = "broken"

        async def publish(self, event):  # noqa: ANN001, ANN201
            raise ConnectionError("redis is gone")

    from app.realtime.hub import Hub

    app.state.hub = Hub(Broken())  # type: ignore[arg-type]
    r = await _post(client, alert_body())
    assert r.status_code == 200
    assert await _count(app, Signal) == 1


async def test_rate_limiting_protects_the_endpoint(client: AsyncClient) -> None:
    from app.api.v1.webhooks import WEBHOOK_LIMIT

    seen = set()
    for i in range(WEBHOOK_LIMIT.limit + 5):
        r = await _post(client, alert_body(id=f"a{i}"))
        seen.add(r.status_code)
    assert 429 in seen


# --------------------------------------------------------- status route


async def test_status_route_needs_permission_and_never_returns_the_secret(
    app: FastAPI, client: AsyncClient
) -> None:
    assert (await client.get("/v1/webhooks/tradingview")).status_code == 401
    await client.post("/auth/register", json=ALICE)
    assert (await client.get("/v1/webhooks/tradingview")).status_code == 403

    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert user is not None
        user.role = Role.trader.value
        await db.commit()

    r = await client.get("/v1/webhooks/tradingview")
    assert r.status_code == 200
    assert r.json()["secret_configured"] is True
    assert SECRET not in r.text
    assert "not executed" in r.json()["note"]


# -------------------------------------------------------------- safety


async def test_nothing_in_the_webhook_package_can_trade() -> None:
    """An alert receiver wired to an execution endpoint is how a research tool
    becomes an unattended trading bot."""
    import importlib
    import pkgutil

    import app.webhooks as pkg

    forbidden = {
        "MetaTrader5",
        "mt5_paper",
        "order_send",
        "place_order",
        "BrokerAdapter",
        "OrderRequest",
        "RiskEngine",
    }
    for module in pkgutil.walk_packages(pkg.__path__, prefix="app.webhooks."):
        loaded = importlib.import_module(module.name)
        assert not (set(dir(loaded)) & forbidden), f"{module.name} reaches execution"


async def test_an_accepted_alert_creates_no_order_and_no_position(
    app: FastAPI, client: AsyncClient
) -> None:
    from app.models.execution import Order, Position

    await _post(client, alert_body())
    assert await _count(app, Order) == 0
    assert await _count(app, Position) == 0


def test_the_published_tradingview_addresses_are_recorded() -> None:
    assert len(TRADINGVIEW_IPS) == 4


def test_every_outcome_is_a_webhook_status_not_a_trading_one() -> None:
    """There is no value `status` can take that describes a trade."""
    assert {str(o) for o in Outcome} == {"accepted", "duplicate", "rejected", "unauthorized"}


# ------------------------------------------------- L45 F-1: routing in the meta


async def _enable_bot(app: FastAPI, **over: object) -> None:
    """Give the platform a bot that runs `rsi_reversion` on a paper account."""
    from decimal import Decimal as D

    from app.auth.models import Role, User
    from app.models.accounts import PaperAccount
    from app.models.bots import Bot

    async with app.state.session_factory() as db:
        db.add(User(id="u1", email="a@b.io", password_hash="x", role=str(Role.admin)))
        db.add(
            PaperAccount(
                id="acct-a",
                user_id="u1",
                name="paper",
                currency="USD",
                starting_balance=D("100000"),
                balance=D("100000"),
                equity=D("100000"),
            )
        )
        await db.flush()
        base: dict[str, object] = {
            "id": "bot-1",
            "user_id": "u1",
            "name": "tv bot",
            "mode": "paper",
            "strategy_version_id": "sv-1",
            "paper_account_id": "acct-a",
            "is_enabled": True,
            "is_disabled": False,
            "max_risk_per_trade": D("100"),
        }
        base.update(over)
        db.add(Bot(**base))
        await db.commit()


async def test_a_routed_alert_records_everything_the_worker_needs(
    app: FastAPI, client: AsyncClient
) -> None:
    """**The L45 F-1 regression, through the real HTTP gateway.**

    `_to_incoming_signal` reads seven keys from `meta`. Five of them were never
    written, so it returned None for every alert this platform had ever
    received and the worker retired each one as `signal_not_executable` —
    which is why all 118 signals on the deployed database sit at `new`.
    """
    from app.main import _to_incoming_signal

    await _enable_bot(app)
    r = await _post(client, alert_body(strategy="rsi_reversion"))
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None

    meta = signal.meta or {}
    # The platform's own symbol code, not the alert's ticker: the pipeline
    # needs "EURUSD" to find a contract spec, and "OANDA:EURUSD" finds none.
    assert meta["internal_symbol"] == "EURUSD"
    # The strategy KEY, which is what `strategy_state()` is keyed by.
    assert meta["strategy_id"] == "rsi_reversion"
    # Routed from the `bots` row, never from the payload.
    assert meta["account_id"] == "acct-a"
    assert meta["bot_id"] == "bot-1"
    # Compared as a Decimal: the `Money` column round-trips as "100.0000",
    # and the string form is the database's business rather than a contract.
    assert Decimal(meta["risk_amount"]) == Decimal("100")

    # And the translation the worker performs now succeeds, which is the whole
    # of F-1: before this it returned None for every alert.
    incoming = _to_incoming_signal(signal)
    assert incoming is not None
    assert incoming.account_id == "acct-a"
    assert incoming.symbol == "EURUSD"
    assert incoming.risk_amount is not None


async def test_an_unroutable_alert_is_recorded_with_the_reason(
    app: FastAPI, client: AsyncClient
) -> None:
    """No bot is not an error on the intake path — it is the normal state of a
    platform whose bots are switched off. What changed is that the reason is
    written down beside the signal instead of being rediscovered later from an
    empty `orders` table."""
    from app.main import _to_incoming_signal

    r = await _post(client, alert_body(strategy="rsi_reversion"))
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    assert "no enabled bot" in (signal.meta or {})["not_executable"]
    assert "account_id" not in (signal.meta or {})
    assert _to_incoming_signal(signal) is None


async def test_an_alert_cannot_choose_its_own_account(app: FastAPI, client: AsyncClient) -> None:
    """A payload that could name an account could name somebody else's. The
    routing comes from `bots`; anything the sender writes lands in `extra`,
    which nothing executes from."""
    await _enable_bot(app)
    r = await _post(
        client,
        alert_body(strategy="rsi_reversion", account_id="somebody-else", risk_amount="999999"),
    )
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    meta = signal.meta or {}
    assert meta["account_id"] == "acct-a"
    assert Decimal(meta["risk_amount"]) == Decimal("100")
    assert meta["extra"]["account_id"] == "somebody-else"
    assert meta["extra"]["risk_amount"] == "999999"


async def test_two_enabled_bots_leave_the_alert_unroutable(
    app: FastAPI, client: AsyncClient
) -> None:
    """Ambiguity is refused, never resolved: choosing would send an order to an
    account nobody selected."""
    await _enable_bot(app)
    async with app.state.session_factory() as db:
        from app.models.bots import Bot

        db.add(
            Bot(
                id="bot-2",
                user_id="u1",
                name="second",
                mode="paper",
                strategy_version_id="sv-1",
                paper_account_id="acct-a",
                is_enabled=True,
                is_disabled=False,
            )
        )
        await db.commit()

    r = await _post(client, alert_body(strategy="rsi_reversion"))
    assert r.status_code == 200, r.text
    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    assert "refusing to choose one" in (signal.meta or {})["not_executable"]


# ------------------------------------- L45 F-1: the bracket, and its opt-in


async def test_without_the_opt_in_no_bracket_is_promoted(app: FastAPI, client: AsyncClient) -> None:
    """**Default behaviour, and it must not change.**

    The stop an alert suggests stays quarantined under `advisory_ignored`. The
    signal is routed, recorded and executable in every other respect, and
    sizing will refuse it for the one honest reason — the platform has no
    bracket.
    """
    await _enable_bot(app)
    r = await _post(client, alert_body(strategy="rsi_reversion", sl="1.09500", tp="1.11000"))
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    meta = signal.meta or {}

    assert meta["bracket_source"] == "none"
    assert "stop_loss" not in meta
    assert "take_profit" not in meta
    # The suggestion is still recorded, exactly where it was.
    assert meta["advisory_ignored"]["sl"] == "1.09500"


async def test_an_opted_in_bot_promotes_the_alerts_bracket(
    app: FastAPI, client: AsyncClient
) -> None:
    """The opt-in, per bot, recorded.

    Under `fixed_risk` a tighter stop makes a LARGER position, so this hands an
    external sender an input that moves size upward. That is why it is a
    decision rather than a default, and why `bracket_source` is written.
    """
    from app.main import _to_incoming_signal

    await _enable_bot(app, use_alert_bracket=True)
    r = await _post(client, alert_body(strategy="rsi_reversion", sl="1.09500", tp="1.11000"))
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    meta = signal.meta or {}

    assert meta["bracket_source"] == "alert"
    assert meta["stop_loss"] == "1.09500"
    assert meta["take_profit"] == "1.11000"
    # And BOTH remain readable: what was suggested and what was used.
    assert meta["advisory_ignored"]["sl"] == "1.09500"

    incoming = _to_incoming_signal(signal)
    assert incoming is not None
    assert incoming.stop_loss == Decimal("1.09500")
    assert incoming.take_profit == Decimal("1.11000")


async def test_an_opted_in_bot_with_no_stop_in_the_alert_says_so(
    app: FastAPI, client: AsyncClient
) -> None:
    """Opting in does not conjure a bracket the alert never carried."""
    await _enable_bot(app, use_alert_bracket=True)
    r = await _post(client, alert_body(strategy="rsi_reversion"))
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    meta = signal.meta or {}
    assert meta["bracket_source"] == "alert"
    assert "stop_loss" not in meta
    assert "carried no stop" in meta["bracket_detail"]


async def test_the_bracket_decision_is_recorded_on_every_routed_signal(
    app: FastAPI, client: AsyncClient
) -> None:
    """`bracket_source` is written even when there is no bracket, so "where did
    this order's stop come from" has an answer on the record rather than being
    reconstructed from which keys happen to be present."""
    await _enable_bot(app)
    await _post(client, alert_body(strategy="rsi_reversion"))
    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None
    assert "bracket_source" in (signal.meta or {})
    assert "bracket_detail" in (signal.meta or {})


async def test_an_alert_becomes_an_order_through_the_deployed_wiring(
    app: FastAPI, client: AsyncClient
) -> None:
    """**The headline flow, end to end, for the first time.**

    TradingView -> gateway -> `signals` row -> the worker's own translation ->
    the application's OWN `ExecutionPipeline` -> Risk -> Sizing -> OMS ->
    adapter -> a filled order.

    Everything here is the deployed object: `app.state.execution_pipeline` is
    the pipeline `create_app` built, with the store, the spec loader and the
    strategy state it was given. Only two things are supplied that a real
    deployment must also supply and this one does not have: a broker adapter
    registered against the account, and an `mt5` contract spec for the symbol.

    Before L45 F-1 this could not be written. `_to_incoming_signal` returned
    None for every alert, because the gateway wrote none of the five keys it
    reads — which is why all 118 signals on the deployed database sit at `new`.
    """
    from decimal import Decimal as D

    from app.brokers.fake import FakeBroker
    from app.main import _to_incoming_signal
    from app.models.market import SymbolMapping

    await _enable_bot(app, use_alert_bracket=True)

    # The broker contract terms, which a deployment syncs from the terminal.
    async with app.state.session_factory() as db:
        db.add(
            SymbolMapping(
                id="map-mt5",
                symbol_id="sym-eur",
                provider="mt5",
                provider_symbol="EURUSD",
                contract_size=D("100000"),
                tick_size=D("0.00001"),
                tick_value=D("1"),
                minimum_volume=D("0.01"),
                maximum_volume=D("100"),
                volume_step=D("0.01"),
                price_precision=5,
                volume_precision=2,
            )
        )
        await db.commit()

    venue = FakeBroker(mode="paper")
    await venue.connect()
    venue.set_quote("EURUSD", "1.10000", "1.10002")
    venue.symbols["EURUSD"] = VENUE_SPEC
    app.state.order_managers.register("acct-a", venue, mode="paper", broker="fake")

    r = await _post(client, alert_body(strategy="rsi_reversion", sl="1.09500", tp="1.11000"))
    assert r.status_code == 200, r.text

    async with app.state.session_factory() as db:
        signal = await db.scalar(select(Signal))
    assert signal is not None

    incoming = _to_incoming_signal(signal)
    assert incoming is not None, "the worker could not translate a routed signal"

    result = await app.state.execution_pipeline.process(incoming)
    assert result.outcome.value in ("filled", "order_submitted"), result.detail
    assert result.created_order

    # And the order is DURABLE — the L45 C-1 property, on the automated path.
    async with app.state.session_factory() as db:
        from app.models.execution import Order

        orders = list((await db.scalars(select(Order))).all())
    assert len(orders) == 1
    assert orders[0].intent_id == signal.signal_key
    assert orders[0].paper_account_id == "acct-a"
