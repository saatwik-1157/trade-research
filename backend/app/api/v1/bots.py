"""The bot manager's surface: lifecycle, health, limits, supervision.

**A control plane, not an execution engine.** Nothing here evaluates a
strategy, sizes a position or submits an order. It moves a bot between states,
reports what the supervisor measured, and asks the runner that owns the bot to
start or stop it. The trading path is unchanged and unreachable from here:
bot -> strategy -> AI -> risk -> sizing -> OMS -> adapter.

**Stopping a bot does not close its positions.** Section 24 and section 32 say
so, and the routes say so in their own descriptions, because an operator who
believes STOP is a flatten button will eventually press it expecting one. A
stopped bot creates no new trades; everything it already has stays under the
position manager.

**Health is measured, not read.** `GET /bots/{id}/health` compares the run's
heartbeat against the clock rather than reporting `status`, because "the
database says RUNNING" is not evidence that anything is running — which is the
sentence the whole supervisor exists to act on.

**Live is not reachable from here.** A bot whose mode is `live` cannot be
started while `LIVE_TRADING` is false, and the refusal names the setting
rather than the request, because a caller retrying with different JSON should
learn that the answer will not change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.bots.limits import BotCounters, BotLimits
from app.bots.limits import check as check_limits
from app.bots.state import ACTIVE, BotState
from app.bots.supervisor import DEFAULT_STALE_AFTER, BotSupervisor
from app.core import audit
from app.core.errors import Conflict, NotFound, request_id_of
from app.models.accounts import BrokerAccount
from app.models.bots import Bot, BotEvent, BotRun
from app.models.execution import Position, Trade
from app.models.strategies import StrategyVersion
from app.security.enforce import announce, require_step_up
from app.security.events import SecurityEvent
from app.security.events import record as security_record
from app.security.stepup import StepUpScope

router = APIRouter(prefix="/bots", tags=["bots"])

# Reading a bot is a TRADER action, not a USER one. `RESOURCE_MIN_ROLE["bots"]`
# says so, the 501 stub this group replaced enforced it, and a bot row carries
# its mode, its limits and why it last stopped -- the operational state of an
# automated trader, which is not the "read-only observer of results" surface a
# new account is given. Reads and writes therefore ask for the same permission;
# there is no `view_bots`, and inventing one to widen this would be adding a
# permission in order to grant access rather than to describe it.
_VIEW = Depends(require_permission(Permission.manage_bots))
_MANAGE = Depends(require_permission(Permission.manage_bots))


# ==================================================================== bodies


class CreateBrokerBotBody(BaseModel):
    """Point one strategy version at one broker account."""

    name: Annotated[str, Field(min_length=1, max_length=100)]
    strategy_version_id: Annotated[str, Field(min_length=1, max_length=36)]
    #: A `broker_accounts.id`, which venue registration writes. Not the label an
    #: operator registered the adapter under -- see `positions.broker_account_id`
    #: and the three identifier confusions that column's history records.
    broker_account_id: Annotated[str, Field(min_length=1, max_length=36)]
    #: Required, not defaulted. `route_for` hands this bot every signal for its
    #: strategy version, and a bot with no per-trade budget is one whose size is
    #: decided by whatever the account limits happen to allow.
    max_risk_per_trade: Annotated[Decimal, Field(gt=0)]
    max_positions: Annotated[int, Field(ge=1)] | None = None
    max_daily_trades: Annotated[int, Field(ge=1)] | None = None
    max_daily_loss: Annotated[Decimal, Field(gt=0)] | None = None
    cooldown_seconds: Annotated[int, Field(ge=0)] | None = None
    reason: Annotated[str, Field(min_length=8, max_length=300)]


class LimitsBody(BaseModel):
    """Per-bot limits. Every field optional; null means this bot states nothing.

    None of these can loosen the account's limits — the manager combines the
    two and takes the more restrictive, so a bot asking for more than its
    account permits gets the account's figure rather than its own.
    """

    max_positions: int | None = Field(default=None, ge=1)
    max_daily_trades: int | None = Field(default=None, ge=1)
    max_daily_loss: Decimal | None = Field(default=None, gt=0)
    max_risk_per_trade: Decimal | None = Field(default=None, gt=0)
    cooldown_seconds: int | None = Field(default=None, ge=0)


class DisableBody(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


# =================================================================== helpers


async def _owned(db: AsyncSession, bot_id: str, user: User) -> Bot:
    row = await db.get(Bot, bot_id)
    if row is None or row.user_id != user.id:
        # A bot belonging to somebody else is reported as absent rather than
        # forbidden: "this id exists but is not yours" is itself information.
        raise NotFound(f"no bot {bot_id}")
    return row


async def _latest_run(db: AsyncSession, bot_id: str) -> BotRun | None:
    return await db.scalar(
        select(BotRun).where(BotRun.bot_id == bot_id).order_by(BotRun.started_at.desc()).limit(1)
    )


def _limits_of(bot: Bot) -> BotLimits:
    return BotLimits(
        max_positions=bot.max_positions,
        max_daily_trades=bot.max_daily_trades,
        max_daily_loss=bot.max_daily_loss,
        max_risk_per_trade=bot.max_risk_per_trade,
        cooldown_seconds=bot.cooldown_seconds,
    )


async def _counters(db: AsyncSession, bot: Bot, *, now: datetime) -> BotCounters:
    """What this bot has actually done today, measured from the record.

    Read rather than cached: a counter kept in memory comes back as zero after
    a restart, and a bot that had used nine of ten daily trades would then take
    another ten.
    """
    account = bot.paper_account_id or bot.broker_account_id
    if account is None:
        return BotCounters()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    open_rows = (
        await db.scalars(
            select(Position).where(
                Position.status.in_(("open", "partially_closed")),
                (Position.paper_account_id == account) | (Position.broker_account_id == account),
            )
        )
    ).all()
    trades = (
        await db.scalars(select(Trade).where(Trade.closed_at >= day, Trade.mode == bot.mode))
    ).all()
    realised = sum((t.net_profit or Decimal("0") for t in trades), Decimal("0"))
    last = max((t.closed_at for t in trades), default=None)
    return BotCounters(
        open_positions=len(open_rows),
        trades_today=len(trades),
        realised_today=realised,
        last_trade_at=last,
    )


def _runner(request: Request):  # noqa: ANN202
    """The paper runner, which is the only runner that exists today.

    A demo or live bot has no runner yet: the OMS and the broker executor are
    built, but nothing drives a strategy loop against them. Rather than
    pretending, `start` refuses for those modes and names what is missing.
    """
    return getattr(request.app.state, "paper", None)


def _snapshot(bot: Bot, run: BotRun | None, *, now: datetime) -> dict[str, Any]:
    state = BotState(run.status) if run is not None else None
    beat = run.last_heartbeat_at if run is not None else None
    age = None
    if beat is not None:
        aware = beat.replace(tzinfo=UTC) if beat.tzinfo is None else beat
        age = round((now - aware).total_seconds(), 1)
    return {
        "bot_id": bot.id,
        "name": bot.name,
        "mode": bot.mode,
        "enabled": bot.is_enabled,
        "disabled": bot.is_disabled,
        "disabled_reason": bot.disabled_reason,
        "strategy_version_id": bot.strategy_version_id,
        "paper_account_id": bot.paper_account_id,
        "broker_account_id": bot.broker_account_id,
        "limits": _limits_of(bot).as_dict(),
        "run_id": run.id if run else None,
        "status": str(state) if state else None,
        "started_at": run.started_at.isoformat() if run and run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run and run.ended_at else None,
        "stop_reason": run.stop_reason if run else None,
        "last_heartbeat_at": beat.isoformat() if beat else None,
        # Measured, not read. A run whose row says RUNNING and whose heartbeat
        # stopped is the case the supervisor exists for.
        "heartbeat_age_seconds": age,
        "heartbeat_stale": (
            bool(state in ACTIVE and age is not None and age > DEFAULT_STALE_AFTER.total_seconds())
            if state
            else False
        ),
    }


# ===================================================================== routes


@router.get(
    "",
    summary="Every bot this user owns, with its latest run and measured health",
)
async def list_bots(db: AsyncSession = Depends(get_db), user: User = _VIEW) -> dict[str, Any]:
    now = datetime.now(UTC)
    bots = list(
        (await db.scalars(select(Bot).where(Bot.user_id == user.id).order_by(Bot.created_at))).all()
    )
    items = [_snapshot(bot, await _latest_run(db, bot.id), now=now) for bot in bots]
    running = sum(1 for i in items if i["status"] == str(BotState.running))
    return {
        "items": items,
        "counts": {
            "total": len(items),
            "running": running,
            "paused": sum(1 for i in items if i["status"] == str(BotState.paused)),
            "crashed": sum(1 for i in items if i["status"] == str(BotState.crashed)),
            "disabled": sum(1 for i in items if i["disabled"]),
            "heartbeat_stale": sum(1 for i in items if i["heartbeat_stale"]),
        },
    }


@router.get("/{bot_id}", summary="One bot: configuration, run, limits, health")
async def get_bot(
    bot_id: str, db: AsyncSession = Depends(get_db), user: User = _VIEW
) -> dict[str, Any]:
    now = datetime.now(UTC)
    bot = await _owned(db, bot_id, user)
    run = await _latest_run(db, bot_id)
    counters = await _counters(db, bot, now=now)
    verdict = check_limits(_limits_of(bot), counters, now=now)
    return {
        **_snapshot(bot, run, now=now),
        "counters": counters.as_dict(),
        "may_open_a_new_trade": verdict.as_dict(),
        "note": (
            "Bot limits restrict; they never widen the account's. A bot that may not "
            "open a new trade still has its open positions managed by the position "
            "manager."
        ),
    }


@router.get("/{bot_id}/events", summary="This bot's activity log, newest first")
async def bot_events(
    bot_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _VIEW,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    await _owned(db, bot_id, user)
    runs = [r for r in (await db.scalars(select(BotRun.id).where(BotRun.bot_id == bot_id))).all()]
    if not runs:
        return {"items": []}
    rows = (
        await db.scalars(
            select(BotEvent)
            .where(BotEvent.bot_run_id.in_(runs))
            .order_by(BotEvent.occurred_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "id": e.id,
                "run_id": e.bot_run_id,
                "type": e.event_type,
                "level": e.level,
                "at": e.occurred_at.isoformat(),
                "payload": e.payload,
            }
            for e in rows
        ]
    }


@router.patch(
    "/{bot_id}/limits",
    summary="Set this bot's limits. They can only restrict, never widen",
    description=(
        "Every field is optional and null means this bot states nothing, so "
        "the account's limit applies. A figure larger than the account's is "
        "accepted and then ignored, because the manager combines the two and "
        "takes the more restrictive — validating at write time would let a "
        "limit that was legal when saved become illegal when the account "
        "tightens, and nobody would notice."
    ),
)
async def set_limits(
    bot_id: str,
    body: LimitsBody,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, Any]:
    bot = await _owned(db, bot_id, user)
    bot.max_positions = body.max_positions
    bot.max_daily_trades = body.max_daily_trades
    bot.max_daily_loss = body.max_daily_loss
    bot.max_risk_per_trade = body.max_risk_per_trade
    bot.cooldown_seconds = body.cooldown_seconds
    await db.commit()
    return {"bot_id": bot.id, "limits": _limits_of(bot).as_dict()}


class BracketSourceBody(BaseModel):
    """Whether this bot may take its bracket from the alert, and why."""

    use_alert_bracket: bool
    reason: Annotated[str, Field(min_length=8, max_length=300)]


@router.post(
    "",
    status_code=201,
    summary="Create a bot that routes one strategy version to one broker account",
    description=(
        "**The join that makes a recorded signal executable.** `route_for` "
        "hands a signal to the one enabled bot for its strategy version in the "
        "platform's mode; without such a bot every alert is retired as "
        "`signal_not_executable`, which is the state every demo signal was in "
        "until this route existed.\n\n"
        "DEMO ONLY. `/v1/paper/bots` creates paper bots and hard-codes their "
        "mode for the same reason this one does: the mode is not a field a "
        "caller may set. There is no live branch here, and a live platform "
        "would be refused by this route as well as by everything behind it.\n\n"
        "Creating it does NOT send anything. It makes signals routable; the "
        "RiskEngine still approves every order and the OMS still owns the "
        "lifecycle. Requires step-up re-authentication and a reason."
    ),
)
async def create_broker_bot(
    body: CreateBrokerBotBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, Any]:
    settings = request.app.state.settings
    mode = settings.trading_mode.value

    # The mode fence, and it is why this route cannot become a live one. A
    # caller cannot pass a mode; the platform's own is used, and only `demo` is
    # accepted. `live` has no branch here at all, so there is no value anybody
    # can send that reaches one.
    if mode != "demo":
        raise Conflict(
            f"this platform runs {mode}; a broker bot is only created on a demo "
            "platform. Paper bots are created at /v1/paper/bots, and there is no "
            "live path"
        )

    version = await db.get(StrategyVersion, body.strategy_version_id)
    if version is None:
        raise NotFound(f"no strategy version {body.strategy_version_id}")

    account = await db.get(BrokerAccount, body.broker_account_id)
    if account is None or not account.is_active:
        raise NotFound(
            f"no active broker account {body.broker_account_id}. A venue registration "
            "writes this row; register the venue first"
        )
    if account.user_id != user.id:
        raise NotFound(f"no active broker account {body.broker_account_id} for this user")
    if account.account_mode != mode:
        raise Conflict(
            f"account {body.broker_account_id} is a {account.account_mode} account and "
            f"this platform runs {mode}; refusing to point a bot at an account in a "
            "mode the platform is not in"
        )

    # `route_for` REFUSES two enabled bots on one strategy version rather than
    # choosing between them, because the account an order lands on is not a
    # coin flip. Catching it here says so at the moment somebody would create
    # the ambiguity, rather than at the moment a signal cannot be routed.
    existing = await db.scalar(
        select(Bot).where(
            Bot.strategy_version_id == body.strategy_version_id,
            Bot.mode == mode,
            Bot.is_enabled.is_(True),
            Bot.is_disabled.is_(False),
        )
    )
    if existing is not None:
        raise Conflict(
            f"bot {existing.id} already runs strategy version "
            f"{body.strategy_version_id} in {mode}; two would make routing a choice "
            "nobody made. Disable that one first"
        )

    await require_step_up(
        request,
        scope=StepUpScope.broker_credentials,
        subject=body.broker_account_id,
        actor_id=user.id,
        settings=settings,
    )

    row = Bot(
        user_id=user.id,
        name=body.name,
        mode=mode,
        strategy_version_id=body.strategy_version_id,
        broker_account_id=body.broker_account_id,
        # ENABLED on creation, deliberately. `is_enabled` means "may be
        # started", and `route_for` requires it -- a bot created disabled with
        # no route to enable it would be the seat nobody can sit in that
        # `_ADAPTERS` was until L70b. The deliberateness lives in the step-up
        # and the reason, not in a second switch nothing can flip.
        is_enabled=True,
        is_disabled=False,
        max_risk_per_trade=body.max_risk_per_trade,
        max_positions=body.max_positions,
        max_daily_trades=body.max_daily_trades,
        max_daily_loss=body.max_daily_loss,
        cooldown_seconds=body.cooldown_seconds,
    )
    db.add(row)
    await db.flush()

    await announce(
        request,
        security_record(
            SecurityEvent.dangerous_action,
            f"a {mode} bot was created for account {body.broker_account_id}",
            user_id=user.id,
            action="create_broker_bot",
            resource="bot",
            outcome=row.id,
        ),
    )
    await audit.record_admin(
        db,
        audit.AuditAction.admin_action,
        "bot",
        actor_user_id=user.id,
        resource_id=row.id,
        reason=body.reason,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        environment=mode,
        extra={
            "action": "create_broker_bot",
            "strategy_version_id": body.strategy_version_id,
            "broker_account_id": body.broker_account_id,
            "max_risk_per_trade": str(body.max_risk_per_trade),
        },
    )
    await db.commit()
    return {
        "bot_id": row.id,
        "name": row.name,
        "mode": row.mode,
        "strategy_version_id": row.strategy_version_id,
        "broker_account_id": row.broker_account_id,
        "enabled": row.is_enabled,
        "max_risk_per_trade": str(row.max_risk_per_trade),
        "note": (
            "Signals for this strategy version now route here. Nothing has been "
            "sent: the RiskEngine approves every order and the OMS owns the "
            "lifecycle."
        ),
    }


@router.post(
    "/{bot_id}/bracket-source",
    summary="Choose where this bot's signals get their stop and target",
    description=(
        "OFF by default and for every existing bot. A TradingView alert's "
        "suggested stop is quarantined under `advisory_ignored` on purpose; "
        "turning this on promotes it to the platform's own bracket for this "
        "bot only. Requires step-up re-authentication and a reason, because "
        "under fixed-risk sizing a TIGHTER stop produces a LARGER position — "
        "so this hands an external sender an input that moves size upward. "
        "`max_risk_per_trade` still caps the money at risk and the RiskEngine "
        "still evaluates every resulting order."
    ),
)
async def set_bracket_source(
    bot_id: str,
    body: BracketSourceBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, Any]:
    bot = await _owned(db, bot_id, user)
    # The password again. Turning this ON lifts a safety control; turning it
    # OFF is also gated, deliberately, so the audit trail has both directions
    # and nobody can quietly flip it back and forth below the record.
    await require_step_up(
        request,
        scope=StepUpScope.bot_bracket_source,
        subject=bot.id,
        actor_id=user.id,
        settings=request.app.state.settings,
    )
    was = bool(bot.use_alert_bracket)
    bot.use_alert_bracket = body.use_alert_bracket

    await announce(
        request,
        security_record(
            SecurityEvent.dangerous_action,
            (
                f"bot {bot.id} may now take its bracket from the alert"
                if body.use_alert_bracket
                else f"bot {bot.id} no longer takes its bracket from the alert"
            ),
            user_id=user.id,
            action="set_bracket_source",
            resource="bot",
            outcome="alert" if body.use_alert_bracket else "none",
        ),
    )
    await audit.record_admin(
        db,
        audit.AuditAction.admin_action,
        "bot",
        actor_user_id=user.id,
        resource_id=bot.id,
        reason=body.reason,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        extra={
            "action": "set_bracket_source",
            "was": "alert" if was else "none",
            "now": "alert" if body.use_alert_bracket else "none",
        },
    )
    await db.commit()
    return {
        "bot_id": bot.id,
        "use_alert_bracket": bot.use_alert_bracket,
        "bracket_source": "alert" if bot.use_alert_bracket else "none",
        "note": (
            "The alert's suggestion is still recorded separately under "
            "advisory_ignored; what was suggested and what was used stay "
            "independently auditable."
        ),
    }


@router.post(
    "/{bot_id}/disable",
    summary="Take a bot out of service until somebody re-enables it",
    description=(
        "Stronger than STOP: a stopped bot may be started by anyone, a "
        "disabled one may not be started at all. It does NOT close positions "
        "— everything the bot already has stays under the position manager, "
        "the same as for a stop."
    ),
)
async def disable_bot(
    bot_id: str,
    body: DisableBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _MANAGE,
) -> dict[str, Any]:
    bot = await _owned(db, bot_id, user)
    bot.is_disabled = True
    bot.is_enabled = False
    bot.disabled_reason = body.reason
    bot.disabled_at = datetime.now(UTC).replace(tzinfo=None)

    # If it is running, stop it too. Disabling a bot that keeps trading would
    # be a label rather than a control.
    runner = _runner(request)
    stopped = False
    if runner is not None:
        try:
            live = runner.get_bot(bot_id, user.id)
        except KeyError:
            live = None
        if live is not None:
            await runner.stop_bot(live, f"disabled: {body.reason}")
            stopped = True
    await db.commit()
    return {
        "bot_id": bot.id,
        "disabled": True,
        "was_running": stopped,
        "reason": body.reason,
        "note": (
            "No position was closed. Everything this bot holds stays under the "
            "position manager, exactly as it would after a stop."
        ),
    }


@router.post(
    "/{bot_id}/enable",
    summary="Return a disabled bot to service. Does not start it",
)
async def enable_bot(
    bot_id: str, db: AsyncSession = Depends(get_db), user: User = _MANAGE
) -> dict[str, Any]:
    bot = await _owned(db, bot_id, user)
    if not bot.is_disabled:
        raise Conflict(f"bot {bot_id} is not disabled")
    bot.is_disabled = False
    bot.disabled_reason = None
    bot.disabled_at = None
    await db.commit()
    return {
        "bot_id": bot.id,
        "disabled": False,
        "running": False,
        "note": "Re-enabled and NOT started. Starting is a separate, deliberate action.",
    }


@router.post(
    "/supervise",
    summary="Sweep every run: mark silent ones crashed, consider recovery",
    description=(
        "Measures each active run's heartbeat against the clock. A run that "
        "has stopped beating is marked crashed — never restarted on the "
        "strength of its status column. Recovery is then considered "
        "separately and refused unless every safety gate agrees; a refusal "
        "leaves the run crashed with the reason, which is the honest state."
    ),
)
async def supervise(db: AsyncSession = Depends(get_db), user: User = _MANAGE) -> dict[str, Any]:
    report = await BotSupervisor(db).sweep()
    await db.commit()
    return report.as_dict()


@router.get(
    "/supervise/restart-plan",
    summary="What a restart would do to each bot, without doing any of it",
    description=(
        "A stopped bot stays stopped and a paused bot stays paused. A bot the "
        "row calls RUNNING is one whose process is gone, so it is marked "
        "crashed for the supervisor to consider rather than silently started."
    ),
)
async def restart_plan(db: AsyncSession = Depends(get_db), user: User = _MANAGE) -> dict[str, Any]:
    plans = await BotSupervisor(db).plan_restart()
    await db.commit()
    return {
        "plans": [p.as_dict() for p in plans],
        "note": (
            "Nothing was started. A restart resumes what was deliberately running and "
            "leaves alone what was deliberately not."
        ),
    }


def _live_refusal(bot: Bot, request: Request) -> str | None:
    """Why this bot may not start, when the reason is the trading mode."""
    settings = getattr(request.app.state, "settings", None)
    if bot.mode == "live" and not (settings and settings.live_trading):
        return (
            "this bot is configured for live and LIVE_TRADING is false. The refusal is "
            "the setting, not the request: retrying will not change it, and no UI "
            "action can"
        )
    if bot.mode in ("demo", "live"):
        return (
            f"no runner drives a {bot.mode} strategy loop yet. The OMS and the broker "
            "executor are built, but nothing evaluates a strategy against them, and "
            "starting a bot that cannot trade would be a status nobody could act on"
        )
    return None


@router.post(
    "/{bot_id}/preflight",
    summary="Would this bot start? Every check, without starting it",
    description=(
        "Runs the dependency checks a start would run and reports each one. "
        "It starts nothing, so a caller can see why a bot will not run "
        "without having to attempt it and read an error."
    ),
)
async def preflight(
    bot_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _VIEW,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    bot = await _owned(db, bot_id, user)
    run = await _latest_run(db, bot_id)
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    add("bot_not_disabled", not bot.is_disabled, bot.disabled_reason or "not disabled")
    add(
        "account_configured",
        bool(bot.paper_account_id or bot.broker_account_id),
        "an account is set" if (bot.paper_account_id or bot.broker_account_id) else "no account",
    )
    mode_refusal = _live_refusal(bot, request)
    add("trading_mode", mode_refusal is None, mode_refusal or f"{bot.mode} is runnable")
    add(
        "not_already_running",
        run is None or BotState(run.status) not in ACTIVE,
        f"the latest run is {run.status}" if run else "no previous run",
    )
    risk = getattr(request.app.state, "risk", None)
    engaged = (
        risk.switches.engaged_for(
            bot.broker_account_id or bot.paper_account_id, bot.strategy_version_id
        )
        if risk
        else None
    )
    add(
        "kill_switch_clear",
        engaged is None,
        "no kill switch" if engaged is None else f"kill switch engaged: {engaged[1]}",
    )
    counters = await _counters(db, bot, now=now)
    verdict = check_limits(_limits_of(bot), counters, now=now)
    add("bot_limits", verdict.allowed, verdict.detail or "within this bot's limits")

    return {
        "bot_id": bot.id,
        "would_start": all(c["passed"] for c in checks),
        "checks": checks,
        "note": "Nothing was started. This is the same set of gates a start applies.",
    }
