"""FastAPI application.

Foundation surface, all read-only:

  GET /health         liveness: the process is up, and which mode it is in
  GET /health/ready   readiness: every dependency, with three states
  GET /health/live    the bare liveness probe a container orchestrator wants

Everything else lives under /v1 (see `app.api.v1`), which behind nginx is
served as /api/v1. The health paths stay at the root because they are an
infrastructure contract: the Compose healthcheck and any orchestrator probe
them there, and /v1/system/health reads the same check functions.

Readiness returns 503 only when a CRITICAL dependency is unavailable. A
degraded optional dependency is reported as degraded with 200, because the
API still works without it. See `app.core.health` for how the overall status
is derived.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from app.admin.router import router as admin_router
from app.ai.registry import ModelRegistry
from app.api.protected import router as legacy_protected_router
from app.api.v1 import build_v1_router
from app.auth.csrf import make_middleware as make_csrf_middleware
from app.auth.ratelimit import make_rate_limiter
from app.auth.reset import UnconfiguredDelivery
from app.auth.router import router as auth_router
from app.backtest.service import BacktestService
from app.bots.worker import BotSupervisorWorker
from app.brokers.registry import BrokerRegistry
from app.core import errors
from app.core.events import make_event_bus
from app.core.health import (
    CheckFn,
    HealthStatus,
    WorkerHealth,
    check_database,
    check_redis,
    overall_status,
    run_checks,
)
from app.core.logging import configure_logging
from app.core.release import describe as describe_release
from app.core.settings import Settings, get_settings
from app.db.session import make_engine, make_session_factory
from app.execution.limits import loader_for as risk_limits_loader_for
from app.execution.pipeline import ExecutionPipeline
from app.execution.portfolio import snapshot_for as portfolio_snapshot_for
from app.execution.store import store_for as order_store_for
from app.execution.worker import ExecutionWorker
from app.marketdata.service import default_service
from app.notifications.channels import NotificationResetDelivery, build_registry
from app.notifications.channels.email import provider_from
from app.notifications.service import NotificationService
from app.notifications.worker import NotificationConsumer, NotificationDeliveryWorker
from app.observability.service import ObservabilityService
from app.observability.thresholds import from_settings as monitoring_thresholds
from app.observability.worker import MonitoringWorker
from app.oms.registry import OrderManagerRegistry
from app.oms.worker import OmsReconcileWorker
from app.paper.service import PaperService
from app.positions.wiring import monitor_for
from app.realtime.hub import Hub
from app.recovery.bots import recovery_gate as bot_recovery_gate
from app.recovery.contract import SafeModeReason as _SafeModeReason
from app.recovery.manager import RecoveryManager
from app.recovery.safe_mode import SafeMode
from app.replay.service import ReplayService
from app.risk.engine import RiskEngine, RiskLimits
from app.risk.service import RiskService
from app.security.announce import SecurityAnnouncer
from app.security.headers import ALLOWED_REQUEST_HEADERS, SecurityHeadersMiddleware
from app.security.stepup import StepUp
from app.sizing.service import SizingService
from app.strategies.engine import StrategyEngine
from app.strategies.registry import default_registry
from app.training.service import TrainingService
from app.validation.service import ValidationService
from app.webhooks.gateway import WebhookGateway
from app.workers.base import WorkerRegistry

log = logging.getLogger("app")


def default_checks(settings: Settings, registry: WorkerRegistry) -> dict[str, CheckFn]:
    t = settings.health_timeout_seconds
    return {
        # The database is critical: no request that touches state works
        # without it.
        #
        # Redis became critical at L07, as `app.core.health` said it would.
        # The bus now carries the events that tell an operator an order
        # changed state, and a process that cannot publish them has observers
        # who are silently blind. Silence in an execution path reads as "no
        # orders moved", so a loud 503 is the safer failure. Note the
        # criticality is about *reporting*: nothing downstream treats a bus
        # failure as permission to trade, and the OMS reconciles against the
        # broker rather than against the bus.
        "database": partial(check_database, settings.database_url, t, True),
        "redis": partial(check_redis, settings.redis_url, t, settings.events_enabled),
        "workers": WorkerHealth(registry),
    }


# =============================================== the execution engine's inputs
#
# Three small adapters between the platform's storage and the pipeline's
# narrow inputs. They live here rather than inside `app.execution` so that
# package keeps its property of holding no session, no adapter and no service
# -- a test parses it to prove exactly that.


def _spec_loader(app: FastAPI):  # noqa: ANN202
    """internal symbol -> its measured contract spec, or None.

    Returns None rather than raising, because a missing spec is a refusal the
    pipeline records with a reason, not an exception it has to classify.
    """

    async def load(symbol: str):  # noqa: ANN202
        from app.symbols.errors import SymbolError
        from app.symbols.service import contract_spec

        async with app.state.session_factory() as db:
            try:
                return await contract_spec(db, symbol)
            except SymbolError:
                return None

    return load


def _strategy_state(app: FastAPI):  # noqa: ANN202
    """What the platform knows about a strategy a signal named.

    The built-in registry answers whether it EXISTS: a key it does not know is
    a strategy this platform cannot attribute a trade to, and the pipeline
    refuses rather than executing an unattributable signal.

    **`strategies.is_active` answers whether it may trade (L56).** Until this
    level the two were the same question -- `enabled=known` -- so a strategy
    that existed could never be switched off, and the pipeline's own
    `strategy_disabled` gate could not fire for any registered strategy. The
    column has been there since L05; nothing read it.

    That is L56's quarantine, and it is deliberately the existing column rather
    than a new one: "no new entries from this strategy" is exactly what
    `is_active=false` means, and a second flag would be a second answer to one
    question.

    **Quarantine stops new signals. It does not touch open positions** -- the
    Position Manager, the RiskEngine and the configured exit policies keep
    managing those, which is what L56 step 14 requires. A strategy being
    switched off is not a reason to sell at a price nobody chose.

    **A database it cannot read leaves the strategy DISABLED**, not enabled:
    an unreadable quarantine flag must not be an open gate.
    """

    async def state(strategy_id: str | None):  # noqa: ANN202
        from sqlalchemy import select

        from app.execution.pipeline import StrategyState
        from app.models.strategies import Strategy

        if not strategy_id:
            return StrategyState(
                exists=False,
                enabled=False,
                reason="the signal named no strategy",
            )
        registry = app.state.strategy_engine.registry
        known = strategy_id in registry.keys()
        if not known:
            return StrategyState(
                exists=False,
                enabled=False,
                reason=f"no strategy {strategy_id!r} is registered",
            )

        try:
            async with app.state.session_factory() as db:
                row = await db.scalar(select(Strategy).where(Strategy.key == strategy_id))
        except Exception:  # noqa: BLE001 - fail closed, never open
            log.exception(
                "the strategy's active flag could not be read; treating it as switched "
                "off, because an unreadable quarantine must not be an open gate",
                extra={"event": "strategy_state_unreadable", "strategy_id": strategy_id},
            )
            return StrategyState(
                exists=True,
                enabled=False,
                reason=(
                    f"strategy {strategy_id} could not be checked against its active "
                    "flag; refusing rather than assuming it is permitted"
                ),
            )

        # A registered strategy with no row has never been recorded, so nobody
        # has switched it off. Enabled, and the registry is the authority for
        # existence -- the same reading L05 gave it.
        if row is None:
            return StrategyState(exists=True, enabled=True, reason="")
        if not row.is_active:
            return StrategyState(
                exists=True,
                enabled=False,
                reason=(
                    f"strategy {strategy_id} is switched off (strategies.is_active is "
                    "false). Open positions are unaffected and stay under the position "
                    "manager"
                ),
            )
        return StrategyState(exists=True, enabled=True, reason="")

    return state


def _to_incoming_signal(row):  # noqa: ANN001, ANN202
    """A `signals` row -> the pipeline's input, or None if it is not actionable.

    Returning None is not an error path: it is how a recorded signal that
    cannot become an order (no account, no entry price) is retired with a
    reason instead of being retried forever.
    """
    from app.execution.pipeline import IncomingSignal

    meta = row.meta or {}
    account_id = meta.get("account_id")
    entry = meta.get("price_reported")
    if not account_id or entry in (None, "None"):
        # L45 F-1: `meta["not_executable"]` names WHY when the gateway could
        # not route the alert to a bot. Before that key existed this branch was
        # reached for every alert the platform had ever received, and the only
        # trace was a `signal_not_executable` log line with no reason on it.
        return None
    from datetime import UTC
    from decimal import Decimal, InvalidOperation

    def money(value):  # noqa: ANN001, ANN202
        if value in (None, "", "None"):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    price = money(entry)
    if price is None:
        return None
    return IncomingSignal(
        signal_id=row.id,
        signal_key=row.signal_key,
        source=row.source,
        symbol=str(meta.get("internal_symbol") or ""),
        side=row.direction,
        signal_time=row.signal_time.replace(tzinfo=UTC),
        account_id=str(account_id),
        mode=row.mode,
        strategy_id=meta.get("strategy_id"),
        entry_price=price,
        stop_loss=money(meta.get("stop_loss")),
        take_profit=money(meta.get("take_profit")),
        auth_strength=row.auth_strength,
        # Recorded so the alert can be audited against what the platform did.
        # Never obeyed: size and brackets are the platform's.
        advisory=dict(meta.get("advisory_ignored") or {}),
        # L45 F-1. Written by the gateway from the `bots` row it routed to,
        # never from the alert payload.
        bot_id=meta.get("bot_id"),
        risk_amount=money(meta.get("risk_amount")),
    )


def create_app(
    settings: Settings | None = None,
    checks: dict[str, CheckFn] | None = None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    # Creating an engine does not connect; the first query does.
    engine = engine or make_engine(settings.database_url)
    registry = WorkerRegistry()
    checks = default_checks(settings, registry) if checks is None else checks
    # At construction, not in the lifespan: uvicorn logs its own startup lines
    # before the lifespan runs, and those should be JSON too.
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            extra={
                "event": "startup",
                "environment": settings.environment.value,
                "trading_mode": settings.trading_mode.value,
                "live_trading": settings.live_trading,
                "live_execution_allowed": settings.live_execution_allowed,
                "event_bus": app.state.event_bus.kind,
            },
        )
        # Started here rather than at construction: the reader is an asyncio
        # task and there is no running loop until the lifespan begins.
        await app.state.hub.start()
        # L34. Started rather than merely registered, unlike the execution
        # worker and the bot supervisor: those begin consuming signals and
        # supervising bots, which is an operator decision. These two only read
        # events and send messages, and nothing in `app/notifications` can
        # place an order or change a limit -- a test parses the package to keep
        # that true. A platform whose notifications begin only when somebody
        # remembers to switch them on is a platform that misses the first
        # breach.
        # L41. `workers_enabled` decides whether THIS process runs them.
        # The API and the workers are one image and one process today, so a
        # second API replica is a second notification consumer and a second
        # monitoring worker. `docker-compose.prod.yml` runs one API with
        # workers off and one worker container with them on; a single-process
        # deployment leaves the default True and behaves as it always did.
        if settings.notifications_enabled and settings.workers_enabled:
            await app.state.notification_consumer.start()
            app.state.workers.start(app.state.notification_delivery)
        # L37. Started with the platform for the same reason the notification
        # workers are, and the opposite reason to the execution worker: this one
        # reads. A platform whose monitoring begins only when somebody remembers
        # to switch it on is a platform that misses the first outage.
        if settings.monitoring_enabled and settings.workers_enabled:
            app.state.workers.start(app.state.monitoring_worker)

        # L51. The execution worker, and it is started ONLY when an operator
        # has said so -- `execution_worker_enabled` defaults False, unlike
        # every other worker flag.
        #
        # Deliberately BEFORE the recovery sequence below in the file and
        # AFTER it at runtime: see the `_start_execution_worker` call that
        # follows the sequence. Nothing may consume a signal until startup
        # reconciliation has run and had the chance to latch safe mode --
        # which, since the L45 C-1 fix, it can actually do, because the
        # pipeline now writes the `orders` rows that reconciliation reads.

        # L38. The startup sequence, after the bus and the workers exist and
        # BEFORE anything consumes a signal -- the execution worker and the bot
        # supervisor are registered and not started, so nothing has begun.
        #
        # It never raises: a recovery routine that can crash leaves the platform
        # in whatever state the crash found. A step that finds something needing
        # a person closes the safe-mode latch with that condition as its reason,
        # and starting successfully never enables live trading.
        if settings.recovery_startup_checks:
            try:
                async with app.state.session_factory() as db:
                    report = await app.state.recovery.run_startup(db, app)
                    await db.commit()
                await app.state.recovery.announce(app.state.hub, report)
                log.info(
                    "startup recovery complete",
                    extra={
                        "event": "recovery_startup",
                        "clean": report.clean,
                        "needs_attention": len(report.attention),
                        "safe_mode": app.state.safe_mode.engaged,
                    },
                )
            except Exception:  # noqa: BLE001 - the platform starts observable
                log.exception(
                    "the startup recovery sequence failed",
                    extra={"event": "recovery_startup_failed"},
                )
                app.state.safe_mode.engage(
                    _SafeModeReason.recovery_failed,
                    "the startup recovery sequence itself failed; nothing was verified",
                )

        # The reconcile sweep, started BEFORE the execution worker and AFTER
        # the recovery sequence. The order is the argument: startup
        # reconciliation has already counted what is unresolved and latched
        # safe mode for it, this loop is what can now clear those, and the
        # consumer of signals starts last.
        if settings.oms_reconcile_enabled and settings.workers_enabled:
            app.state.workers.start(app.state.oms_reconciler)
            log.info(
                "the OMS reconcile sweep is running; orders parked `unknown` will be "
                "settled against the venue without a human",
                extra={
                    "event": "oms_reconcile_started",
                    "interval_seconds": settings.oms_reconcile_interval_seconds,
                    "accounts": sorted(app.state.order_managers.managers),
                },
            )
        else:
            log.info(
                "the OMS reconcile sweep is registered and NOT started; an order "
                "parked `unknown` stays blocked until POST /v1/orders/{id}/reconcile",
                extra={
                    "event": "oms_reconcile_idle",
                    "reason": (
                        "oms_reconcile_enabled is false"
                        if not settings.oms_reconcile_enabled
                        else "workers_enabled is false"
                    ),
                },
            )

        # The position monitor, on the same terms as the execution worker and
        # after the same recovery sequence: a process that came up with an
        # unresolved order has latched safe mode before anything here closes
        # a position.
        if settings.position_monitor_enabled and settings.workers_enabled:
            app.state.workers.start(app.state.position_monitor)
            log.warning(
                "the position monitor is running; open positions will be managed and "
                "closed without a browser",
                extra={
                    "event": "position_monitor_started",
                    "trading_mode": settings.trading_mode.value,
                    "interval_seconds": settings.position_monitor_interval_seconds,
                },
            )
        else:
            log.info(
                "the position monitor is registered and NOT started; nothing manages "
                "an open position until POST /v1/positions/monitor/start or /sweep",
                extra={
                    "event": "position_monitor_idle",
                    "reason": (
                        "position_monitor_enabled is false"
                        if not settings.position_monitor_enabled
                        else "workers_enabled is false"
                    ),
                },
            )

        # L51. The execution worker starts HERE and nowhere earlier: after the
        # recovery sequence has run, so a platform that came up with an
        # unresolved order has already latched safe mode and this worker's
        # first pass is refused at gate zero rather than acting on it.
        #
        # It is also refused if the sequence RAISED -- the handler above
        # latches `recovery_failed`, and safe mode is checked per pass, not
        # once at startup.
        if settings.execution_worker_enabled and settings.workers_enabled:
            app.state.workers.start(app.state.execution)
            log.warning(
                "the execution worker is running; recorded signals will be carried "
                "through risk, sizing and the OMS",
                extra={
                    "event": "execution_worker_started",
                    "trading_mode": settings.trading_mode.value,
                    "live_trading": settings.live_trading,
                    "safe_mode": app.state.safe_mode.engaged,
                    # Named so the log answers "could this have reached a
                    # venue?" without anybody cross-referencing another file.
                    "order_managers": sorted(app.state.order_managers.managers),
                },
            )
        else:
            log.info(
                "the execution worker is registered and NOT started; no recorded "
                "signal will be acted on",
                extra={
                    "event": "execution_worker_idle",
                    "reason": (
                        "execution_worker_enabled is false"
                        if not settings.execution_worker_enabled
                        else "workers_enabled is false"
                    ),
                },
            )
        yield
        # Stop workers before the hub they publish onto, and the hub before
        # the bus it reads from.
        await app.state.workers.stop_all()
        # After the workers that publish, before the hub they publish onto.
        await app.state.notification_consumer.stop()
        await app.state.training.shutdown()
        await app.state.validation.shutdown()
        await app.state.replay.shutdown()
        await app.state.paper.shutdown()
        await app.state.brokers.disconnect_all()
        await app.state.hub.stop()
        await app.state.event_bus.close()
        await app.state.engine.dispose()
        log.info("shutdown", extra={"event": "shutdown"})

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.checks = checks
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.workers = registry
    app.state.event_bus = make_event_bus(settings.redis_url if settings.events_enabled else None)
    # One hub per process: N sockets share one bus reader rather than opening
    # N Redis subscriptions.
    app.state.hub = Hub(app.state.event_bus, settings.ws_max_connections_per_user)
    # Providers are registered, not connected. Constructing one opens no
    # terminal and makes no network call; each reports its own usability.
    app.state.market_data = default_service(settings.environment.value)
    # The registry is populated at import from a fixed tuple of classes. No
    # strategy is ever loaded from a name, a path or a payload.
    strategies = default_registry()
    app.state.strategy_engine = StrategyEngine(strategies, app.state.hub)
    # Runs in background tasks, bounded by a semaphore. A prefix-walk
    # backtest is CPU-bound and letting a user queue fifty is a denial of
    # service they did not intend.
    app.state.backtests = BacktestService(
        app.state.session_factory, app.state.market_data, strategies, app.state.hub
    )
    # Replay sessions live in background tasks, so closing a browser does
    # not stop one. The service holds no broker adapter and cannot reach a
    # venue.
    app.state.replay = ReplayService(
        app.state.session_factory, app.state.market_data, strategies, app.state.hub
    )
    # Paper bots also live in background tasks, for the same reason. The
    # service reaches `app.paper`, which holds no broker adapter and imports
    # none -- a test parses every module in the package to prove it. PAPER
    # routes to PAPER_EXECUTION_ONLY by a function that takes no configuration.
    from app.symbols import service as symbol_service

    # The central safety authority. It owns the effective limits, the kill
    # switches and the latched locks, and it FAILS CLOSED: a check that raises,
    # a configuration that will not load and a decision that cannot be
    # persisted all produce a refusal, never a pass.
    app.state.risk = RiskService(
        app.state.session_factory,
        live_trading=settings.live_trading,
        trading_mode=settings.trading_mode.value,
    )
    # Position sizing (L18). It proposes a quantity and holds no authority:
    # the engine is pure arithmetic and this service only counts and logs it.
    # It is constructed after `risk` deliberately, to read in the order the
    # pipeline runs -- risk vetoes, sizing measures, and risk vetoes again.
    app.state.sizing = SizingService()
    # The AI layer (L24). EMPTY at startup, and deliberately: a model is
    # registered by an explicit action, never loaded from a path or a payload,
    # and a deployment with none answers MODEL_UNAVAILABLE rather than a
    # default. Nothing in `app.ai` can reach risk, sizing, the OMS or an
    # adapter -- the dependency runs one way, from execution to ai.
    app.state.ai_models = ModelRegistry()
    # Training (L25). Background tasks bounded by a semaphore, on the same
    # pattern the backtester and replay have used since L14 -- section 4 asks
    # to reuse the existing job system rather than add a queue. It produces a
    # CANDIDATE and a draft model version; it cannot promote, deploy or trade,
    # and a test parses every module in `app.training` to keep that true.
    app.state.training = TrainingService(app.state.session_factory, app.state.hub)
    # Validation (L26). The same background-job shape again, for the same
    # reason. It reads a candidate and writes a `validation_runs` row; it does
    # not promote, activate, deploy or trade, and it never touches
    # `model_versions.status` -- a PASS makes a candidate eligible for
    # CONSIDERATION by the registry, which is a human decision at L28.
    app.state.validation = ValidationService(app.state.session_factory, app.state.hub)
    app.state.paper = PaperService(
        app.state.session_factory,
        app.state.market_data,
        strategies,
        symbol_service,
        app.state.hub,
        app.state.risk,
        # L27. The seat `app/execution/ai.py` has held since L16 is now
        # fillable: a strategy configured for an AI mode gets a filter built
        # from a VALIDATED model version. A strategy with no configuration runs
        # AI_DISABLED and behaves exactly as the deterministic strategy does.
        app.state.ai_models,
    )
    # The gateway records signals. It holds no broker credential and imports
    # no execution code; an accepted alert becomes a row, never an order.
    # One adapter per account, registered deliberately. Empty at startup:
    # constructing an adapter opens no terminal, and connecting one is an
    # operator action, not a side effect of the API booting.
    app.state.brokers = BrokerRegistry()
    # One order manager per account, beside the adapter it drives. Empty at
    # startup for the same reason `brokers` is: constructing a manager needs an
    # adapter, and registering an adapter is an operator action. Every
    # broker-bound order route refuses while it is empty, which is the honest
    # state of a deployment that has not been pointed at a venue.
    app.state.order_managers = OrderManagerRegistry()

    # The automated execution engine (L20). It ORCHESTRATES: every gate it
    # runs belongs to a component that already exists, and it holds no broker
    # adapter of its own. Registered and NOT started, for the same reason the
    # adapter registry is empty -- beginning to consume signals is an operator
    # decision, not a side effect of the process booting. `POST
    # /v1/execution/start` runs it, and it survives a closed browser from
    # there because it is a supervised worker rather than anything attached to
    # a request.
    # L38. The latch, built before the pipeline that consults it. It starts
    # OPEN: a process that has not run its startup sequence has not found a
    # reason to close it, and closing it pre-emptively would mean every boot
    # began in safe mode for no stated condition -- which section 18 calls
    # using safe mode to hide errors.
    app.state.safe_mode = SafeMode()
    app.state.recovery = RecoveryManager(app.state.safe_mode)

    # One store, shared by the pipeline that writes orders and the sweep that
    # settles them. Two `store_for` calls would be two symbol caches answering
    # the same question.
    app.state.order_store = order_store_for(app.state.session_factory)

    app.state.execution_pipeline = ExecutionPipeline(
        # L38's gate, in front of every existing one and never instead of one.
        safe_mode=app.state.safe_mode,
        managers=app.state.order_managers,
        # The engine's limits are replaced per pass by the worker's caller
        # once a bot configuration exists (L22). Until then it runs on the
        # account's stored configuration through RiskService, and a bare
        # engine here would be a second, looser copy -- so it is built from
        # the same defaults the service loads.
        risk=RiskEngine(RiskLimits()),
        spec_for=_spec_loader(app),
        strategy_state=_strategy_state(app),
        # L45 C-1. Without this the pipeline creates orders in memory and
        # writes none, so `orders.intent_id` UNIQUE guards nothing on the
        # automated path, startup reconciliation finds `unresolved=0`, safe
        # mode never latches, and an order whose venue state was never
        # established is re-sent after any restart. It is passed HERE, at the
        # one place the deployed pipeline is built.
        store=app.state.order_store,
        # L53. What the RiskEngine is evaluated against. Without it the engine
        # sees only equity -- which is None here -- so every portfolio-level
        # limit is unenforceable, including `one_position_per_symbol`, which is
        # on by default and could not fire.
        portfolio=portfolio_snapshot_for(app.state.session_factory),
        # L55. The caller this file's comment above has described since L22 and
        # which was never written. Without it the automated path evaluates
        # every signal against `RiskLimits()` -- 17 of 20 limits unset -- so an
        # operator's configured account limits apply to the API order path and
        # not to this one. It can only ever TIGHTEN: RiskService combines
        # account, strategy and symbol limits by taking the more restrictive.
        limits_for=risk_limits_loader_for(app.state.session_factory, app.state.risk),
    )
    app.state.execution = ExecutionWorker(
        app.state.session_factory,
        app.state.execution_pipeline,
        to_signal=_to_incoming_signal,
    )
    registry.register(app.state.execution)

    # L19's reconciler, on a loop for the first time. `OrderManager.reconcile`
    # was built, tested, and called from exactly one place: POST
    # /v1/orders/{id}/reconcile. So an order parked `unknown` at 02:00 blocked
    # its intent, its account's bot recovery and safe mode's reason list until
    # somebody woke up and posted.
    #
    # Registered and NOT started unless `oms_reconcile_enabled` says so, like
    # the execution worker and the bot supervisor. It sends nothing, but it
    # decides: a reconciliation that finds nothing at the venue writes
    # `failed`, the one state a fresh order for the same intent may follow.
    #
    # It is NOT a second reconciler. `app/recovery/reconciliation.py` counts
    # unresolved orders and deliberately settles none -- "settling an order is
    # an act against a venue and this module does not act" -- and that stays
    # true. This is a worker that calls the OMS's own method, and the manual
    # route stays as the on-demand equivalent, exactly as POST
    # /v1/bots/supervise sits beside the bot supervisor worker.
    app.state.oms_reconciler = OmsReconcileWorker(
        app.state.order_managers,
        app.state.order_store,
        interval_seconds=settings.oms_reconcile_interval_seconds,
        max_per_pass=settings.oms_reconcile_max_per_pass,
    )
    registry.register(app.state.oms_reconciler)

    # The bot supervisor (L22). It measures bot heartbeats rather than trusting
    # `bot_runs.status`, marks a silent run crashed, and considers recovery --
    # refusing every one of them while no safety check is wired, because the
    # absence of a check is not evidence that recovery is safe.
    #
    # Registered and NOT started, like the execution worker: beginning to
    # supervise is an operator action. `POST /v1/bots/supervise` runs one sweep
    # on demand in the meantime, which is what makes the mechanism usable
    # before anybody turns the loop on.
    # L38 fills the `SafetyCheck` seat L22 left open. L22's default refuses
    # everything, deliberately -- "the absence of a check is not evidence that
    # recovery is safe" -- and this does not lower that bar: it supplies the
    # conditions L22's docstring already named (a kill switch, an unresolved
    # order) plus safe mode, an unsettled position and an unusable adapter.
    # Every one is a refusal; none is an approval, and L22's own gates still
    # run afterwards.
    app.state.bot_supervisor = BotSupervisorWorker(
        app.state.session_factory,
        safe_to_recover=bot_recovery_gate(
            app.state.session_factory,
            safe_mode=app.state.safe_mode,
            risk=app.state.risk,
            brokers=app.state.brokers,
        ),
    )
    registry.register(app.state.bot_supervisor)

    # L21's position monitor, finally given a host. It was written, tested and
    # never constructed: `grep PositionMonitor app/` found the class, its own
    # module and a mention in a docstring, and nothing else. So `PolicySet`
    # had no production caller -- the one `PositionManager` this platform
    # built called `close_now`, which takes the caller's decision and never
    # consults a policy -- and seven of the nine exit policies could not fire
    # at all.
    #
    # Registered and NOT started, like the execution worker and for a sharper
    # reason: this one CLOSES positions. `POST /v1/positions/sweep` runs one
    # pass on demand, which is what makes the mechanism usable and testable
    # before anybody turns the loop on.
    #
    # Every exit it could apply is OFF unless a setting turns it on, and that
    # is the repository's own measurement rather than caution -- see
    # `app/positions/wiring.py` and `reports/exit_search_d1.json`.
    app.state.position_monitor = monitor_for(
        sessions=app.state.session_factory,
        managers=app.state.order_managers,
        risk=app.state.risk,
        market_data=app.state.market_data,
        brokers=app.state.brokers,
        hub=app.state.hub,
        settings=settings,
    )
    registry.register(app.state.position_monitor)
    app.state.webhook_gateway = WebhookGateway(
        secret=settings.tv_webhook_secret,
        mode=settings.trading_mode.value,
        max_age_seconds=settings.tv_webhook_max_age_seconds,
        future_tolerance_seconds=settings.tv_webhook_future_tolerance_seconds,
        restrict_to_tradingview_ips=settings.tv_webhook_restrict_ips,
    )
    app.state.rate_limiter = make_rate_limiter(
        settings.redis_url if settings.rate_limit_shared else None
    )

    # ============================================================ notifications
    #
    # L34. One registry of channel adapters, read by the API, the delivery
    # worker and the health surface, so all three answer from the same object
    # rather than each deciding for itself whether email is configured.
    # Constructing an adapter opens no socket and authenticates nothing -- the
    # rule the broker registry has followed since L10, and for the same reason:
    # a process that reached out to three providers on startup would fail to
    # start because one of them was down.
    app.state.notification_channels = build_registry(settings, app.state.hub)
    app.state.notifications = NotificationService(
        app.state.notification_channels, base_url=settings.app_base_url
    )
    # A second SUBSCRIBER on the one bus, not a second bus. The hub fans events
    # out to browsers and is deliberately incapable of writing anything; this
    # reads the same stream and persists notifications. See
    # `app/notifications/worker.py` for why they are not the same object.
    app.state.notification_consumer = NotificationConsumer(
        app.state.event_bus, app.state.session_factory, app.state.notifications
    )
    app.state.notification_delivery = NotificationDeliveryWorker(
        app.state.session_factory, app.state.notifications
    )
    registry.register(app.state.notification_delivery)

    # L04 wrote `ResetDelivery` and said its implementation "needs the
    # notification engine (level 34)". This is that level. With no SMTP host
    # configured the port still refuses exactly as `UnconfiguredDelivery` did,
    # so a deployment that has not set one behaves as it did before.
    _email = provider_from(settings)
    app.state.reset_delivery = (
        NotificationResetDelivery(_email, base_url=settings.app_base_url)
        if _email.configured
        else UnconfiguredDelivery()
    )

    # ============================================================= observability
    #
    # L37. One collection pass, read by the summary, the component list, the
    # metrics and the admin panel -- four independent probes would be four
    # answers to "is the database up", and the one on the dashboard would
    # eventually disagree with the one the orchestrator restarted on.
    #
    # It reuses L02's check functions rather than probing again, L02's worker
    # base for its loop, L05's `system_events` table for its incident history
    # and L07's bus for its one alert. It imports no risk engine, no order
    # manager and no adapter class -- a test parses the package to keep that
    # true, because a monitor that can act is a monitor that can act wrongly.
    app.state.observability = ObservabilityService(thresholds=monitoring_thresholds(settings))
    app.state.monitoring_worker = MonitoringWorker(
        app.state.session_factory, app.state.observability, app
    )
    registry.register(app.state.monitoring_worker)

    # L39. Step-up grants live in the process for the reason L38's safe-mode
    # latch does: a five-minute credential that survives a restart is a
    # credential nobody revoked. One API process holds them; a second needs a
    # shared store, which SECURITY.md records as a known limit.
    app.state.step_up = StepUp(ttl_seconds=float(settings.step_up_ttl_seconds))
    # The producer for SECURITY_ALERT and ACCOUNT_SECURITY_ALERT. It publishes
    # onto the L07 hub, which `NotificationConsumer` already reads, so a
    # security event becomes a notification by the path every other event
    # takes -- no second bus and no direct call into the notification service.
    app.state.security_announcer = SecurityAnnouncer(hub=app.state.hub)

    errors.install(app)
    # Ordering: CSRF runs inside the request-id middleware so a rejection is
    # still correlated, and outside the routers so no handler sees an
    # unverified state-changing request.
    app.middleware("http")(
        make_csrf_middleware(settings.session_cookie_name, settings.csrf_enabled)
    )
    # L39. Added last, so it is the OUTERMOST middleware: a 403 from the CSRF
    # wall and a 500 from the error handler are the responses an attacker sees
    # most, and they must carry the headers too.
    #
    # `add_middleware` with a pure-ASGI class rather than `app.middleware("http")`.
    # See `SecurityHeadersMiddleware`: a third `BaseHTTPMiddleware` measurably
    # worsened WebSocket flakiness, and this one adds no task group and skips
    # non-HTTP scopes entirely.
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    # The versioned surface is the documented one. The unprefixed auth and
    # admin routers stay mounted at their original paths, hidden from the
    # schema, so a caller written before /v1 keeps working; they are the same
    # router objects, so there is one implementation behind both paths.
    app.include_router(build_v1_router())
    app.include_router(auth_router, include_in_schema=False, deprecated=True)
    app.include_router(admin_router, include_in_schema=False, deprecated=True)
    app.include_router(legacy_protected_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        # L39. Explicit rather than `*`: with credentials, a wildcard permits
        # any header any origin cares to send, which is wider than anything
        # the frontend asks for. The list is `ALLOWED_REQUEST_HEADERS`.
        allow_headers=ALLOWED_REQUEST_HEADERS,
        expose_headers=[errors.REQUEST_ID_HEADER],
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        """Mode and identity. No dependency is touched and no secret is shown.

        L41 added `release`: which build is serving this request. Before it,
        `/health` reported a version string nobody had changed since L02, so
        "is the fix deployed?" and "did the rollback take?" were both
        unanswerable from outside the container.
        """
        return {
            "status": "ok",
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            **settings.public_summary(),
            "release": describe_release(settings),
            "workers_in_process": settings.workers_enabled,
        }

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        """Liveness only: the process is running. Touches no dependency."""
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, object]:
        results = await run_checks(app.state.checks)
        overall = overall_status(results)
        if overall is HealthStatus.unavailable:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": str(overall),
            "critical_unavailable": sorted(
                r.name
                for r in results.values()
                if r.critical and r.status is HealthStatus.unavailable
            ),
            "checks": {name: r.as_dict() for name, r in results.items()},
        }

    @app.get("/health/trading")
    async def trading_ready(response: Response) -> dict[str, object]:
        """Step 22. A THIRD state, and the distinction is the point.

        `/health/live` says the process is running. `/health/ready` says its
        dependencies answer, which is what a load balancer needs. Neither says
        the platform should be allowed to trade -- a process can be perfectly
        alive and perfectly ready while safe mode is latched, the market data is
        stale, or an order's venue state was never established.

        **This is a report, not a gate**, and that is deliberate rather than a
        shortcut. Nothing consults it before trading: the RiskEngine vetoes, the
        OMS reconciles, safe mode refuses at gate zero, and the adapter is not
        registered. If this endpoint returned SAFE while the risk engine was
        blocking, the platform still would not trade -- which is the property
        that makes it safe to compute a summary at all. A deployment that used
        this as its permission to trade would be trusting a summary over the
        gates, and the gates are the thing that has been tested.

        **It reveals a verdict and a count, never the reasons.** This route is
        unauthenticated, like the rest of `/health`, and the reasons name
        components and their states -- which is an operational map. The reasons
        are served by `GET /v1/monitoring/summary`, which requires a session.

        503 when trading is blocked, so a deployment script can gate a
        smoke test on it without parsing the body.
        """
        safe_mode = getattr(app.state, "safe_mode", None)
        engaged = bool(safe_mode.engaged) if safe_mode is not None else False

        observability = getattr(app.state, "observability", None)
        verdict = "UNKNOWN"
        blockers = 0
        if observability is not None and observability.last is not None:
            verdict = str(observability.last.safety)
            blockers = len(observability.last.safety_reasons)
        elif observability is not None:
            # Never collected yet. UNKNOWN is the honest answer and is NOT
            # upgraded to SAFE by the absence of a finding -- the same rule
            # L37 applies to a component nobody could observe.
            verdict = "UNKNOWN"

        allowed = settings.live_execution_allowed
        blocked = engaged or verdict in ("BLOCKED", "UNKNOWN")
        if blocked:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "trading": "BLOCKED" if blocked else verdict,
            "safe_mode": engaged,
            "blocker_count": blockers,
            "live_execution_allowed": allowed,
            "trading_mode": settings.trading_mode.value,
            "detail": (
                "Reasons are served by GET /v1/monitoring/summary, which needs a "
                "session: they name components and their states, and this route "
                "is unauthenticated."
            ),
        }

    return app


app = create_app()
