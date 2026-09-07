"""What each component reports about itself, read from the system that owns it.

Sections 12, 65 and 66, and the rule that shapes every function here:

    **A state is observed or it is UNKNOWN. Nothing is inferred from silence.**

Section 65 forbids fake health: no healthy broker, no connected Redis, no
active bot that was not actually measured. So every collector reads the owning
system's own answer -- `BrokerRegistry.health()`, `WorkerRegistry.stale()`,
`Hub.status()`, `MarketDataService.statuses()`, the L29 monitoring tables -- and
reports `UNKNOWN` when there is nothing to read rather than a reassuring
default.

**A collector reads. It cannot act.** `app/observability` imports no risk
engine, no order manager, no sizing service and no broker adapter class; the
registries it is handed expose health and description methods, and it calls
those. A parse test enumerates the package. Section 72: monitoring cannot place
an order, modify a position, bypass risk, enable live trading or change a bot's
state, and it cannot because there is nothing in scope through which it could.

**A failing probe is a DEGRADED component, not a failed pass.** Section 66 and
section 67: monitoring must detect its own failure rather than take the process
down with it. Every collector is wrapped so an exception becomes an UNKNOWN
component carrying the error category, and the pass continues.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.ai_registry import ModelDeployment
from app.models.bots import Bot, BotRun
from app.models.execution import Order, Position
from app.models.market import MarketBar
from app.models.monitoring import ModelAlert
from app.models.ops import NotificationDelivery
from app.notifications.contract import DeliveryStatus
from app.observability.contract import (
    ComponentHealth,
    ComponentState,
    Criticality,
    ErrorCategory,
    Layer,
    from_health,
)
from app.observability.thresholds import Thresholds

log = logging.getLogger("app.observability")

Collector = Callable[[], Awaitable[list[ComponentHealth]]]


def _health(
    name: str,
    layer: Layer,
    state: ComponentState,
    criticality: Criticality,
    detail: str,
    **extra: Any,
) -> ComponentHealth:
    return ComponentHealth(
        name=name,
        layer=layer,
        state=state,
        criticality=criticality,
        detail=detail,
        checked_at=utcnow(),
        latency_ms=extra.pop("latency_ms", None),
        error_count=extra.pop("error_count", 0),
        last_error=extra.pop("last_error", None),
        facts=extra,
    )


def categorise(exc: BaseException) -> ErrorCategory:
    """Section 52. A closed vocabulary, from the exception's own type."""
    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return ErrorCategory.timeout
    if "connection" in name or "socket" in name or "dns" in name:
        return ErrorCategory.network
    if "auth" in name or "permission" in name:
        return ErrorCategory.authentication
    if "operationalerror" in name or "dbapi" in name or "sqlalchemy" in name:
        return ErrorCategory.database
    if "value" in name or "validation" in name:
        return ErrorCategory.validation
    return ErrorCategory.unknown


def failed(
    name: str, layer: Layer, criticality: Criticality, exc: BaseException
) -> ComponentHealth:
    """A probe that raised. UNKNOWN, because we did not observe the component.

    Not UNHEALTHY: the monitor failed, and reporting the component as broken
    because the monitor broke is exactly the fake health section 65 forbids --
    in the other direction.
    """
    return _health(
        name,
        layer,
        ComponentState.unknown,
        criticality,
        f"the probe failed: {type(exc).__name__}",
        last_error=f"{type(exc).__name__}: {exc}"[:200],
        error_count=1,
        error_category=str(categorise(exc)),
    )


# ============================================================ infrastructure


async def infrastructure(checks: dict[str, Any], bars: Thresholds) -> list[ComponentHealth]:
    """Database, Redis and whatever else `app.state.checks` holds.

    Reuses L02's check functions rather than probing again: `/health/ready`
    already runs these and the HTTP status code is derived from them. Two
    probes would be two answers, and the one on the dashboard would eventually
    disagree with the one the orchestrator used to restart the container.
    """
    from app.core.health import run_checks

    out: list[ComponentHealth] = []
    results = await run_checks(checks)
    for name, result in results.items():
        state = from_health(result.status)
        # Latency turns a HEALTHY into a DEGRADED, which the three-state check
        # has no vocabulary for. Section 13: a database that answers slowly is
        # not a database that is down.
        if state is ComponentState.healthy and name == "database":
            if result.latency_ms >= bars.database_latency_critical_ms:
                state = ComponentState.unhealthy
            elif result.latency_ms >= bars.database_latency_warning_ms:
                state = ComponentState.degraded
        if state is ComponentState.healthy and name == "redis":
            if result.latency_ms >= bars.redis_latency_critical_ms:
                state = ComponentState.unhealthy
            elif result.latency_ms >= bars.redis_latency_warning_ms:
                state = ComponentState.degraded
        out.append(
            _health(
                name,
                Layer.workers if name == "workers" else Layer.infrastructure,
                state,
                Criticality.critical if result.critical else Criticality.optional,
                result.detail,
                latency_ms=result.latency_ms,
            )
        )
    return out


def event_bus(app_state: Any) -> ComponentHealth:
    """The bus, from the hub's own observation rather than from a URL."""
    hub = getattr(app_state, "hub", None)
    if hub is None:
        return _health(
            "event_bus",
            Layer.infrastructure,
            ComponentState.unknown,
            Criticality.important,
            "no hub in this process",
        )
    status = hub.status()
    healthy = bool(status.get("bus_healthy")) and bool(status.get("reader_running"))
    return _health(
        "event_bus",
        Layer.infrastructure,
        ComponentState.healthy if healthy else ComponentState.degraded,
        Criticality.important,
        (
            f"{status.get('bus')} bus, reader running"
            if healthy
            else f"{status.get('bus')} bus: {status.get('bus_error') or 'reader not running'}"
        ),
        last_error=status.get("bus_error"),
        publish_failures=int(status.get("publish_failures") or 0),
        kind=status.get("bus"),
    )


def realtime(app_state: Any) -> ComponentHealth:
    """Section 36. Connections and counters, never a message payload."""
    hub = getattr(app_state, "hub", None)
    if hub is None:
        return _health(
            "realtime",
            Layer.realtime,
            ComponentState.unknown,
            Criticality.optional,
            "no hub in this process",
        )
    status = hub.status()
    dropped = int(status.get("events_dropped_slow") or 0)
    return _health(
        "realtime",
        Layer.realtime,
        ComponentState.degraded if dropped else ComponentState.healthy,
        Criticality.optional,
        (
            f"{status.get('connections')} connection(s); {dropped} dropped for slowness"
            if dropped
            else f"{status.get('connections')} connection(s)"
        ),
        connections=int(status.get("connections") or 0),
        subscriptions=int(status.get("subscriptions") or 0),
        events_published=int(status.get("events_published") or 0),
        events_delivered=int(status.get("events_delivered") or 0),
        events_dropped_slow=dropped,
    )


# =================================================================== workers


def workers(app_state: Any, bars: Thresholds) -> list[ComponentHealth]:
    """Section 15. Per worker, from L02's registry -- never restarted here.

    Section 47 is explicit that L37 detects and L38 recovers. This reports
    `RECOVERY_REQUIRED` in words and does not act: a monitor that restarts a
    worker is a supervisor, and a supervisor that is also the thing observing
    the worker cannot tell you it failed to restart it.
    """
    registry = getattr(app_state, "workers", None)
    if registry is None or not getattr(registry, "workers", {}):
        return [
            _health(
                "workers",
                Layer.workers,
                ComponentState.unknown,
                Criticality.optional,
                "no workers registered in this process",
            )
        ]
    out: list[ComponentHealth] = []
    for status in registry.statuses():
        name = str(status["name"])
        passes = int(status.get("passes") or 0)
        failures = int(status.get("failures") or 0)
        rate = failures / passes if passes else 0.0
        if not status.get("running"):
            state, detail = ComponentState.not_configured, "registered and not started"
        elif status.get("stale"):
            state, detail = (
                ComponentState.unhealthy,
                "no heartbeat within its own staleness allowance; recovery required (L38)",
            )
        elif rate >= bars.worker_failure_rate_warning:
            state, detail = (
                ComponentState.degraded,
                f"{failures} of {passes} passes failed",
            )
        else:
            state, detail = ComponentState.healthy, f"{passes} pass(es), {failures} failed"
        out.append(
            _health(
                f"worker:{name}",
                Layer.workers,
                state,
                Criticality.important,
                detail,
                error_count=failures,
                last_error=status.get("last_error"),
                passes=passes,
                last_heartbeat=status.get("last_heartbeat"),
                started_at=status.get("started_at"),
            )
        )
    return out


async def queues(db: AsyncSession, app_state: Any, bars: Thresholds) -> list[ComponentHealth]:
    """Section 16. The one real queue this platform has: notification delivery.

    Named honestly. There is no Celery, no RQ and no dead-letter queue here;
    the notification delivery table IS the queue, and its depth and oldest
    pending row are what a backlog looks like. Inventing entries for queues the
    platform does not have would be fake health.
    """
    pending = [str(DeliveryStatus.pending), str(DeliveryStatus.retrying)]
    depth = int(
        await db.scalar(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.status.in_(pending)
            )
        )
        or 0
    )
    oldest = await db.scalar(
        select(func.min(NotificationDelivery.created_at)).where(
            NotificationDelivery.status.in_(pending)
        )
    )
    age = (utcnow() - oldest).total_seconds() if oldest else 0.0
    failed_count = int(
        await db.scalar(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.status == str(DeliveryStatus.failed)
            )
        )
        or 0
    )
    if depth >= bars.queue_backlog_critical:
        state, detail = ComponentState.unhealthy, f"{depth} deliveries waiting"
    elif depth >= bars.queue_backlog_warning or age >= bars.queue_age_warning_seconds:
        state, detail = (
            ComponentState.degraded,
            f"{depth} waiting, oldest {int(age)}s",
        )
    else:
        state, detail = ComponentState.healthy, f"{depth} waiting"
    return [
        _health(
            "queue:notification-delivery",
            Layer.workers,
            state,
            Criticality.important,
            detail,
            error_count=failed_count,
            depth=depth,
            oldest_pending_seconds=round(age, 1),
            failed=failed_count,
        )
    ]


# =============================================================== market data


async def market_data(db: AsyncSession, app_state: Any, bars: Thresholds) -> list[ComponentHealth]:
    """Sections 20 and 21. Providers, and the freshness of what is stored.

    **Freshness is measured, or it is UNAVAILABLE.** Section 21: where a source
    timestamp does not exist, say so rather than fabricate a latency. This
    platform stores bars with a `bar_time`, so the age of the newest one is a
    real measurement; a deployment that has ingested nothing reports UNKNOWN
    with "no bar has ever been stored", which is the honest answer and is
    exactly what this machine's state is.
    """
    out: list[ComponentHealth] = []
    service = getattr(app_state, "market_data", None)
    if service is None:
        out.append(
            _health(
                "market_data.providers",
                Layer.market_data,
                ComponentState.unknown,
                Criticality.important,
                "no market data service in this process",
            )
        )
    else:
        statuses = await service.statuses()
        usable = [s for s in statuses if s.usable]
        out.append(
            _health(
                "market_data.providers",
                Layer.market_data,
                ComponentState.healthy if usable else ComponentState.degraded,
                Criticality.important,
                f"{len(usable)} of {len(statuses)} provider(s) usable",
                providers={str(s.name): s.usable for s in statuses},
            )
        )

    newest = await db.scalar(select(func.max(MarketBar.bar_time)))
    if newest is None:
        out.append(
            _health(
                "market_data.freshness",
                Layer.market_data,
                ComponentState.unknown,
                Criticality.important,
                "no bar has ever been stored, so freshness is UNAVAILABLE rather than 0",
                newest_bar=None,
            )
        )
        return out
    age = (utcnow() - newest).total_seconds()
    if age >= bars.market_data_critical_seconds:
        state, detail = (
            ComponentState.unhealthy,
            f"newest bar is {int(age)}s old; strategy decisions on this are unsafe",
        )
    elif age >= bars.market_data_stale_seconds:
        state, detail = ComponentState.degraded, f"newest bar is {int(age)}s old"
    else:
        state, detail = ComponentState.healthy, f"newest bar is {int(age)}s old"
    out.append(
        _health(
            "market_data.freshness",
            Layer.market_data,
            state,
            Criticality.important,
            detail,
            newest_bar=newest.isoformat(),
            age_seconds=round(age, 1),
            note=(
                "missing data is not an unchanged price. A stale feed degrades this "
                "component; it does not make the last price current."
            ),
        )
    )
    return out


# =================================================================== trading


async def trading(db: AsyncSession, app_state: Any, bars: Thresholds) -> list[ComponentHealth]:
    """Sections 17, 18, 25, 27 and 28. Read from the systems that own each.

    Nothing here is recomputed. The broker's state is the adapter's own answer,
    the risk state is `RiskService.status()`, and the order counts are the OMS's
    rows. Section 17 and section 25: monitoring does not independently determine
    broker truth and does not become the RiskEngine.
    """
    out: list[ComponentHealth] = []

    # --- Broker adapters, through the registry (§17, §18).
    brokers = getattr(app_state, "brokers", None)
    adapters = getattr(brokers, "adapters", {}) if brokers is not None else {}
    if not adapters:
        out.append(
            _health(
                "broker",
                Layer.trading,
                ComponentState.not_configured,
                Criticality.optional,
                (
                    "no adapter is registered. Registering one is an operator action, "
                    "not a side effect of the process booting, so this is the honest "
                    "state of a deployment that has not been pointed at a venue."
                ),
                adapters=0,
            )
        )
    else:
        # The `if` above returned for `brokers is None`; mypy cannot see that
        # through `getattr(..., None)`, so the narrowing is stated.
        assert brokers is not None
        health = await brokers.health()
        connected = [a for a, h in health.items() if h.usable]
        degraded = [a for a, h in health.items() if not h.usable]
        out.append(
            _health(
                "broker",
                Layer.trading,
                (ComponentState.healthy if not degraded else ComponentState.degraded),
                Criticality.critical,
                f"{len(connected)} of {len(health)} adapter(s) connected",
                error_count=len(degraded),
                states={a: str(h.state) for a, h in health.items()},
                trade_allowed={a: h.trade_allowed for a, h in health.items()},
                reconnects=sum(h.reconnects for h in health.values()),
            )
        )

    # --- OMS: what the order table says, including the state it could not
    # establish. Section 17: reported as reconciliation required, never failed.
    unknown_orders = int(
        await db.scalar(select(func.count(Order.id)).where(Order.status == "unknown")) or 0
    )
    working = int(
        await db.scalar(
            select(func.count(Order.id)).where(
                Order.status.in_(["created", "submitted", "acknowledged", "partially_filled"])
            )
        )
        or 0
    )
    managers = getattr(getattr(app_state, "order_managers", None), "managers", {}) or {}
    out.append(
        _health(
            "oms",
            Layer.trading,
            (
                ComponentState.degraded
                if unknown_orders >= bars.unknown_orders_warning
                else ComponentState.healthy
            ),
            Criticality.important,
            (
                f"{unknown_orders} order(s) whose broker state could not be established; "
                "RECONCILIATION_REQUIRED, not failed"
                if unknown_orders
                else f"{working} working order(s), none unresolved"
            ),
            error_count=unknown_orders,
            orders_working=working,
            orders_unknown=unknown_orders,
            order_managers=len(managers),
        )
    )

    # --- Positions (§28). Counts and states; nothing is modified.
    reconciling = int(
        await db.scalar(
            select(func.count(Position.id)).where(Position.status.in_(["unknown", "reconciling"]))
        )
        or 0
    )
    open_positions = int(
        await db.scalar(
            select(func.count(Position.id)).where(Position.status.in_(["open", "partially_closed"]))
        )
        or 0
    )
    out.append(
        _health(
            "positions",
            Layer.trading,
            ComponentState.degraded if reconciling else ComponentState.healthy,
            Criticality.important,
            (
                f"{reconciling} position(s) need reconciliation"
                if reconciling
                else f"{open_positions} open"
            ),
            error_count=reconciling,
            open=open_positions,
            reconciling=reconciling,
        )
    )

    # --- RiskEngine (§25). Its own status, never a second opinion.
    risk = getattr(app_state, "risk", None)
    if risk is None:
        out.append(
            _health(
                "risk_engine",
                Layer.trading,
                ComponentState.unknown,
                Criticality.critical,
                "no risk service in this process",
            )
        )
    else:
        status = risk.status()
        switches = status.get("kill_switches", {})
        engaged = (
            bool(switches.get("global"))
            or bool(switches.get("accounts"))
            or bool(switches.get("strategies"))
        )
        out.append(
            _health(
                "risk_engine",
                Layer.trading,
                ComponentState.degraded if engaged else ComponentState.healthy,
                Criticality.critical,
                (
                    "a kill switch is engaged; the engine is blocking, which is it "
                    "working rather than failing"
                    if engaged
                    else "no kill switch engaged"
                ),
                kill_switch_global=bool(switches.get("global")),
                kill_switch_accounts=len(switches.get("accounts") or []),
                kill_switch_strategies=len(switches.get("strategies") or []),
                trading_mode=status.get("trading_mode"),
                authority=(
                    "the RiskEngine decides. This is a reading of what it decided; "
                    "nothing in app/observability can change a limit or release a switch."
                ),
            )
        )

    # --- Bots (§22, §23). State, not control.
    total_bots = int(await db.scalar(select(func.count(Bot.id))) or 0)
    live_runs = int(
        await db.scalar(
            select(func.count(BotRun.id)).where(BotRun.status.in_(["starting", "running"]))
        )
        or 0
    )
    silent = 0
    if live_runs:
        cutoff = utcnow() - timedelta(seconds=120)
        silent = int(
            await db.scalar(
                select(func.count(BotRun.id)).where(
                    BotRun.status.in_(["starting", "running"]),
                    (BotRun.last_heartbeat_at.is_(None)) | (BotRun.last_heartbeat_at < cutoff),
                )
            )
            or 0
        )
    out.append(
        _health(
            "bots",
            Layer.trading,
            (
                ComponentState.unknown
                if silent
                else (ComponentState.healthy if total_bots else ComponentState.not_configured)
            ),
            Criticality.optional,
            (
                f"{silent} run(s) with no recent heartbeat: UNKNOWN, not dead. "
                "Recovery is L38's decision."
                if silent
                else (
                    f"{live_runs} live run(s) of {total_bots} bot(s)" if total_bots else "no bots"
                )
            ),
            error_count=silent,
            bots=total_bots,
            runs_live=live_runs,
            runs_silent=silent,
        )
    )
    return out


# ======================================================================== ai


async def ai(db: AsyncSession, app_state: Any) -> list[ComponentHealth]:
    """Sections 31 and 32. Consumed from L28 and L29; nothing is recalculated.

    Drift is not computed here, validation is not repeated here, and the
    registry is the source of truth for what is deployed. Section 31 says so in
    as many words, and the collector obeys it by reading two tables.
    """
    deployed = int(
        await db.scalar(
            select(func.count(ModelDeployment.id)).where(ModelDeployment.status == "active")
        )
        or 0
    )
    open_alerts = int(
        await db.scalar(select(func.count(ModelAlert.id)).where(ModelAlert.status == "firing")) or 0
    )
    critical_alerts = int(
        await db.scalar(
            select(func.count(ModelAlert.id)).where(
                ModelAlert.status == "firing", ModelAlert.severity == "critical"
            )
        )
        or 0
    )
    registry = getattr(app_state, "ai_models", None)
    loaded = len(getattr(registry, "models", {}) or {}) if registry is not None else 0

    if deployed == 0:
        state, detail = (
            ComponentState.not_configured,
            "no model version is deployed. Nothing to monitor is a fact about the "
            "registry, not a clean bill of health.",
        )
    elif critical_alerts:
        state, detail = ComponentState.unhealthy, f"{critical_alerts} critical model alert(s)"
    elif open_alerts:
        state, detail = ComponentState.degraded, f"{open_alerts} open model alert(s)"
    else:
        state, detail = ComponentState.healthy, f"{deployed} active deployment(s)"
    return [
        _health(
            "ai.models",
            Layer.ai,
            state,
            Criticality.optional,
            detail,
            error_count=open_alerts,
            deployments_active=deployed,
            models_loaded=loaded,
            open_alerts=open_alerts,
            source="L28 registry and L29 monitoring, read rather than recomputed",
        )
    ]


# ============================================================= notifications


async def notifications(
    db: AsyncSession, app_state: Any, bars: Thresholds
) -> list[ComponentHealth]:
    """Section 34 and 35. Channels and delivery, from L34's own registry."""
    out: list[ComponentHealth] = []
    service = getattr(app_state, "notifications", None)
    if service is None:
        return [
            _health(
                "notifications",
                Layer.notifications,
                ComponentState.unknown,
                Criticality.optional,
                "no notification service in this process",
            )
        ]

    for described in service.channels.describe():
        name = str(described.get("channel"))
        available = bool(described.get("available"))
        state = (
            ComponentState.healthy
            if available
            else (
                ComponentState.not_configured
                if described.get("state") in ("NOT_CONFIGURED", "DISABLED")
                else ComponentState.degraded
            )
        )
        out.append(
            _health(
                f"notifications.{name.lower()}",
                Layer.notifications,
                state,
                Criticality.optional,
                str(described.get("detail") or described.get("state") or ""),
                error_count=int(described.get("failed") or 0),
                channel_state=described.get("state"),
                sent=described.get("sent"),
                rate_limited=described.get("rate_limited"),
            )
        )

    consumer = getattr(app_state, "notification_consumer", None)
    if consumer is not None:
        status = consumer.status()
        running = bool(status.get("running"))
        out.append(
            _health(
                "notifications.consumer",
                Layer.notifications,
                ComponentState.healthy if running else ComponentState.not_configured,
                Criticality.important,
                (
                    f"reading the {status.get('bus')} bus; {status.get('events_handled')} "
                    "event(s) handled"
                    if running
                    else "not started"
                ),
                error_count=int(status.get("failures") or 0),
                last_error=status.get("last_error"),
                events_handled=status.get("events_handled"),
                notifications_created=status.get("notifications_created"),
            )
        )

    delivered = int(
        await db.scalar(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.status == str(DeliveryStatus.delivered)
            )
        )
        or 0
    )
    failed_count = int(
        await db.scalar(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.status == str(DeliveryStatus.failed)
            )
        )
        or 0
    )
    attempted = delivered + failed_count
    rate = failed_count / attempted if attempted else 0.0
    out.append(
        _health(
            "notifications.delivery",
            Layer.notifications,
            (
                ComponentState.degraded
                if rate >= bars.notification_failure_rate_warning and attempted
                else ComponentState.healthy
            ),
            Criticality.optional,
            f"{delivered} delivered, {failed_count} failed",
            error_count=failed_count,
            delivered=delivered,
            failed=failed_count,
            failure_rate=round(rate, 4),
        )
    )
    return out


# ===================================================== the monitor's own self


def self_health(
    *, last_collection: Any, failures: int, duration_ms: float | None, bars: Thresholds
) -> ComponentHealth:
    """Section 67. A monitor that cannot detect its own failure is decoration."""
    if last_collection is None:
        return _health(
            "monitoring.self",
            Layer.infrastructure,
            ComponentState.unknown,
            Criticality.important,
            "no collection has completed yet",
            error_count=failures,
        )
    age = (utcnow() - last_collection).total_seconds()
    stale = age > max(bars.interval_seconds * 3, 60)
    return _health(
        "monitoring.self",
        Layer.infrastructure,
        ComponentState.degraded if stale or failures else ComponentState.healthy,
        Criticality.important,
        (f"last collection {int(age)}s ago" + (f", {failures} failure(s)" if failures else "")),
        latency_ms=duration_ms,
        error_count=failures,
        last_collection=last_collection.isoformat(),
        interval_seconds=bars.interval_seconds,
    )


def timed() -> Callable[[], float]:
    started = time.perf_counter()
    return lambda: round((time.perf_counter() - started) * 1000, 1)


__all__ = [
    "Collector",
    "ai",
    "categorise",
    "event_bus",
    "failed",
    "infrastructure",
    "market_data",
    "notifications",
    "queues",
    "realtime",
    "self_health",
    "timed",
    "trading",
    "workers",
]
