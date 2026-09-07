"""The bot manager (L22).

The paper runner's own lifecycle has been tested since L16
(`tests/test_paper.py`). This file tests what L22 added: the nine-state
machine both runners share, the per-bot limits that can only tighten, and the
supervisor that measures heartbeats instead of trusting a status column.

The three that matter most:

  * `test_a_paused_run_is_recorded_as_paused` — it was recorded as `stopping`,
    so a paused bot and a bot shutting down were the same row.
  * `test_a_bot_limit_can_only_tighten_an_account_limit` — §17, and it holds
    by arithmetic rather than by a check somebody has to run.
  * `test_recovery_is_refused_when_no_safety_check_is_wired` — the absence of
    a check is not evidence that recovery is safe.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.bots import (
    ACTIVE,
    FINISHED,
    MAY_TRADE,
    RECOVERABLE,
    TRANSITIONS,
    BotCounters,
    BotLimits,
    BotState,
    BotSupervisor,
    IllegalBotTransition,
    check,
    check_transition,
    may_trade,
)
from app.db.base import Base
from app.models.bots import BOT_RUN_STATUSES, Bot, BotEvent, BotRun
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


# ================================================================= fixtures


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        from app.auth.models import User

        session.add(User(id="u1", email="a@b.io", password_hash="x", role="trader"))
        await session.flush()
        yield session
    await engine.dispose()


async def make_bot(db: AsyncSession, **over: object) -> Bot:
    fields: dict[str, object] = {
        "user_id": "u1",
        "name": "EURUSD test bot",
        "mode": "paper",
        "is_enabled": True,
    }
    fields.update(over)
    row = Bot(**fields)
    db.add(row)
    await db.flush()
    return row


async def make_run(db: AsyncSession, bot: Bot, **over: object) -> BotRun:
    fields: dict[str, object] = {
        "bot_id": bot.id,
        "started_at": (NOW - timedelta(minutes=5)).replace(tzinfo=None),
        "status": str(BotState.running),
        "last_heartbeat_at": (NOW - timedelta(seconds=5)).replace(tzinfo=None),
    }
    fields.update(over)
    row = BotRun(**fields)
    db.add(row)
    await db.flush()
    return row


async def events_for(db: AsyncSession, run_id: str) -> list[BotEvent]:
    return list(
        (
            await db.scalars(
                select(BotEvent).where(BotEvent.bot_run_id == run_id).order_by(BotEvent.id)
            )
        ).all()
    )


# ============================================================ the state machine


def test_the_table_and_the_code_agree_on_the_states() -> None:
    assert {s.value for s in BotState} == set(BOT_RUN_STATUSES)
    assert len(BOT_RUN_STATUSES) == 9


def test_only_a_running_bot_may_open_a_new_trade() -> None:
    """Pausing, stopping and every failure state all mean "no new trades".
    Collapsing that into "not stopped" would let a crashed bot keep trading."""
    assert MAY_TRADE == {BotState.running}
    for state in BotState:
        assert may_trade(state) is (state is BotState.running)


def test_a_disabled_bot_is_terminal() -> None:
    """Re-enabling is an action on the BOT, which starts a new run. It is not
    a transition out of this one."""
    assert TRANSITIONS[BotState.disabled] == frozenset()


@pytest.mark.parametrize(
    ("current", "wanted"),
    [
        (BotState.stopped, BotState.running),
        (BotState.crashed, BotState.running),
        (BotState.halted, BotState.running),
        (BotState.disabled, BotState.running),
        (BotState.recovering, BotState.running),
    ],
)
def test_nothing_reaches_running_without_passing_starting(
    current: BotState, wanted: BotState
) -> None:
    """Starting is where the dependency checks live, so a recovered or
    restarted bot must pass them like any other."""
    with pytest.raises(IllegalBotTransition):
        check_transition(current, wanted)


def test_a_halted_run_is_not_recoverable() -> None:
    """A kill switch is a decision somebody made. Recovering out of it
    automatically would be exactly the bot-level bypass §22 forbids."""
    assert RECOVERABLE == {BotState.crashed}
    assert BotState.halted not in RECOVERABLE
    assert BotState.recovering not in TRANSITIONS[BotState.halted]


def test_active_and_finished_partition_the_states() -> None:
    assert ACTIVE & FINISHED == frozenset()
    assert (ACTIVE | FINISHED | {BotState.recovering}) == set(BotState)


# ================================================================ bot limits


def test_a_bot_limit_can_only_tighten_an_account_limit() -> None:
    """§17. It holds by arithmetic, not by a check somebody has to run: the
    combination cannot produce a looser figure than either input."""
    account = BotLimits(max_risk_per_trade=Decimal("2"), max_positions=3)
    greedy = BotLimits(max_risk_per_trade=Decimal("5"), max_positions=99)
    effective = greedy.effective(account)
    assert effective.max_risk_per_trade == Decimal("2")
    assert effective.max_positions == 3

    modest = BotLimits(max_risk_per_trade=Decimal("0.5"), max_positions=1)
    assert modest.effective(account).max_risk_per_trade == Decimal("0.5")
    assert modest.effective(account).max_positions == 1


def test_a_silent_layer_states_nothing_rather_than_unlimited() -> None:
    """None means NOT SET. A field defaulting to a number would be this layer
    inventing a policy."""
    account = BotLimits(max_positions=3)
    assert BotLimits().effective(account).max_positions == 3
    assert account.effective(BotLimits()).max_positions == 3
    assert BotLimits().effective(BotLimits()).max_positions is None


def test_the_longer_cooldown_wins_because_longer_is_stricter() -> None:
    """The one field where the LARGER number is the more restrictive. Getting
    it backwards would let a bot shorten a cooldown its account imposed."""
    account = BotLimits(cooldown_seconds=600)
    assert BotLimits(cooldown_seconds=60).effective(account).cooldown_seconds == 600
    assert BotLimits(cooldown_seconds=900).effective(account).cooldown_seconds == 900


def test_a_breached_daily_loss_stops_new_trades() -> None:
    verdict = check(
        BotLimits(max_daily_loss=Decimal("200")),
        BotCounters(realised_today=Decimal("-200")),
        now=NOW,
    )
    assert not verdict.allowed
    assert verdict.code == "bot_daily_loss"
    # And it says what does NOT happen, because that is the part operators
    # assume wrongly.
    assert "Open positions are unaffected" in verdict.detail


def test_a_full_position_book_stops_new_trades() -> None:
    verdict = check(BotLimits(max_positions=3), BotCounters(open_positions=3), now=NOW)
    assert not verdict.allowed
    assert verdict.code == "bot_max_positions"
    assert check(BotLimits(max_positions=3), BotCounters(open_positions=2), now=NOW).allowed


def test_a_daily_trade_limit_stops_new_trades() -> None:
    verdict = check(BotLimits(max_daily_trades=10), BotCounters(trades_today=10), now=NOW)
    assert not verdict.allowed
    assert verdict.code == "bot_daily_trades"


def test_a_cooldown_blocks_until_it_has_elapsed() -> None:
    limits = BotLimits(cooldown_seconds=600)
    recent = BotCounters(last_trade_at=NOW - timedelta(seconds=60))
    blocked = check(limits, recent, now=NOW)
    assert not blocked.allowed
    assert blocked.code == "bot_cooldown"
    assert "540s remaining" in blocked.detail

    old = BotCounters(last_trade_at=NOW - timedelta(seconds=601))
    assert check(limits, old, now=NOW).allowed


def test_a_naive_timestamp_does_not_raise_against_an_aware_clock() -> None:
    """A stamp read from the database is naive and `now` is aware. This
    repository has already paid for that mismatch once, in the risk engine."""
    limits = BotLimits(cooldown_seconds=600)
    naive = BotCounters(last_trade_at=(NOW - timedelta(seconds=60)).replace(tzinfo=None))
    verdict = check(limits, naive, now=NOW)
    assert not verdict.allowed


def test_no_limits_means_no_bot_level_refusal() -> None:
    """The RiskEngine's global limits still apply; this layer just says
    nothing."""
    assert check(BotLimits(), BotCounters(open_positions=99, trades_today=99), now=NOW).allowed


# ================================================================ supervisor


async def test_a_silent_run_is_marked_crashed_not_restarted(db: AsyncSession) -> None:
    """§26. "The database says RUNNING" is not evidence that a bot is
    running."""
    bot = await make_bot(db)
    run = await make_run(
        db, bot, last_heartbeat_at=(NOW - timedelta(minutes=10)).replace(tzinfo=None)
    )

    report = await BotSupervisor(db).sweep(now=NOW)
    assert report.checked == 1
    assert len(report.stale) == 1
    assert run.status == str(BotState.crashed)
    assert "no heartbeat" in report.stale[0].reason
    kinds = [e.event_type for e in await events_for(db, run.id)]
    assert "heartbeat_stale" in kinds


async def test_a_beating_run_is_left_alone(db: AsyncSession) -> None:
    bot = await make_bot(db)
    run = await make_run(db, bot)
    report = await BotSupervisor(db).sweep(now=NOW)
    assert report.stale == []
    assert run.status == str(BotState.running)


async def test_a_run_that_has_never_beaten_is_judged_from_its_start(
    db: AsyncSession,
) -> None:
    """Never beating is only suspicious once it has had time to."""
    bot = await make_bot(db)
    fresh = await make_run(
        db,
        bot,
        status=str(BotState.starting),
        started_at=(NOW - timedelta(seconds=5)).replace(tzinfo=None),
        last_heartbeat_at=None,
    )
    assert (await BotSupervisor(db).sweep(now=NOW)).stale == []
    assert fresh.status == str(BotState.starting)

    fresh.started_at = (NOW - timedelta(minutes=10)).replace(tzinfo=None)
    await db.flush()
    assert len((await BotSupervisor(db).sweep(now=NOW)).stale) == 1
    assert fresh.status == str(BotState.crashed)


async def test_recovery_is_refused_when_no_safety_check_is_wired(
    db: AsyncSession,
) -> None:
    """The absence of a check is not evidence that recovery is safe, and a
    supervisor that restarted everything because nobody told it not to would
    be the most dangerous default in the system."""
    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.crashed))

    report = await BotSupervisor(db).sweep(now=NOW)
    assert report.recovered == []
    assert len(report.refused) == 1
    assert "cannot be shown to be safe" in report.refused[0].reason
    assert run.status == str(BotState.crashed)
    assert "recovery_refused" in [e.event_type for e in await events_for(db, run.id)]


async def test_recovery_runs_when_every_gate_agrees(db: AsyncSession) -> None:
    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.crashed))
    restarted: list[str] = []

    async def safe(_bot: Bot) -> str | None:
        return None

    async def restart(b: Bot, _r: BotRun) -> bool:
        restarted.append(b.id)
        return True

    report = await BotSupervisor(db, safe_to_recover=safe, restart=restart).sweep(now=NOW)
    assert restarted == [bot.id]
    assert len(report.recovered) == 1
    assert run.status == str(BotState.recovering)
    kinds = [e.event_type for e in await events_for(db, run.id)]
    assert "recovery_started" in kinds
    assert "recovery_completed" in kinds


async def test_a_runner_that_declines_leaves_the_run_crashed(
    db: AsyncSession,
) -> None:
    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.crashed))

    async def safe(_bot: Bot) -> str | None:
        return None

    async def decline(_b: Bot, _r: BotRun) -> bool:
        return False

    report = await BotSupervisor(db, safe_to_recover=safe, restart=decline).sweep(now=NOW)
    assert report.refused
    assert run.status == str(BotState.crashed)
    assert "recovery_failed" in [e.event_type for e in await events_for(db, run.id)]


async def test_a_restart_that_raises_leaves_the_run_crashed(db: AsyncSession) -> None:
    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.crashed))

    async def safe(_bot: Bot) -> str | None:
        return None

    async def explode(_b: Bot, _r: BotRun) -> bool:
        raise RuntimeError("the runner is down")

    report = await BotSupervisor(db, safe_to_recover=safe, restart=explode).sweep(now=NOW)
    assert report.refused
    assert run.status == str(BotState.crashed)


async def test_a_halted_run_is_never_swept_into_recovery(db: AsyncSession) -> None:
    """No bot-level bypass of a global safety control."""
    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.halted))

    async def safe(_bot: Bot) -> str | None:
        return None

    report = await BotSupervisor(db, safe_to_recover=safe).sweep(now=NOW)
    assert report.recovered == []
    assert report.refused == []
    assert run.status == str(BotState.halted)


# ========================================================== restart recovery


async def test_a_restart_leaves_stopped_and_paused_bots_alone(
    db: AsyncSession,
) -> None:
    """§29. "Do not blindly start every bot" is the sentence that matters."""
    stopped_bot = await make_bot(db, name="stopped")
    stopped = await make_run(db, stopped_bot, status=str(BotState.stopped))
    paused_bot = await make_bot(db, name="paused")
    paused = await make_run(db, paused_bot, status=str(BotState.paused))

    plans = {p.bot_id: p for p in await BotSupervisor(db).plan_restart(now=NOW)}
    assert plans[stopped_bot.id].action == "leave"
    assert plans[paused_bot.id].action == "resume_paused"
    assert stopped.status == str(BotState.stopped)
    assert paused.status == str(BotState.paused)


async def test_a_restart_marks_an_orphaned_running_bot_crashed(
    db: AsyncSession,
) -> None:
    """The row says RUNNING and this process did not start it, so its process
    is gone whatever the row says. Marked crashed for the supervisor to
    consider — never silently started."""
    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.running))

    plans = await BotSupervisor(db).plan_restart(now=NOW)
    assert plans[0].action == "mark_crashed"
    assert run.status == str(BotState.crashed)
    assert "restart_orphan" in [e.event_type for e in await events_for(db, run.id)]


async def test_a_restart_leaves_a_disabled_bot_alone(db: AsyncSession) -> None:
    bot = await make_bot(db, is_disabled=True)
    run = await make_run(db, bot, status=str(BotState.running))
    plans = await BotSupervisor(db).plan_restart(now=NOW)
    assert plans[0].action == "leave"
    assert "disabled" in plans[0].reason
    assert run.status == str(BotState.running)  # untouched


async def test_a_bot_that_has_never_run_is_left_alone(db: AsyncSession) -> None:
    bot = await make_bot(db)
    plans = await BotSupervisor(db).plan_restart(now=NOW)
    assert plans[0].bot_id == bot.id
    assert plans[0].action == "leave"
    assert plans[0].run_id is None


# ======================================================== architecture fences


def test_the_bot_manager_cannot_reach_a_venue() -> None:
    """§59.4: the bot manager cannot directly call MT5, and cannot reach the
    OMS or an adapter either. The trading path runs through the runner."""
    package = Path(__file__).resolve().parents[1] / "app" / "bots"
    forbidden = ("app.oms", "app.brokers", "MetaTrader5", "app.sizing")
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for bad in forbidden:
                    assert not name.startswith(bad), f"{path.name} imports {name}"


def test_the_paper_runner_and_the_manager_share_one_state_machine() -> None:
    """Two vocabularies would let a bot be `paused` to one and `stopping` to
    the other, which is the exact defect L22 fixed."""
    from app.bots import state as shared
    from app.paper import service as runner

    assert runner.BotState is shared.BotState
    assert runner.FINISHED is shared.FINISHED


def test_live_trading_is_still_disabled_by_default() -> None:
    from app.core.settings import LIVE_GATES, Settings

    settings = Settings(_env_file=None)
    assert settings.live_trading is False
    assert settings.live_execution_allowed is False
    assert not any(LIVE_GATES.values())


# ================================== the defect this level fixed, end to end


async def test_a_paused_run_is_recorded_as_paused(db: AsyncSession) -> None:
    """FIXED AT L22, and this is the level's most consequential change.

    `PaperService.pause_bot` wrote `stopping` here, because the table had no
    value for `paused`. So a paused bot and a bot shutting down were the same
    row: after a restart, a supervisor reading it would either resume a bot
    somebody had deliberately paused, or abandon one that was only
    mid-shutdown. Both are wrong and only one is visible.

    The two now have different rows, and the restart planner acts on the
    difference — which is what makes the fix observable rather than cosmetic.
    """
    bot_a = await make_bot(db, name="paused bot")
    paused = await make_run(db, bot_a, status=str(BotState.paused))
    bot_b = await make_bot(db, name="stopping bot")
    stopping = await make_run(db, bot_b, status=str(BotState.stopping))

    assert paused.status != stopping.status

    plans = {p.bot_id: p for p in await BotSupervisor(db).plan_restart(now=NOW)}
    # The deliberately-paused one is preserved as paused...
    assert plans[bot_a.id].action == "resume_paused"
    assert paused.status == str(BotState.paused)
    # ...and the one that was mid-shutdown is an orphan whose process is gone.
    assert plans[bot_b.id].action == "mark_crashed"
    assert stopping.status == str(BotState.crashed)


def test_the_pause_path_no_longer_writes_stopping() -> None:
    """Asserted on the source, because the behaviour it guards is one line and
    a regression would look like a tidy-up."""
    import inspect

    from app.paper import service

    source = inspect.getsource(service.PaperService.pause_bot)
    assert '"stopping"' not in source
    assert "BotState.paused" in source


# ============================================ L51 Phase 19: the recovery budget


async def _crash_loop(
    db: AsyncSession, bot: Bot, *, sweeps: int, budget: object | None = None
) -> int:
    """Sweep repeatedly while every restart immediately crashes again.

    This is the real failure shape, not an approximation of it: a restart
    produces a NEW run, that run dies on start, and the next sweep finds a new
    crashed run. Counting per run would always see one attempt, which is
    exactly why the budget counts per bot.
    """
    from app.bots.supervisor import BotSupervisor

    attempts = 0

    async def safe(_bot: Bot) -> str | None:
        return None

    async def restart(b: Bot, _r: BotRun) -> bool:
        nonlocal attempts
        attempts += 1
        # The runner starts it; it dies immediately and is recorded crashed.
        await make_run(db, b, status=str(BotState.crashed))
        return True

    for i in range(sweeps):
        kwargs: dict[str, object] = {"safe_to_recover": safe, "restart": restart}
        if budget is not None:
            kwargs["budget"] = budget
        # Time advances a minute per sweep, so the cooldown is the thing under
        # test rather than an artefact of every sweep sharing one timestamp.
        await BotSupervisor(db, **kwargs).sweep(now=NOW + timedelta(minutes=i))  # type: ignore[arg-type]
    return attempts


async def test_a_bot_that_crashes_on_start_is_not_restarted_forever(
    db: AsyncSession,
) -> None:
    """**L51 Phase 19, and rule 16: no infinite restart loops.**

    Every safety gate the supervisor had asked whether recovery is safe *now* —
    safe mode, kill switches, unresolved orders, unsettled positions, an
    unusable adapter. None asked how many times it had already been tried, so a
    bot that dies on start passed all of them on every sweep.
    """
    bot = await make_bot(db)
    await make_run(db, bot, status=str(BotState.crashed))

    attempts = await _crash_loop(db, bot, sweeps=12)

    assert attempts <= 3, (
        f"the supervisor restarted a crash-on-start bot {attempts} times in 12 sweeps"
    )


async def test_the_budget_is_what_stops_it(db: AsyncSession) -> None:
    """The capability check. A generous budget lets the loop run, which proves
    the bound above is doing the work rather than something incidental."""
    from app.bots.budget import RecoveryBudget

    bot = await make_bot(db)
    await make_run(db, bot, status=str(BotState.crashed))

    unbounded = RecoveryBudget(max_attempts=1000, window=timedelta(hours=1), cooldown=timedelta(0))
    attempts = await _crash_loop(db, bot, sweeps=12, budget=unbounded)

    assert attempts > 3, "the loop did not reproduce, so the bounded test above proves nothing"


async def test_the_cooldown_stops_a_fast_loop(db: AsyncSession) -> None:
    """`cooldown` and `max_attempts` stop different things: a fast loop and a
    slow one. A bot crashing every six minutes would defeat either alone."""
    from app.bots.budget import RecoveryBudget

    budget = RecoveryBudget(
        max_attempts=99, window=timedelta(hours=1), cooldown=timedelta(minutes=5)
    )
    refusal = budget.refusal(
        attempts_in_window=1, last_attempt_at=NOW - timedelta(seconds=30), now=NOW
    )
    assert refusal is not None
    assert "cooldown" in refusal
    # And past the cooldown it allows again.
    assert (
        budget.refusal(attempts_in_window=1, last_attempt_at=NOW - timedelta(minutes=6), now=NOW)
        is None
    )


async def test_an_exhausted_budget_records_why(db: AsyncSession) -> None:
    """A refusal an operator cannot see is a bot that silently stops recovering.

    L51 Phase 27: a system that escalates appropriately is better than one that
    performs unsafe recovery — but only if the escalation is legible.
    """
    from app.bots.budget import RecoveryBudget
    from app.bots.supervisor import BotSupervisor

    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.crashed))

    async def safe(_bot: Bot) -> str | None:
        return None

    async def restart(_b: Bot, _r: BotRun) -> bool:
        return True

    # A budget already spent: zero attempts allowed.
    spent = RecoveryBudget(max_attempts=0, window=timedelta(hours=1), cooldown=timedelta(0))
    report = await BotSupervisor(db, safe_to_recover=safe, restart=restart, budget=spent).sweep(
        now=NOW
    )

    assert report.refused
    assert run.status == str(BotState.crashed)
    kinds = [e.event_type for e in await events_for(db, run.id)]
    assert "recovery_budget_exhausted" in kinds
    assert "recovery_started" not in kinds


async def test_a_safety_refusal_keeps_its_own_message(db: AsyncSession) -> None:
    """The budget runs after the safety gates, so 'the platform is in safe
    mode' is not replaced by 'out of budget'. Both would be true, and only one
    tells the operator what to do."""
    from app.bots.budget import RecoveryBudget
    from app.bots.supervisor import BotSupervisor

    bot = await make_bot(db)
    run = await make_run(db, bot, status=str(BotState.crashed))

    async def refuse(_bot: Bot) -> str | None:
        return "the platform is in safe mode, so no bot is recovered"

    spent = RecoveryBudget(max_attempts=0, window=timedelta(hours=1), cooldown=timedelta(0))
    report = await BotSupervisor(db, safe_to_recover=refuse, budget=spent).sweep(now=NOW)

    assert report.refused
    assert "safe mode" in report.refused[0].reason
    kinds = [e.event_type for e in await events_for(db, run.id)]
    assert "recovery_refused" in kinds
    assert "recovery_budget_exhausted" not in kinds
