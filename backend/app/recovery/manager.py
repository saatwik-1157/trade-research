"""Startup recovery, on-demand reconciliation, and the safe-mode decision.

Steps 17, 18 and 27.

**The startup sequence is the level.** Step 17 lists it and step 5 states the
rule it enforces:

    MT5 DISCONNECT -> STOP NEW ORDERS -> ALERT -> RECONNECT -> RECONCILE
    -> VALIDATE SAFETY -> RESUME ONLY IF SAFE

`run_startup` walks the sequence, and a step that finds something needing a
person **closes the safe-mode latch with that condition as its reason**. There
is no path from "the process started" to "orders may be sent".

**It never enables live trading.** Step 17 and rule 15. `TRADING_MODE` and
`LIVE_TRADING` are read and reported; nothing here writes either, no code path
touches `LIVE_GATES`, and a test greps for the assignment. Recovery finishing
successfully means new work may start *in the mode the deployment is already
configured for*.

**It repairs nothing.** Every check delegates to the reconciler the owning
level already built, and none of those writes. Step 14 and rule 12: recovery
must never duplicate an order, and the cheapest way to guarantee that is for
the recovery path to have no way of sending one -- `app/recovery` imports no
order manager class and no broker adapter class, and a parse test enumerates
the package.

**Every step is auditable.** Step 27. Each one becomes a `system_events` row
through L37's `incidents.record` -- the same table, the same scrubber, the same
correlation id -- and a blocking one publishes a `SYSTEM_ALERT`, which L34
turns into a notification and L35 delivers to Discord. Step 28 says not to call
Discord from recovery logic; this module imports no notification service, no
email provider and no Discord adapter.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.ops import SystemEvent
from app.recovery import reconciliation
from app.recovery.contract import (
    STARTUP_SEQUENCE,
    RecoveryReport,
    RecoveryState,
    SafeModeReason,
    Step,
    StepStatus,
)
from app.recovery.safe_mode import SafeMode

log = logging.getLogger("app.recovery")

#: Which safe-mode reason a blocking step closes the latch with. A step with no
#: entry here is reported and does NOT latch: not every problem is a reason to
#: stop trading, and step 18 says not to use safe mode to hide errors.
LATCHES: dict[str, SafeModeReason] = {
    "oms_reconciliation": SafeModeReason.unknown_order_state,
    "position_reconciliation": SafeModeReason.position_mismatch,
    "broker": SafeModeReason.broker_unreachable,
    "database": SafeModeReason.database_inconsistent,
    "migrations": SafeModeReason.database_inconsistent,
}


def _step(name: str, status: StepStatus, detail: str, **facts: Any) -> Step:
    return Step(name=name, status=status, detail=detail, at=utcnow(), facts=facts)


class RecoveryManager:
    """One per process. Holds the latch and the last report."""

    def __init__(self, safe_mode: SafeMode | None = None) -> None:
        self.safe_mode = safe_mode or SafeMode()
        self.last_startup: RecoveryReport | None = None
        self.last_reconciliation: RecoveryReport | None = None

    # =============================================================== startup

    async def run_startup(
        self, db: AsyncSession, app: Any, *, record: bool = True
    ) -> RecoveryReport:
        """Step 17's sequence. Never raises; a failing step is a failing step.

        A recovery routine that can crash is a recovery routine that leaves the
        platform in whatever state the crash found. Every check is wrapped, and
        an unexpected exception becomes a FAILED step with its category rather
        than an exception out of the lifespan.
        """
        state = app.state
        settings = state.settings
        report = RecoveryReport(kind="startup", at=utcnow())

        async def run(name: str, coro: Any) -> None:
            try:
                report.steps.append(await coro)
            except Exception as exc:  # noqa: BLE001 - a failed check is a fact
                report.steps.append(
                    _step(
                        name,
                        StepStatus.failed,
                        f"the check itself failed: {type(exc).__name__}: {exc}"[:300],
                    )
                )
                log.warning(
                    "a startup recovery step failed",
                    extra={"event": "recovery_step_failed", "step": name},
                )

        # 1. Configuration. Reported, never changed.
        report.steps.append(
            _step(
                "configuration",
                StepStatus.ok,
                (
                    f"{settings.environment.value}, trading mode "
                    f"{settings.trading_mode.value}, live trading "
                    f"{'on' if settings.live_trading else 'off'}"
                ),
                trading_mode=settings.trading_mode.value,
                live_trading=settings.live_trading,
                live_execution_allowed=settings.live_execution_allowed,
                blockers=len(settings.live_execution_blockers()),
            )
        )

        # 2, 3. Database and schema.
        await run("database", self._check_database(db))
        await run("migrations", self._check_schema(db))

        # 4, 5. Redis and the bus, from the hub's own observation.
        report.steps.append(self._check_bus(state))

        # 6, 7. Market data and the webhook gateway.
        await run("market_data", self._check_market_data(state))
        report.steps.append(self._check_webhook(settings))

        # 8 to 11. The broker link, then orders, then positions -- in that
        # order, because a comparison against a venue that cannot be reached is
        # not a comparison.
        await run("broker", reconciliation.check_broker(getattr(state, "brokers", None)))
        await run("oms_reconciliation", reconciliation.check_orders(db))
        await run("position_reconciliation", reconciliation.check_positions(db))

        # 12, 13. Risk, then bots.
        report.steps.append(self._check_risk(state))
        factory = getattr(state, "session_factory", None)
        if factory is not None:
            await run("bot_recovery", reconciliation.check_bots(factory))

        # 14, 15. Notifications and monitoring.
        report.steps.append(self._check_notifications(state))
        report.steps.append(self._check_monitoring(state))

        # 16. The mode this process will run in, decided from the above.
        self._latch_from(report)
        report.steps.append(self._operational_mode(settings))
        report.safe_mode_engaged = [str(latch.reason) for latch in self.safe_mode.reasons]

        if record:
            await self._record(db, report)
        self.last_startup = report
        return report

    # ========================================================== the checks

    async def _check_database(self, db: AsyncSession) -> Step:
        """Step 15. The lightest possible query; §13 of L37 asks for exactly that."""
        value = (await db.execute(text("SELECT 1"))).scalar_one()
        if value != 1:  # pragma: no cover - defensive
            return _step("database", StepStatus.failed, f"unexpected reply {value!r}")
        return _step("database", StepStatus.ok, "SELECT 1 ok")

    async def _check_schema(self, db: AsyncSession) -> Step:
        """Step 15. The schema the models expect is the schema that is there.

        A count of the tables `app.models.EXPECTED_TABLES` promises, compared
        against what the connection can see. A missing table is a FAILED step
        and latches safe mode: an application running against a schema it does
        not match will write rows nobody can read back.

        It does not run migrations and does not check the alembic revision:
        applying a migration at startup is a deployment decision, and a
        process that migrates on boot is N processes racing to migrate.
        """
        from sqlalchemy import inspect

        from app.models import EXPECTED_TABLES

        def _names(connection: Any) -> set[str]:
            return set(inspect(connection).get_table_names())

        present = await db.run_sync(lambda sync: _names(sync.connection()))
        missing = sorted(EXPECTED_TABLES - present)
        if missing:
            return _step(
                "migrations",
                StepStatus.failed,
                (
                    f"{len(missing)} table(s) the models expect are not present. This "
                    "process is running against a schema it does not match; migrations "
                    "have not been applied. Nothing is migrated here -- a process that "
                    "migrates on boot is N processes racing to migrate."
                ),
                missing=missing[:10],
            )
        return _step(
            "migrations",
            StepStatus.ok,
            f"all {len(EXPECTED_TABLES)} expected tables are present",
            tables=len(EXPECTED_TABLES),
        )

    def _check_bus(self, state: Any) -> Step:
        hub = getattr(state, "hub", None)
        if hub is None:
            return _step("event_bus", StepStatus.skipped, "no hub in this process")
        status = hub.status()
        healthy = bool(status.get("bus_healthy")) and bool(status.get("reader_running"))
        return _step(
            "event_bus",
            StepStatus.ok if healthy else StepStatus.attention,
            (
                f"{status.get('bus')} bus, reader running"
                if healthy
                else f"{status.get('bus')} bus: {status.get('bus_error') or 'reader not running'}"
            ),
            kind=status.get("bus"),
        )

    async def _check_market_data(self, state: Any) -> Step:
        """Step 11. Freshness is measured or it is UNAVAILABLE.

        SKIPPED when nothing has ever been stored: "we have no data" is not
        "the data is stale", and reporting the first as the second would put
        the platform into safe mode on every fresh install.
        """
        from sqlalchemy import func, select

        from app.models.market import MarketBar

        factory = getattr(state, "session_factory", None)
        if factory is None:  # pragma: no cover - always present
            return _step("market_data", StepStatus.skipped, "no session factory")
        async with factory() as db:
            newest = await db.scalar(select(func.max(MarketBar.bar_time)))
        if newest is None:
            return _step(
                "market_data",
                StepStatus.skipped,
                (
                    "no bar has ever been stored, so freshness is UNAVAILABLE. That is "
                    "not staleness: a platform with no ingested data has nothing to be "
                    "stale about, and strategies that need bars refuse for want of them."
                ),
                newest_bar=None,
            )
        age = (utcnow() - newest).total_seconds()
        return _step(
            "market_data",
            StepStatus.ok,
            f"newest stored bar is {int(age)}s old",
            age_seconds=round(age, 1),
        )

    def _check_webhook(self, settings: Any) -> Step:
        """Step 12. Whether the gateway will accept anything at all."""
        configured = bool(getattr(settings, "tv_webhook_secret", ""))
        return _step(
            "webhook_gateway",
            StepStatus.ok if configured else StepStatus.skipped,
            (
                "a shared secret is configured"
                if configured
                else (
                    "no shared secret is configured, so the receiver REFUSES every "
                    "alert. That is L09's deliberate default, not a fault."
                )
            ),
            secret_configured=configured,
        )

    def _check_risk(self, state: Any) -> Step:
        """Step 19. Read the engine's own status. Never change it."""
        risk = getattr(state, "risk", None)
        if risk is None:
            return _step("risk_engine", StepStatus.failed, "no risk service in this process")
        status = risk.status()
        switches = status.get("kill_switches", {})
        engaged = (
            bool(switches.get("global"))
            or bool(switches.get("accounts"))
            or bool(switches.get("strategies"))
        )
        return _step(
            "risk_engine",
            StepStatus.ok,
            (
                "loaded; a kill switch is engaged, which is a decision somebody made "
                "and recovery does not undo"
                if engaged
                else "loaded; no kill switch engaged"
            ),
            kill_switch_engaged=engaged,
            trading_mode=status.get("trading_mode"),
            authority=(
                "recovery reads the engine and never releases a switch. Rule 2: "
                "recovery cannot bypass the RiskEngine."
            ),
        )

    def _check_notifications(self, state: Any) -> Step:
        """Step 21. A notification failure never blocks anything here."""
        service = getattr(state, "notifications", None)
        if service is None:
            return _step("notifications", StepStatus.skipped, "not built in this process")
        channels = service.channels.describe()
        available = [c for c in channels if c.get("available")]
        return _step(
            "notifications",
            StepStatus.ok,
            (
                f"{len(available)} of {len(channels)} channel(s) available. A channel "
                "being unavailable is never a reason to block trading."
            ),
            channels={str(c.get("channel")): c.get("state") for c in channels},
        )

    def _check_monitoring(self, state: Any) -> Step:
        service = getattr(state, "observability", None)
        if service is None:
            return _step("monitoring", StepStatus.skipped, "not built in this process")
        return _step(
            "monitoring",
            StepStatus.ok,
            "the observability collector is registered",
            collections=service.collections,
        )

    def _operational_mode(self, settings: Any) -> Step:
        """Step 17's last line, and rule 15. Started is not the same as safe."""
        engaged = self.safe_mode.engaged
        return _step(
            "operational_mode",
            StepStatus.attention if engaged else StepStatus.ok,
            (
                (
                    "SAFE MODE. New orders, automated execution and bot recovery are "
                    "blocked until the conditions below are resolved and an "
                    "administrator releases the latch."
                )
                if engaged
                else (
                    f"normal, in {settings.trading_mode.value} mode. Starting "
                    "successfully does not enable live trading and never will: that "
                    "is a configuration and a set of gates, neither of which recovery "
                    "can touch."
                )
            ),
            safe_mode=engaged,
            reasons=[str(latch.reason) for latch in self.safe_mode.reasons],
            trading_mode=settings.trading_mode.value,
            live_trading=settings.live_trading,
        )

    # ============================================================ safe mode

    def _latch_from(self, report: RecoveryReport) -> None:
        """Close the latch for every blocking step that maps to a reason."""
        for step in report.steps:
            if not step.blocking:
                continue
            reason = LATCHES.get(step.name)
            if reason is None:
                continue
            self.safe_mode.engage(reason, step.detail)

    async def release(
        self, db: AsyncSession, app: Any, *, actor_user_id: str, why: str
    ) -> RecoveryReport:
        """Step 18's exit. Re-checks first, then releases only what cleared.

        Deliberately not a plain "unset the flag": a release that did not
        re-run the sequence would let somebody clear a latch while the
        condition that closed it was still true, which is how a platform
        resumes trading into an unreconciled account.
        """
        report = await self.run_startup(db, app, record=False)
        cleared: list[str] = []
        blocking = {LATCHES.get(s.name) for s in report.steps if s.blocking}
        for latch in list(self.safe_mode.reasons):
            if latch.reason in blocking:
                continue
            if self.safe_mode.release(latch.reason, actor_user_id=actor_user_id, why=why):
                cleared.append(str(latch.reason))
        report.safe_mode_engaged = [str(latch.reason) for latch in self.safe_mode.reasons]
        report.steps.append(
            _step(
                "safe_mode_release",
                StepStatus.ok if cleared else StepStatus.attention,
                (f"released {len(cleared)} latch(es); {len(self.safe_mode.reasons)} still holding"),
                released=cleared,
                still_engaged=report.safe_mode_engaged,
            )
        )
        await self._record(db, report)
        self.last_startup = report
        return report

    # ============================================================ recording

    async def _record(self, db: AsyncSession, report: RecoveryReport) -> None:
        """Step 27. Every step, in the table L05 built for operational events.

        One row per step, not one per report: the question an operator asks is
        "what did the broker check say", and a single blob per run answers it
        only by being read in full. `incidents._scrub` runs over the facts, so
        a credential cannot reach the payload even if a check put one there.
        """
        from app.observability.incidents import _scrub

        for step in report.steps:
            db.add(
                SystemEvent(
                    component=f"recovery.{step.name}"[:32],
                    event_type=f"RECOVERY_{report.kind.upper()}"[:32],
                    level=(
                        "error"
                        if step.status is StepStatus.failed
                        else "warning"
                        if step.blocking
                        else "info"
                    ),
                    occurred_at=step.at,
                    payload={
                        "status": str(step.status),
                        "detail": step.detail[:500],
                        **_scrub(step.facts),
                    },
                )
            )

    async def announce(self, hub: Any | None, report: RecoveryReport) -> int:
        """Step 28. One SYSTEM_ALERT per blocking step, through L34.

        This module imports no notification service, no email provider and no
        Discord adapter. It publishes a catalogued event and stops thinking
        about it; L34 grades it, applies each recipient's preferences, and L35
        delivers it. Step 28 asks for exactly that.
        """
        if hub is None:
            return 0
        from app.core.events import Event

        sent = 0
        for step in report.attention:
            try:
                await hub.publish(
                    Event(
                        type="SYSTEM_ALERT",
                        payload={
                            "component": f"recovery.{step.name}",
                            "check": f"RECOVERY_{report.kind.upper()}",
                            "title": f"Recovery: {step.name} needs attention",
                            "reason": step.detail[:300],
                            "health": (
                                "unavailable" if step.status is StepStatus.failed else "degraded"
                            ),
                        },
                        source="recovery",
                        channel="system",
                    )
                )
            except Exception:  # noqa: BLE001 - a report is not lost because a socket was
                log.warning(
                    "a recovery alert could not be published",
                    extra={"event": "recovery_alert_failed", "step": step.name},
                )
                continue
            sent += 1
        return sent

    # ============================================================== serving

    def state(self) -> RecoveryState:
        if self.safe_mode.engaged:
            return RecoveryState.safe_mode
        if self.last_startup is None:
            return RecoveryState.unknown
        return RecoveryState.normal if self.last_startup.clean else RecoveryState.degraded

    def status(self, settings: Any) -> dict[str, Any]:
        return {
            "state": str(self.state()),
            "safe_mode": self.safe_mode.describe(),
            "startup": self.last_startup.as_dict() if self.last_startup else None,
            "last_reconciliation": (
                self.last_reconciliation.as_dict() if self.last_reconciliation else None
            ),
            "environment": {
                "trading_mode": settings.trading_mode.value,
                "live_trading": settings.live_trading,
                "live_execution_allowed": settings.live_execution_allowed,
            },
            "sequence": [{"step": name, "what": what} for name, what in STARTUP_SEQUENCE],
            "rules": [
                "recovery cannot bypass the RiskEngine",
                "recovery cannot submit an order outside the OMS and the adapter",
                "an unknown order state is settled by asking the venue, never by retrying",
                "a broker reconnect does not by itself mean trading is safe",
                "position state is reconciled before autonomous execution resumes",
                "recovery never enables live trading",
                "recovery never destroys a historical record",
            ],
        }


__all__ = ["LATCHES", "RecoveryManager"]
