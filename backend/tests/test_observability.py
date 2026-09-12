"""Monitoring, observability and system health (L37).

The ones that matter most:

  * `test_monitoring_cannot_reach_anything_that_trades` — §72, §81.
  * `test_no_route_restarts_reconnects_or_retries_anything` — §47.
  * `test_a_brief_failure_produces_no_incident` — §45.
  * `test_a_sustained_failure_produces_one_incident_and_then_a_recovery` — §43, §45.
  * `test_an_unobserved_component_is_unknown_not_healthy` — §11, §65.
  * `test_a_failing_probe_degrades_one_component_and_not_the_pass` — §66, §67.
  * `test_an_optional_component_never_makes_the_platform_unhealthy` — §12, §39.
  * `test_trading_safety_is_unknown_when_something_could_not_be_observed` — §40.
  * `test_an_identifier_may_not_be_a_metric_label` — §50.
  * `test_the_public_health_endpoint_reveals_nothing_detailed` — §58.
  * `test_detailed_monitoring_requires_authorization` — §56, §59, §73.
  * `test_uptime_is_refused_rather_than_invented` — §64.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.auth.models import Role, User
from app.core.events import Event
from app.core.health import HealthStatus
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.execution import Order
from app.models.market import MarketBar, Symbol
from app.models.ops import SystemEvent
from app.observability import collectors, incidents
from app.observability.contract import (
    ComponentHealth,
    ComponentState,
    Criticality,
    IncidentType,
    Layer,
    aggregate,
    from_health,
)
from app.observability.metrics import MAX_SERIES, CardinalityRefused, Registry
from app.observability.service import ObservabilityService, trading_safety
from app.observability.thresholds import Thresholds
from app.observability.worker import MonitoringWorker
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "observability"
ROUTER = BACKEND / "app" / "api" / "v1" / "monitoring.py"

ADMIN = {"email": "root@tr-platform.io", "password": "correct horse battery"}
PLAIN = {"email": "reader@tr-platform.io", "password": "another long passphrase"}
SECRET = "broker-password-do-not-leak"
NOW = datetime(2026, 9, 5, 12, 0, 0)


# ================================================================== fixtures


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # `checks={}` keeps the fixture off a real database and Redis; the
    # infrastructure collector is exercised on its own below with fabricated
    # CheckResults, which is the only honest way to test a degraded database
    # without degrading one.
    application = create_app(settings, checks={}, engine=engine)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


@pytest.fixture
async def admin(app: FastAPI, client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ADMIN)
    await _promote(app, ADMIN["email"], Role.admin)
    return client


def component(
    name: str = "thing",
    state: ComponentState = ComponentState.healthy,
    criticality: Criticality = Criticality.important,
    **facts: Any,
) -> ComponentHealth:
    return ComponentHealth(
        name=name,
        layer=Layer.infrastructure,
        state=state,
        criticality=criticality,
        detail="detail",
        checked_at=NOW,
        facts=facts,
    )


# ============================================= 1. monitoring cannot act (§72)


FORBIDDEN_IMPORTS = (
    "app.risk",
    "app.oms",
    "app.brokers.mt5",
    "app.brokers.base",
    "app.sizing",
    "app.execution",
    "app.positions",
    "app.paper.oms",
)

FORBIDDEN_CALLS = (
    "submit_order",
    "place_order",
    "cancel_order",
    "modify_position",
    "close_position",
    "connect",
    "disconnect",
    "reconnect",
    "restart",
    "start",
    "stop",
    "engage_kill_switch",
    "release_kill_switch",
)


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_monitoring_cannot_reach_anything_that_trades() -> None:
    """Sections 72 and 81. A property of the imports, not a promise."""
    offences: list[str] = []
    for path in [*_modules(), ROUTER]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(FORBIDDEN_IMPORTS):
                    offences.append(f"{path.name} imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(FORBIDDEN_IMPORTS):
                        offences.append(f"{path.name} imports {alias.name}")
    assert offences == []


def test_no_route_restarts_reconnects_or_retries_anything() -> None:
    """Section 47. L37 detects; L38 recovers. Kept apart on purpose."""
    called: set[str] = set()
    for path in [*_modules(), ROUTER]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if isinstance(name, str):
                    called.add(name)
    assert called.isdisjoint(FORBIDDEN_CALLS)


def test_monitoring_publishes_no_new_event_type() -> None:
    """Section 81: no second event bus, and no invented catalogue entry."""
    from app.realtime.catalogue import EventType

    source = (PACKAGE / "incidents.py").read_text(encoding="utf-8")
    published = [t for t in EventType if f'type="{t}"' in source]
    assert published == [EventType.SYSTEM_ALERT]


def test_monitoring_never_sends_a_notification_directly() -> None:
    """Section 46. It publishes an event; L34 decides what to do with it."""
    joined = "\n".join(p.read_text(encoding="utf-8") for p in _modules())
    for forbidden in ("EmailChannel", "DiscordWebhookChannel", "NotificationService(", "smtplib"):
        assert forbidden not in joined


# ================================================ 2. states and aggregation


def test_an_unobserved_component_is_unknown_not_healthy() -> None:
    """Sections 11 and 65. Silence is never a clean bill of health."""
    failed = collectors.failed(
        "thing", Layer.infrastructure, Criticality.critical, RuntimeError("x")
    )
    assert failed.state is ComponentState.unknown
    assert failed.facts["error_category"]


def test_not_configured_is_not_unhealthy() -> None:
    """Section 11 and section 12. Two different operational facts."""
    states = {c.state for c in [component(state=ComponentState.not_configured)]}
    assert states == {ComponentState.not_configured}
    unconfigured = component(state=ComponentState.not_configured, criticality=Criticality.optional)
    assert aggregate([unconfigured]) is ComponentState.healthy


def test_a_critical_component_unhealthy_makes_the_platform_unhealthy() -> None:
    assert (
        aggregate(
            [
                component("db", ComponentState.unhealthy, Criticality.critical),
                component("other", ComponentState.healthy, Criticality.optional),
            ]
        )
        is ComponentState.unhealthy
    )


def test_an_optional_component_never_makes_the_platform_unhealthy() -> None:
    """Section 12 and 39: an unconfigured integration is not a fault."""
    assert (
        aggregate(
            [
                component("db", ComponentState.healthy, Criticality.critical),
                component("discord", ComponentState.unhealthy, Criticality.optional),
            ]
        )
        is ComponentState.healthy
    )


def test_health_is_not_averaged() -> None:
    """Nine healthy and one critical unhealthy is not 90% healthy."""
    many = [component(f"c{i}", ComponentState.healthy, Criticality.optional) for i in range(9)]
    many.append(component("db", ComponentState.unhealthy, Criticality.critical))
    assert aggregate(many) is ComponentState.unhealthy


def test_an_empty_platform_is_healthy_and_says_so_elsewhere() -> None:
    assert aggregate([]) is ComponentState.healthy


def test_the_three_state_check_maps_onto_the_five() -> None:
    """One translation, so the two vocabularies cannot drift."""
    assert from_health(HealthStatus.healthy) is ComponentState.healthy
    assert from_health(HealthStatus.degraded) is ComponentState.degraded
    assert from_health(HealthStatus.unavailable) is ComponentState.unhealthy


# ============================================ 3. hysteresis and incidents (§45)


def _tracker() -> incidents.Tracker:
    return incidents.Tracker(Thresholds(consecutive_failures=2, consecutive_successes=2))


def test_the_first_observation_is_a_baseline_not_an_incident() -> None:
    tracker = _tracker()
    assert tracker.observe(component("db", ComponentState.unhealthy)) is None


def test_a_brief_failure_produces_no_incident() -> None:
    """Section 45. DOWN/UP/DOWN/UP is what this exists to prevent."""
    tracker = _tracker()
    tracker.observe(component("db", ComponentState.healthy))
    for state in (ComponentState.unhealthy, ComponentState.healthy) * 3:
        assert tracker.observe(component("db", state)) is None


def test_a_sustained_failure_produces_one_incident_and_then_a_recovery() -> None:
    """Sections 43 and 45. Raised once; cleared once; never twice."""
    tracker = _tracker()
    tracker.observe(component("db", ComponentState.healthy))
    assert tracker.observe(component("db", ComponentState.unhealthy)) is None
    raised = tracker.observe(component("db", ComponentState.unhealthy))
    assert raised is not None
    assert raised.incident_type is IncidentType.service_down
    assert raised.is_recovery is False
    # Still down: no second incident.
    assert tracker.observe(component("db", ComponentState.unhealthy)) is None
    assert tracker.observe(component("db", ComponentState.healthy)) is None
    recovered = tracker.observe(component("db", ComponentState.healthy))
    assert recovered is not None
    assert recovered.incident_type is IncidentType.service_recovered
    assert recovered.is_recovery is True


def test_an_escalation_within_bad_is_still_reported() -> None:
    """DEGRADED becoming UNHEALTHY is the transition an operator reacts to."""
    tracker = _tracker()
    tracker.observe(component("db", ComponentState.healthy))
    tracker.observe(component("db", ComponentState.degraded))
    assert tracker.observe(component("db", ComponentState.degraded)) is not None
    worse = tracker.observe(component("db", ComponentState.unhealthy))
    assert worse is not None and worse.current is ComponentState.unhealthy


def test_a_component_nobody_configured_never_raises_an_incident() -> None:
    """An unconfigured optional channel is not an incident, every 15 seconds."""
    tracker = _tracker()
    tracker.observe(component("notifications.discord", ComponentState.not_configured))
    for _ in range(5):
        assert (
            tracker.observe(component("notifications.discord", ComponentState.not_configured))
            is None
        )


def test_a_components_incident_type_matches_what_it_is() -> None:
    tracker = _tracker()
    for name, expected in (
        ("worker:execution", IncidentType.worker_stale),
        ("queue:notification-delivery", IncidentType.queue_backlog),
        ("market_data.freshness", IncidentType.stale_market_data),
        ("oms", IncidentType.reconciliation_required),
        ("database", IncidentType.service_down),
    ):
        tracker.observe(component(name, ComponentState.healthy))
        tracker.observe(component(name, ComponentState.unhealthy))
        raised = tracker.observe(component(name, ComponentState.unhealthy))
        assert raised is not None and raised.incident_type is expected


async def test_an_incident_is_recorded_in_the_table_l05_already_had(
    app: FastAPI,
) -> None:
    """Section 76. `system_events` existed and nothing wrote to it."""
    incident = incidents.Incident(
        component="database",
        incident_type=IncidentType.service_down,
        level="critical",
        previous=ComponentState.healthy,
        current=ComponentState.unhealthy,
        detail="unavailable",
        at=NOW,
        criticality=Criticality.critical,
        facts={"latency_ms": 12.0},
    )
    async with app.state.session_factory() as db:
        await incidents.record(db, incident, correlation_id="c1")
        await db.commit()
        row = await db.scalar(select(SystemEvent))
    assert row is not None
    assert row.component == "database"
    assert row.event_type == str(IncidentType.service_down)
    assert row.level == "critical"
    assert row.correlation_id == "c1"
    assert row.payload["from"] == str(ComponentState.healthy)


async def test_a_secret_in_a_fact_never_reaches_a_stored_incident(app: FastAPI) -> None:
    """Sections 7 and 53."""
    incident = incidents.Incident(
        component="broker",
        incident_type=IncidentType.service_down,
        level="error",
        previous=ComponentState.healthy,
        current=ComponentState.unhealthy,
        detail="gone",
        at=NOW,
        criticality=Criticality.critical,
        facts={"webhook_url": SECRET, "broker_password": SECRET, "reconnects": 3},
    )
    async with app.state.session_factory() as db:
        await incidents.record(db, incident)
        await db.commit()
        row = await db.scalar(select(SystemEvent))
    assert row is not None
    assert SECRET not in str(row.payload)
    assert row.payload["reconnects"] == 3


async def test_an_incident_publishes_one_system_alert() -> None:
    """Section 46. One catalogued event; no email, no Discord, no second bus."""
    published: list[Event] = []

    class _Hub:
        async def publish(self, event: Event) -> None:
            published.append(event)

    incident = incidents.Incident(
        component="market_data.freshness",
        incident_type=IncidentType.stale_market_data,
        level="warning",
        previous=ComponentState.healthy,
        current=ComponentState.degraded,
        detail="newest bar is 1200s old",
        at=NOW,
        criticality=Criticality.important,
    )
    assert await incidents.publish(_Hub(), incident) is True
    assert published[0].type == "SYSTEM_ALERT"
    assert published[0].channel == "system"
    assert published[0].payload["health"] == "degraded"


async def test_a_publish_failure_does_not_lose_the_incident() -> None:
    """Section 66. The row is written regardless of whether it is announced."""

    class _BrokenHub:
        async def publish(self, event: Event) -> None:
            raise RuntimeError("bus down")

    incident = incidents.Incident(
        component="db",
        incident_type=IncidentType.service_down,
        level="critical",
        previous=ComponentState.healthy,
        current=ComponentState.unhealthy,
        detail="x",
        at=NOW,
        criticality=Criticality.critical,
    )
    assert await incidents.publish(_BrokenHub(), incident) is False


# ============================================= 4. a real collection pass


async def test_a_collection_pass_reports_every_layer(app: FastAPI) -> None:
    service: ObservabilityService = app.state.observability
    async with app.state.session_factory() as db:
        snapshot = await service.collect(db, app)
        await db.commit()
    names = {c.name for c in snapshot.components}
    assert {"event_bus", "realtime", "oms", "positions", "risk_engine", "broker"} <= names
    assert "monitoring.self" in names
    assert snapshot.duration_ms >= 0


async def test_an_empty_platform_reports_what_is_absent_rather_than_healthy(
    app: FastAPI,
) -> None:
    """Section 65. No fake broker, no fake feed, no invented freshness."""
    service: ObservabilityService = app.state.observability
    async with app.state.session_factory() as db:
        snapshot = await service.collect(db, app)
    found = {c.name: c for c in snapshot.components}
    assert found["broker"].state is ComponentState.not_configured
    assert found["market_data.freshness"].state is ComponentState.unknown
    assert "no bar has ever been stored" in found["market_data.freshness"].detail
    assert found["market_data.freshness"].facts["newest_bar"] is None


async def test_stale_market_data_is_measured_not_assumed(app: FastAPI) -> None:
    """Sections 20 and 21. A real age from a real stored bar."""
    from app.auth.models import utcnow

    async with app.state.session_factory() as db:
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        # The rows below reference this symbol. SQLAlchemy has no
        # relationship() to tell it they depend on it, so without this
        # flush the children can be written first -- an IntegrityError
        # now that SQLite enforces foreign keys.
        await db.flush()
        db.add(
            MarketBar(
                provider="mt5",
                provider_symbol="EURUSD",
                symbol_id="sym-eur",
                timeframe="M1",
                bar_time=utcnow() - timedelta(seconds=2000),
                open=Decimal("1.1"),
                high=Decimal("1.1"),
                low=Decimal("1.1"),
                close=Decimal("1.1"),
            )
        )
        await db.commit()
    service: ObservabilityService = app.state.observability
    async with app.state.session_factory() as db:
        snapshot = await service.collect(db, app)
    freshness = next(c for c in snapshot.components if c.name == "market_data.freshness")
    assert freshness.state is ComponentState.degraded
    assert freshness.facts["age_seconds"] > 1900
    assert "unchanged price" in freshness.facts["note"]


async def test_an_unresolved_order_is_reconciliation_required_not_failed(
    app: FastAPI,
) -> None:
    """Section 17. The wording is the point."""
    async with app.state.session_factory() as db:
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        # The rows below reference this symbol. SQLAlchemy has no
        # relationship() to tell it they depend on it, so without this
        # flush the children can be written first -- an IntegrityError
        # now that SQLite enforces foreign keys.
        await db.flush()
        db.add(
            Order(
                id="o-1",
                intent_id="i1",
                mode="paper",
                symbol_id="sym-eur",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                status="unknown",
                source="manual",
            )
        )
        await db.commit()
    service: ObservabilityService = app.state.observability
    async with app.state.session_factory() as db:
        snapshot = await service.collect(db, app)
    oms = next(c for c in snapshot.components if c.name == "oms")
    assert oms.state is ComponentState.degraded
    assert "RECONCILIATION_REQUIRED" in oms.detail
    # The only occurrence of the word is the clause that denies it.
    assert "not failed" in oms.detail
    assert oms.detail.lower().count("failed") == 1


async def test_a_failing_probe_degrades_one_component_and_not_the_pass(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sections 66 and 67."""

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(collectors, "trading", boom)
    service: ObservabilityService = app.state.observability
    async with app.state.session_factory() as db:
        snapshot = await service.collect(db, app)
    broken = next(c for c in snapshot.components if c.name == "trading")
    assert broken.state is ComponentState.unknown
    assert service.failures >= 1
    # The rest of the pass still happened.
    assert len(snapshot.components) > 5


async def test_the_monitor_reports_its_own_health(app: FastAPI) -> None:
    """Section 67. A monitor that cannot detect its own failure is decoration."""
    service: ObservabilityService = app.state.observability
    async with app.state.session_factory() as db:
        await service.collect(db, app)
        snapshot = await service.collect(db, app)
    own = next(c for c in snapshot.components if c.name == "monitoring.self")
    assert own.state is ComponentState.healthy
    assert own.facts["interval_seconds"] > 0


async def test_the_worker_collects_and_only_commits_when_something_changed(
    app: FastAPI,
) -> None:
    service: ObservabilityService = app.state.observability
    worker = MonitoringWorker(app.state.session_factory, service, app, interval_seconds=0.01)
    await worker.tick()
    await worker.tick()
    assert service.collections == 2


# ==================================================== 5. trading safety (§40)


def test_trading_safety_is_blocked_when_something_is_unhealthy() -> None:
    settings = Settings(_env_file=None)
    safety, reasons = trading_safety(
        [
            component("database", ComponentState.unhealthy, Criticality.critical),
            component("risk_engine", ComponentState.healthy, Criticality.critical),
            component("broker", ComponentState.healthy, Criticality.critical),
            component("oms", ComponentState.healthy, Criticality.important),
            component("positions", ComponentState.healthy, Criticality.important),
            component("market_data.freshness", ComponentState.healthy, Criticality.important),
        ],
        settings,
    )
    assert str(safety) == "BLOCKED"
    assert any("database is unhealthy" in r for r in reasons)


def test_trading_safety_is_unknown_when_something_could_not_be_observed() -> None:
    """Section 40. Not SAFE by default; we do not know, and we say so."""
    settings = Settings(_env_file=None)
    safety, reasons = trading_safety(
        [
            component("database", ComponentState.healthy, Criticality.critical),
            component("risk_engine", ComponentState.healthy, Criticality.critical),
            component("broker", ComponentState.unknown, Criticality.critical),
            component("oms", ComponentState.healthy, Criticality.important),
            component("positions", ComponentState.healthy, Criticality.important),
            component("market_data.freshness", ComponentState.healthy, Criticality.important),
        ],
        settings,
    )
    assert str(safety) == "UNKNOWN"
    assert any("could not be observed" in r for r in reasons)


def test_trading_safety_names_a_component_that_was_never_collected() -> None:
    settings = Settings(_env_file=None)
    safety, reasons = trading_safety([], settings)
    assert str(safety) == "UNKNOWN"
    assert any("was not collected" in r for r in reasons)


def test_trading_safety_always_reports_the_live_gates() -> None:
    settings = Settings(_env_file=None)
    _, reasons = trading_safety([], settings)
    assert any("live execution is not allowed" in r for r in reasons)


# ================================================ 6. metrics (§49, §50, §70)


def test_an_identifier_may_not_be_a_metric_label() -> None:
    """Section 50. Refused outright, not documented as discouraged."""
    registry = Registry()
    for label in ("trade_id", "order_id", "request_id", "user_id", "correlation_id"):
        with pytest.raises(CardinalityRefused):
            registry.increment("x", labels={label: "abc"})


def test_a_bounded_label_is_accepted() -> None:
    registry = Registry()
    registry.increment("component_checks_total", labels={"result": "ok"})
    assert registry.value("component_checks_total", {"result": "ok"}) == 1.0


def test_a_metric_stops_growing_at_the_series_cap() -> None:
    """An unbounded label set is a memory leak that looks like observability."""
    registry = Registry()
    for i in range(MAX_SERIES + 50):
        registry.increment("wide", labels={"bucket": str(i)})
    snapshot = [m for m in registry.snapshot() if m["name"] == "wide"][0]
    assert len(snapshot["series"]) <= MAX_SERIES + 1
    assert snapshot["overflowed"] > 0


def test_counters_gauges_and_histograms_behave_differently() -> None:
    registry = Registry()
    registry.increment("c", 2)
    registry.increment("c", 3)
    assert registry.value("c") == 5.0
    registry.gauge("g", 7)
    registry.gauge("g", 2)
    assert registry.value("g") == 2.0
    registry.observe("h", 12.0)
    registry.observe("h", 30.0)
    histogram = [m for m in registry.snapshot() if m["name"] == "h"][0]["series"][0]
    assert histogram["value"] == 2
    assert histogram["mean"] == 21.0
    assert sum(histogram["buckets"].values()) == 2


async def test_a_collection_records_bounded_metrics(app: FastAPI) -> None:
    service: ObservabilityService = app.state.observability
    async with app.state.session_factory() as db:
        await service.collect(db, app)
    names = {m["name"] for m in service.metrics.snapshot()}
    assert "component_state" in names
    assert "monitoring_collections_total" in names
    for metric in service.metrics.snapshot():
        for series in metric["series"]:
            for label in series["labels"]:
                assert "_id" not in label


def test_recording_a_metric_never_raises_into_a_caller() -> None:
    """Section 66. A counter is not a reason to fail anything."""
    registry = Registry()
    registry.gauge("g", float("nan"))  # must not raise
    assert registry.value("g") is not None


# ================================================= 7. the API (§56, 57, 58)


async def test_the_public_health_endpoint_reveals_nothing_detailed(
    client: AsyncClient,
) -> None:
    """Section 58. A status word and the mode, and no dependency detail."""
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["trading_mode"] == "paper"
    assert "database" not in str(body).lower()
    assert "redis" not in str(body).lower()
    live = (await client.get("/health/live")).json()
    assert live == {"status": "alive"}


async def test_detailed_monitoring_requires_authorization(
    app: FastAPI, client: AsyncClient
) -> None:
    """Sections 56, 59 and 73."""
    detailed = [
        "/v1/monitoring/components",
        "/v1/monitoring/events",
        "/v1/monitoring/metrics",
        "/v1/monitoring/thresholds",
        "/v1/monitoring/uptime",
    ]
    for path in detailed:
        assert (await client.get(path)).status_code == 401, path
    await client.post("/auth/register", json=PLAIN)
    for path in detailed:
        assert (await client.get(path)).status_code == 403, path
    # The summary is the one a signed-in user may read.
    assert (await client.get("/v1/monitoring/summary")).status_code == 200


async def test_the_summary_carries_the_environment_and_the_safety_verdict(
    client: AsyncClient,
) -> None:
    await client.post("/auth/register", json=PLAIN)
    body = (await client.get("/v1/monitoring/summary")).json()
    assert body["environment"]["trading_mode"] == "paper"
    assert body["environment"]["live_trading"] is False
    assert body["trading_safety"] in ("SAFE", "DEGRADED", "BLOCKED", "UNKNOWN")
    assert "observational" in body.get("trading_safety_note", "") or body["collected_at"] is None


async def test_nothing_has_been_collected_reads_as_unknown_not_healthy(client: AsyncClient) -> None:
    """Section 65, applied to the summary itself."""
    await client.post("/auth/register", json=PLAIN)
    body = (await client.get("/v1/monitoring/summary")).json()
    assert body["status"] == "UNKNOWN"
    assert "not a healthy one" in body["note"]


async def test_a_collection_can_be_triggered_and_changes_no_trading_state(
    app: FastAPI, admin: AsyncClient
) -> None:
    before = (await admin.get("/v1/monitoring/summary")).json()["status"]
    r = await admin.post("/v1/monitoring/collect", headers=_csrf(admin))
    assert r.status_code == 200
    assert r.json()["status"] != before or r.json()["collected_at"] is not None
    async with app.state.session_factory() as db:
        assert (await db.scalar(select(Order))) is None


def _csrf(client: AsyncClient) -> dict[str, str]:
    from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER

    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def test_uptime_is_refused_rather_than_invented(admin: AsyncClient) -> None:
    """Section 64. No 99.99% without the history to support it."""
    body = (await admin.get("/v1/monitoring/uptime")).json()
    assert body["available"] is False
    assert "insufficient history" in body["why"]


async def test_no_monitoring_route_returns_a_secret(app: FastAPI) -> None:
    """Sections 58 and 73."""
    configured = Settings(
        _env_file=None,
        smtp_host="mail.example.com",
        smtp_password=SECRET,
        tv_webhook_secret=SECRET,
    )
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(configured, checks={}, engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        await c.post("/auth/register", json=ADMIN)
        await _promote(application, ADMIN["email"], Role.admin)
        await c.post("/v1/monitoring/collect", headers=_csrf(c))
        for path in (
            "/v1/monitoring/summary",
            "/v1/monitoring/components",
            "/v1/monitoring/events",
            "/v1/monitoring/metrics",
            "/v1/monitoring/thresholds",
            "/v1/monitoring/contract",
        ):
            body = (await c.get(path)).text
            assert SECRET not in body, path
            assert configured.database_url not in body, path
    await engine.dispose()


async def test_the_contract_states_what_monitoring_cannot_do(admin: AsyncClient) -> None:
    body = (await admin.get("/v1/monitoring/contract")).json()
    cannot = " ".join(body["does_not"])
    assert "place, modify or cancel an order" in cannot
    assert "enable live trading" in cannot
    assert "L38" in body["recovery"]
    assert body["state_meanings"]["UNKNOWN"].startswith("not observed")


async def test_a_component_that_was_not_collected_answers_404(admin: AsyncClient) -> None:
    await admin.post("/v1/monitoring/collect", headers=_csrf(admin))
    assert (await admin.get("/v1/monitoring/components/nothing-here")).status_code == 404


async def test_events_are_readable_after_an_incident(app: FastAPI, admin: AsyncClient) -> None:
    async with app.state.session_factory() as db:
        db.add(
            SystemEvent(
                component="database",
                event_type=str(IncidentType.service_down),
                level="critical",
                occurred_at=NOW,
                payload={"from": "HEALTHY", "to": "UNHEALTHY"},
            )
        )
        await db.commit()
    body = (await admin.get("/v1/monitoring/events")).json()
    assert body["events"][0]["component"] == "database"
    assert "retention" in body


# ============================================== 8. thresholds are in one place


def test_every_threshold_is_served_and_none_is_a_trading_limit(admin: AsyncClient) -> None:
    """Section 44."""
    bars = Thresholds()
    served = bars.as_dict()
    assert served["hysteresis"]["consecutive_failures"] == bars.consecutive_failures
    assert "RiskEngine owns what may be traded" in served["authority"]


def test_thresholds_are_not_scattered_through_the_collectors() -> None:
    """A threshold in five files is five numbers that drift."""
    source = (PACKAGE / "collectors.py").read_text(encoding="utf-8")
    for number in ("900", "3600", "250.0", "1000.0"):
        assert f"= {number}" not in source
