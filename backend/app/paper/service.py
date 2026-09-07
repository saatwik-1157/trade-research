"""Paper accounts and paper bots: lifecycle, persistence and the background loop.

A paper bot lives in a backend task, so **closing the browser does not stop
it**. The frontend controls a backend bot; it does not host one. That is the
same shape L15 gave replay sessions, and the same reason.

Three things in here are load-bearing.

**Restart recovery is built on the database's own uniqueness, not on memory.**
`orders.intent_id` is `unique=True`, and the intent id IS the signal key -- a
hash of (account, strategy, symbol, timeframe, bar time, signal type). So the
same bar can never produce a second order, whatever happens to the process:
the in-memory `seen_signals` set is rebuilt from the persisted intent ids on
resume, and if that were ever wrong the unique index would still refuse the
insert. Two independent mechanisms, one of which survives a kill -9.

**One pass is one transaction.** The order, its execution, the position, the
trades and the account balance are written together or not at all. A fill with
no position row, or a position with no balance change, would be a corruption
that nothing downstream could detect -- the journal would simply be wrong.

**Configuration is frozen while a bot runs.** The strategy, symbol, timeframe,
risk limits and sizing are copied onto the run when it starts. Editing the bot
changes what the NEXT run uses; it cannot change what a running one is doing,
because a bot that silently changed strategy mid-flight would make its own
trade record unreadable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import strategy_config as ai_config
from app.ai.decision import AiIntegrationError, AiMode
from app.ai.integration import AiIntegrationService, RegistryResolver
from app.auth.models import utcnow
from app.backtest.config import CostModel
from app.bots.state import FINISHED, BotState
from app.core.events import Event
from app.execution.ai import IntegrationFilter
from app.journal.lifecycle import exit_reason_of as journal_exit
from app.marketdata.types import Provider, Timeframe
from app.models.accounts import PaperAccount
from app.models.bots import Bot, BotEvent, BotRun
from app.models.execution import Execution, Order, OrderEvent, Position, Trade
from app.models.risk import RiskEvent
from app.paper.clock import from_storage, now_utc
from app.paper.engine import NO_ORDER, Outcome, PaperEngine, Pass
from app.paper.portfolio import (
    AccountState,
    PaperPortfolio,
    check_transition,
    value_per_price_unit,
)
from app.paper.router import ExecutionMode, ExecutionProvider, provider_for
from app.realtime.catalogue import EventType, Scope
from app.realtime.channels import Channel
from app.risk.engine import KillSwitches, RiskEngine, RiskLimits
from app.risk.service import RiskService
from app.sizing.calculator import SizingMethod

log = logging.getLogger("app.paper.service")

MAX_BOTS_PER_USER = 5
# How many passes to keep in memory for the state endpoint. The orders,
# positions and trades are the durable record; the pass log is a diagnostic.
PASS_BUFFER = 200
# A bot wakes at least this often even on a slow timeframe, so pause and stop
# are responsive rather than blocked behind an hour-long sleep.
MAX_SLEEP_SECONDS = 15.0
MIN_SLEEP_SECONDS = 1.0

BOT_STATUSES = ("starting", "running", "stopping", "stopped", "crashed", "halted")


class PaperBusy(Exception):
    """Too many bots already running for this user."""


class BotNotRunnable(Exception):
    """The bot cannot start in its current state, and the reason says why."""


# ============================================================ account loading


async def load_portfolio(db: AsyncSession, row: PaperAccount) -> PaperPortfolio:
    """Rebuild a portfolio from what is persisted, never from memory.

    Open positions and closed trades come from the tables, so a restarted
    process sees exactly what the previous one left. `balance` is the stored
    figure rather than a replay of the trade history: replaying would silently
    diverge from the account the user has been reading.
    """
    portfolio = PaperPortfolio(
        account_id=row.id,
        currency=row.currency,
        starting_balance=row.starting_balance,
        balance=row.balance,
        state=AccountState(row.status),
    )
    portfolio.peak_equity = max(row.equity, row.starting_balance)
    return portfolio


async def persist_account(db: AsyncSession, portfolio: PaperPortfolio, equity: Decimal) -> None:
    row = await db.get(PaperAccount, portfolio.account_id)
    if row is None:
        return
    row.balance = portfolio.balance
    row.equity = equity
    row.realized_pnl = portfolio.realized_pnl
    row.unrealized_pnl = equity - portfolio.balance
    row.commission_paid = portfolio.commission_paid
    row.status = str(portfolio.state)


# ================================================================== the bot


@dataclass
class RunningBot:
    """One live paper bot. Everything it touches is on this object."""

    id: str
    user_id: str
    account_id: str
    engine: PaperEngine
    symbol: str
    timeframe: Timeframe
    provider: Provider
    run_id: str
    status: str = "starting"
    error: str | None = None
    stop_reason: str | None = None
    # Frozen at start. Editing the bot changes the NEXT run, not this one.
    config: dict[str, object] = field(default_factory=dict)
    recent: list[Pass] = field(default_factory=list)
    _resume: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, object]:
        return {
            "bot_id": self.id,
            "run_id": self.run_id,
            "paper_account_id": self.account_id,
            "status": self.status,
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "provider": str(self.provider),
            "error": self.error,
            "stop_reason": self.stop_reason,
            "execution_mode": str(ExecutionMode.paper).upper(),
            "execution_provider": str(provider_for(ExecutionMode.paper)),
            "frozen_config": self.config,
            **self.engine.state(),
        }


class PaperService:
    """Owns the running bots. All state per-instance; nothing global."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        market_data: object,
        registry: object,
        symbols: object,
        hub: object | None = None,
        risk: RiskService | None = None,
        ai_models: object | None = None,
    ) -> None:
        self.sessions = sessions
        self.market_data = market_data
        self.registry = registry
        self.symbols = symbols
        self.hub = hub
        # L24's in-process model registry. None means this deployment has no
        # models loaded, which is not an error: every strategy then runs
        # AI_DISABLED and behaves exactly as the deterministic strategy does.
        self.ai_models = ai_models
        # The central safety authority. When present it owns the effective
        # limits, the kill switches and the latched locks; the engine keeps the
        # pure evaluation that mints the Approval the OMS requires.
        self.risk = risk
        self.live: dict[str, RunningBot] = {}
        # Kill switches, in memory and consulted by the risk engine on every
        # proposal. Global, account and strategy scopes come from the engine;
        # bot scope is here because a bot is not a risk-engine concept.
        self.switches = KillSwitches()
        self.bot_kill: set[str] = set()
        self.emergency_stop = False
        self.emergency_reason = ""

    # ------------------------------------------------------------- the AI seat

    async def _ai_filter(
        self,
        db: AsyncSession,
        *,
        strategy_key: str,
        account_id: str,
        symbol: str,
        timeframe: object,
    ) -> IntegrationFilter | None:
        """The AI seat for one bot, or None when nothing is configured. L27.

        Returns None for AI_DISABLED rather than a filter that accepts
        everything, and the difference is not cosmetic: a filter would run,
        journal a row and appear in the counters, so a reader could not tell a
        disabled deployment from one whose model agrees with everything. §7
        asks that AI_DISABLED behave *exactly* as the deterministic strategy,
        and the cheapest way to guarantee that is for there to be no AI object.

        **A configuration naming an ineligible model refuses the bot rather
        than downgrading it.** §28: fallback is never implicit. Starting the bot
        with the AI quietly off would be exactly the silent change §12 forbids.
        """
        config = await ai_config.config_for(db, strategy_key, account_id=account_id)
        if config.mode is AiMode.disabled:
            return None
        try:
            await ai_config.check_models_are_eligible(db, config)
            # L28 §28: models reach a trading decision THROUGH THE REGISTRY.
            # Resolved once here rather than per signal, so the version a run
            # used is a fixed fact for the life of the run -- which is what §37
            # and §39 need in order to say which model produced a decision --
            # and so the AI pipeline never opens a session per bar (§45).
            resolver = await RegistryResolver.build(
                db,
                config,
                symbol=symbol,
                timeframe=str(timeframe),
                environment="paper",
            )
        except AiIntegrationError as exc:
            raise BotNotRunnable(str(exc)) from exc

        service = AiIntegrationService(resolver)
        return IntegrationFilter(
            service,
            config,
            # The paper engine passes a `SignalContext` straight through, so
            # this is never called for a paper bot. It is here so the seat
            # refuses rather than raises if a caller ever hands it a raw signal.
            context_for=_no_raw_signal,
        )

    async def _journal_ai(self, db: AsyncSession, bot: RunningBot, outcome: Pass | None) -> None:
        """Drain the seat's decisions into `ai_decisions`. §35.

        Shaped exactly like `pending_verdicts` above: the engine (and the filter
        it holds) collect, and the service — which has the session — writes.
        Every decision leaves a row, including the ones that changed nothing: a
        decision that leaves no trace is indistinguishable from an AI nobody
        asked, and the two have opposite meanings when somebody later counts how
        often the layer answered.
        """
        seat = getattr(bot.engine, "ai", None)
        decisions = list(getattr(seat, "decisions", []) or [])
        if not decisions:
            return
        seat.decisions.clear()  # type: ignore[union-attr]
        for decision, context in decisions:
            record = ai_config.record_of(
                decision,
                context,
                bot_id=bot.engine.bot_id,
                account_id=bot.account_id,
                trading_mode="paper",
            )
            # What happened AFTER the AI layer, on the same row, so "the AI
            # accepted and risk vetoed" is one fact rather than a correlation.
            if outcome is not None:
                record.final_outcome = str(outcome.outcome)[:32]
            db.add(record)

    # ------------------------------------------------------------- accounts

    async def create_account(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        name: str,
        currency: str,
        starting_balance: Decimal,
    ) -> PaperAccount:
        if starting_balance <= 0:
            raise ValueError("initial virtual capital must be positive")
        row = PaperAccount(
            user_id=user_id,
            name=name,
            currency=currency.upper(),
            starting_balance=starting_balance,
            balance=starting_balance,
            equity=starting_balance,
            status=str(AccountState.created),
            is_active=False,
        )
        db.add(row)
        await db.flush()
        return row

    async def get_account(self, db: AsyncSession, account_id: str, user_id: str) -> PaperAccount:
        row = await db.get(PaperAccount, account_id)
        # Someone else's account answers exactly as one that does not exist.
        if row is None or row.user_id != user_id:
            raise KeyError(account_id)
        return row

    async def move_account(
        self, db: AsyncSession, row: PaperAccount, wanted: AccountState
    ) -> PaperAccount:
        current = AccountState(row.status)
        check_transition(current, wanted)
        row.status = str(wanted)
        row.is_active = wanted is AccountState.active
        if wanted in (AccountState.paused, AccountState.disabled, AccountState.closed):
            # Stopping the account stops anything trading it. An account that
            # is not tradeable with a bot still running would be a contradiction
            # the next pass would have to resolve by accident.
            for bot in [b for b in self.live.values() if b.account_id == row.id]:
                await self.stop_bot(bot, reason=f"account moved to {wanted}")
        return row

    async def reset_account(
        self, db: AsyncSession, row: PaperAccount, *, starting_balance: Decimal | None = None
    ) -> dict[str, object]:
        """Explicit reset. Never silent, and never a delete.

        Prior orders, positions and trades are ARCHIVED by being left exactly
        where they are and stamped with the reset -- this platform does not
        hard-delete a trading record, because a record that can vanish cannot
        be audited. What resets is the balance and the statistics.
        """
        for bot in [b for b in self.live.values() if b.account_id == row.id]:
            await self.stop_bot(bot, reason="account reset")

        stamp = utcnow()
        archived = {
            "orders": await self._count(db, Order, row.id),
            "positions": await self._count(db, Position, row.id),
        }
        # Open positions are marked closed-by-reset rather than left dangling
        # against a balance that no longer reflects them.
        open_rows = (
            await db.scalars(
                select(Position).where(
                    Position.paper_account_id == row.id, Position.status == "open"
                )
            )
        ).all()
        for position in open_rows:
            position.status = "closed"
            position.closed_at = stamp

        row.starting_balance = starting_balance or row.starting_balance
        row.balance = row.starting_balance
        row.equity = row.starting_balance
        row.realized_pnl = Decimal("0")
        row.unrealized_pnl = Decimal("0")
        row.commission_paid = Decimal("0")
        row.reset_at = stamp
        row.reset_count = (row.reset_count or 0) + 1
        return {
            "reset_at": stamp.isoformat(),
            "reset_count": row.reset_count,
            "archived": archived,
            "positions_closed_by_reset": len(open_rows),
            "note": (
                "Prior orders, executions and trades are preserved. A paper "
                "record is never hard-deleted; it is archived in place."
            ),
        }

    @staticmethod
    async def _count(db: AsyncSession, model: Any, account_id: str) -> int:
        """How many rows of `model` this account owns. Counted, never estimated."""
        total = await db.scalar(
            select(func.count()).select_from(model).where(model.paper_account_id == account_id)
        )
        return int(total or 0)

    # ----------------------------------------------------------------- bots

    async def start_bot(
        self,
        db: AsyncSession,
        *,
        bot: Bot,
        user_id: str,
        symbol: str,
        timeframe: Timeframe,
        provider: Provider,
        costs: CostModel,
        limits: RiskLimits,
        sizing_method: SizingMethod,
        quantity: Decimal | None,
        risk_amount: Decimal | None,
        risk_percent: Decimal | None,
    ) -> RunningBot:
        if self.emergency_stop:
            raise BotNotRunnable(
                f"emergency stop is engaged: {self.emergency_reason or 'no reason recorded'}"
            )
        if bot.id in self.bot_kill:
            raise BotNotRunnable(f"a kill switch is engaged on bot {bot.id}")
        # The global, account and strategy scopes too. Without this a global
        # kill switch stopped the running bots but did not stop one being
        # started again -- every order would then have been vetoed by risk,
        # which is safe but reads as working, and a switch that looks off when
        # it is on is worse than one that refuses loudly.
        engaged = self.switches.engaged_for(
            bot.paper_account_id,
            bot.strategy_version_id or str((bot.config or {}).get("strategy_key", "")),
        )
        if engaged is not None:
            raise BotNotRunnable(f"a kill switch is engaged: {engaged[1]}")
        if bot.id in self.live and self.live[bot.id].status in ("running", "paused"):
            raise BotNotRunnable(f"bot {bot.id} is already {self.live[bot.id].status}")
        mine = [b for b in self.live.values() if b.user_id == user_id and b.status == "running"]
        if len(mine) >= MAX_BOTS_PER_USER:
            raise PaperBusy(
                f"{len(mine)} paper bots are already running; the limit is {MAX_BOTS_PER_USER}"
            )
        if bot.paper_account_id is None:
            raise BotNotRunnable("a paper bot needs a paper account")

        account = await self.get_account(db, bot.paper_account_id, user_id)
        if AccountState(account.status) is not AccountState.active:
            raise BotNotRunnable(
                f"the paper account is {account.status}; activate it before starting a bot"
            )

        spec = await self.symbols.contract_spec(db, symbol, str(provider))  # type: ignore[attr-defined]
        strategy = self.registry.create(  # type: ignore[attr-defined]
            str((bot.config or {}).get("strategy_key", "")), (bot.config or {}).get("config") or {}
        )
        portfolio = await load_portfolio(db, account)
        await self._restore_positions(db, portfolio, account.id, spec)

        engine = PaperEngine(
            portfolio=portfolio,
            strategy=strategy,
            spec=spec,
            risk=RiskEngine(limits, self.switches),
            costs=costs,
            timeframe=timeframe,
            sizing_method=sizing_method,
            quantity=quantity,
            risk_amount=risk_amount,
            risk_percent=risk_percent,
            bot_id=bot.id,
            strategy_id=bot.strategy_version_id or str((bot.config or {}).get("strategy_key", "")),
            # L27. None when the strategy is AI_DISABLED, which is the default
            # for a strategy nobody has configured.
            ai=await self._ai_filter(
                db,
                strategy_key=str((bot.config or {}).get("strategy_key", "")),
                account_id=account.id,
                symbol=symbol,
                timeframe=timeframe,
            ),
        )
        # Restart recovery: a bar already ordered against can never order again.
        engine.seen_signals |= await self._known_intents(db, account.id)

        run = BotRun(
            bot_id=bot.id,
            started_at=utcnow(),
            status="starting",
            host="backend",
            summary={
                "symbol": symbol,
                "timeframe": str(timeframe),
                "provider": str(provider),
                "execution_mode": "PAPER",
                "execution_provider": str(ExecutionProvider.paper_execution_only),
                "recovered_intents": len(engine.seen_signals),
                "recovered_positions": len(portfolio.positions),
            },
        )
        db.add(run)
        await db.flush()

        frozen = {
            "strategy_key": (bot.config or {}).get("strategy_key"),
            "strategy_version_id": bot.strategy_version_id,
            "strategy_config": (bot.config or {}).get("config") or {},
            "symbol": symbol,
            "timeframe": str(timeframe),
            "provider": str(provider),
            "costs": costs.describe(),
            "sizing": {
                "method": str(sizing_method),
                "quantity": str(quantity) if quantity is not None else None,
                "risk_amount": str(risk_amount) if risk_amount is not None else None,
                "risk_percent": str(risk_percent) if risk_percent is not None else None,
            },
            "limits": {k: (str(v) if v is not None else None) for k, v in vars(limits).items()},
        }
        running = RunningBot(
            id=bot.id,
            user_id=user_id,
            account_id=account.id,
            engine=engine,
            symbol=symbol,
            timeframe=timeframe,
            provider=provider,
            run_id=run.id,
            config=frozen,
        )
        self.live[bot.id] = running
        bot.is_enabled = True
        await self._bot_event(db, run.id, "bot_started", {"frozen_config": frozen})
        await db.commit()

        running.status = "running"
        run_id = run.id
        async with self.sessions() as s2:
            row = await s2.get(BotRun, run_id)
            if row is not None:
                row.status = "running"
                await s2.commit()
        running._resume.set()
        running._task = asyncio.create_task(self._drive(running), name=f"paper:{bot.id}")
        await self._publish(running, EventType.BOT_STARTED, {"status": "running"})
        return running

    async def _restore_positions(
        self, db: AsyncSession, portfolio: PaperPortfolio, account_id: str, spec: object
    ) -> None:
        """Reopen positions the database says are still open.

        Without this a restart would look flat and the strategy would open a
        second position on top of one that already exists.
        """
        from app.paper.portfolio import PaperPosition

        rows = (
            await db.scalars(
                select(Position).where(
                    Position.paper_account_id == account_id, Position.status == "open"
                )
            )
        ).all()
        if not rows:
            return
        per_unit = value_per_price_unit(
            getattr(spec, "tick_value", None), getattr(spec, "tick_size", None)
        )
        symbol = getattr(spec, "internal_symbol", "")
        for row in rows:
            portfolio.positions[symbol] = PaperPosition(
                symbol=symbol,
                side=row.side,
                quantity=row.quantity,
                average_entry=row.entry_price,
                opened_at=from_storage(row.opened_at),
                updated_at=from_storage(row.opened_at),
                value_per_unit=per_unit,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
            )

    @staticmethod
    async def _known_intents(db: AsyncSession, account_id: str) -> set[str]:
        rows = (
            await db.scalars(select(Order.intent_id).where(Order.paper_account_id == account_id))
        ).all()
        return set(rows)

    # ------------------------------------------------------------- controls

    def get_bot(self, bot_id: str, user_id: str) -> RunningBot:
        bot = self.live.get(bot_id)
        if bot is None or bot.user_id != user_id:
            raise KeyError(bot_id)
        return bot

    async def pause_bot(self, bot: RunningBot) -> RunningBot:
        if bot.status not in ("running",):
            raise BotNotRunnable(f"only a running bot can be paused, not a {bot.status} one")
        bot._resume.clear()
        bot.status = str(BotState.paused)
        # FIXED AT L22. This wrote `stopping`, because `bot_runs.status` had no
        # value for `paused` -- so a paused bot and a bot shutting down were
        # the same row, and after a restart nothing could tell them apart. A
        # supervisor reading that row would either resume a bot somebody had
        # deliberately paused or abandon one that was only mid-shutdown.
        await self._set_run_status(bot, str(BotState.paused), "paused by the user")
        await self._publish(bot, EventType.BOT_PAUSED, {"status": "paused"})
        return bot

    async def resume_bot(self, bot: RunningBot) -> RunningBot:
        if bot.status != str(BotState.paused):
            raise BotNotRunnable(f"only a paused bot can be resumed, not a {bot.status} one")
        if self.emergency_stop or bot.id in self.bot_kill:
            raise BotNotRunnable("a kill switch or emergency stop is engaged")
        bot.status = "running"
        await self._set_run_status(bot, "running", None)
        bot._resume.set()
        await self._publish(bot, EventType.BOT_STARTED, {"status": "running"})
        return bot

    async def stop_bot(self, bot: RunningBot, reason: str = "stopped by the user") -> RunningBot:
        bot.status = "stopped"
        bot.stop_reason = reason
        bot._resume.set()
        if bot._task is not None:
            bot._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bot._task
        await self._set_run_status(bot, "stopped", reason)
        await self._publish(bot, EventType.BOT_STOPPED, {"status": "stopped", "reason": reason})
        self.live.pop(bot.id, None)
        return bot

    # --------------------------------------------------------- kill switches

    async def engage_kill_switch(
        self, *, scope: str, target: str | None, reason: str
    ) -> dict[str, object]:
        """Stop new orders. Positions are preserved for review, never auto-closed.

        Closing on a kill switch would be trading a decision nobody made, at a
        price nobody chose, at the exact moment something is known to be wrong.
        """
        if scope == "global":
            self.switches = KillSwitches(global_stop=True, global_reason=reason)
        elif scope == "account" and target:
            self.switches = KillSwitches(
                global_stop=self.switches.global_stop,
                global_reason=self.switches.global_reason,
                accounts=self.switches.accounts | {target},
                strategies=self.switches.strategies,
            )
        elif scope == "strategy" and target:
            self.switches = KillSwitches(
                global_stop=self.switches.global_stop,
                global_reason=self.switches.global_reason,
                accounts=self.switches.accounts,
                strategies=self.switches.strategies | {target},
            )
        elif scope == "bot" and target:
            self.bot_kill.add(target)
        else:
            raise ValueError(f"unknown kill-switch scope {scope!r} or missing target")

        for bot in list(self.live.values()):
            bot.engine.risk.switches = self.switches
        stopped: list[str] = []
        for bot in list(self.live.values()):
            if scope == "global" or (scope == "account" and bot.account_id == target):
                await self.stop_bot(bot, reason=f"kill switch ({scope}): {reason}")
                stopped.append(bot.id)
            elif scope == "bot" and bot.id == target:
                await self.stop_bot(bot, reason=f"kill switch (bot): {reason}")
                stopped.append(bot.id)
        return {
            "scope": scope,
            "target": target,
            "reason": reason,
            "bots_stopped": stopped,
            "positions": "preserved for review; a kill switch never closes a position",
        }

    async def release_kill_switch(self, *, scope: str, target: str | None) -> dict[str, object]:
        if scope == "global":
            self.switches = KillSwitches(
                accounts=self.switches.accounts, strategies=self.switches.strategies
            )
        elif scope == "account" and target:
            self.switches = KillSwitches(
                global_stop=self.switches.global_stop,
                global_reason=self.switches.global_reason,
                accounts=self.switches.accounts - {target},
                strategies=self.switches.strategies,
            )
        elif scope == "strategy" and target:
            self.switches = KillSwitches(
                global_stop=self.switches.global_stop,
                global_reason=self.switches.global_reason,
                accounts=self.switches.accounts,
                strategies=self.switches.strategies - {target},
            )
        elif scope == "bot" and target:
            self.bot_kill.discard(target)
        else:
            raise ValueError(f"unknown kill-switch scope {scope!r} or missing target")
        for bot in list(self.live.values()):
            bot.engine.risk.switches = self.switches
        return self.switch_state()

    async def engage_emergency_stop(self, reason: str) -> dict[str, object]:
        self.emergency_stop = True
        self.emergency_reason = reason
        stopped = []
        for bot in list(self.live.values()):
            await self.stop_bot(bot, reason=f"emergency stop: {reason}")
            stopped.append(bot.id)
        log.warning(
            "paper emergency stop engaged",
            extra={"event": "paper_emergency_stop", "bots_stopped": len(stopped)},
        )
        return {
            "engaged": True,
            "reason": reason,
            "bots_stopped": stopped,
            "positions": "preserved. Nothing is closed automatically.",
        }

    def switch_state(self) -> dict[str, object]:
        return {
            "global": self.switches.global_stop,
            "global_reason": self.switches.global_reason,
            "accounts": sorted(self.switches.accounts),
            "strategies": sorted(self.switches.strategies),
            "bots": sorted(self.bot_kill),
            "emergency_stop": self.emergency_stop,
            "emergency_reason": self.emergency_reason,
        }

    # -------------------------------------------------------------- driving

    async def one_pass(self, bot: RunningBot) -> Pass | None:
        """Fetch, evaluate, execute and persist. One transaction."""
        async with self.sessions() as db:
            series = await self.market_data.get_bars(  # type: ignore[attr-defined]
                db, bot.symbol, bot.timeframe, bot.provider, limit=400
            )
            bars = [b for b in series.series.bars if b.complete]
            if not bars:
                return None
            # Domain time: aware, so it can be compared with bar times.
            now = now_utc()

            # The Risk Service is the authority. Before any work: a latched
            # lock refuses the pass outright, and the effective limits are
            # resolved from the hierarchy -- the bot's own configuration goes
            # in as a layer, so it can tighten the account's limits and can
            # never loosen them.
            if self.risk is not None:
                locked = await self.risk.precheck(db, account_id=bot.account_id)
                if locked is not None:
                    blocked = Pass(
                        Outcome.kill_switch,
                        now,
                        bot.symbol,
                        f"{locked.state}: {locked.reason}"[:300],
                    )
                    bot.engine.counters.record(blocked.outcome)
                    bot.recent.append(blocked)
                    del bot.recent[:-PASS_BUFFER]
                    return blocked
                resolved = await self.risk.limits_for(
                    db,
                    account_id=bot.account_id,
                    strategy_id=bot.engine.strategy_id,
                    symbol=bot.symbol,
                    caller=bot.engine.risk.limits,
                )
                bot.engine.risk = RiskEngine(resolved.limits, self.risk.switches)
                bot.engine.configuration_version = resolved.version

            # Brackets first: an open position's stop or target is checked
            # before a new signal is considered, so a reversal cannot jump the
            # exit that the same bar already triggered.
            closed = bot.engine.check_brackets(bars[-1], now)
            bot.engine.pending_verdicts.clear()
            result = bot.engine.process(bars, now=now)

            # Every verdict, approvals included, becomes a durable coded record.
            if self.risk is not None:
                for verdict in bot.engine.pending_verdicts:
                    await self.risk.record_verdict(
                        db,
                        verdict,
                        account_id=bot.account_id,
                        execution_mode="paper",
                        configuration_version=bot.engine.configuration_version,
                    )
                bot.engine.pending_verdicts.clear()
            for outcome in (closed, result):
                if outcome is not None:
                    await self._persist(db, bot, outcome)
            await self._journal_ai(db, bot, result)
            equity = bot.engine.portfolio.equity(bot.engine._prices())
            await persist_account(db, bot.engine.portfolio, equity)
            await db.commit()

        for outcome in (closed, result):
            if outcome is not None:
                bot.recent.append(outcome)
                await self._publish_pass(bot, outcome)
        del bot.recent[:-PASS_BUFFER]
        return result

    async def _drive(self, bot: RunningBot) -> None:
        try:
            while bot.status in ("running", "paused"):
                await bot._resume.wait()
                if bot.status != "running":
                    return
                await self.one_pass(bot)
                await asyncio.sleep(self._sleep_for(bot.timeframe))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bot's failure is its own
            log.warning(
                "paper bot failed",
                extra={
                    "event": "paper_bot_failed",
                    "bot_id": bot.id,
                    "paper_account_id": bot.account_id,
                    "error": type(exc).__name__,
                    "execution_mode": "PAPER",
                },
            )
            bot.error = f"{type(exc).__name__}: {exc}"[:300]
            bot.status = "crashed"
            await self._set_run_status(bot, "crashed", bot.error)
            await self._publish(bot, EventType.BOT_ERROR, {"error": bot.error})

    @staticmethod
    def _sleep_for(timeframe: Timeframe) -> float:
        """Poll at the timeframe's cadence, capped so controls stay responsive."""
        from app.marketdata.types import seconds_of

        try:
            cadence = float(seconds_of(timeframe)) / 20.0
        except Exception:  # noqa: BLE001
            cadence = MAX_SLEEP_SECONDS
        return max(MIN_SLEEP_SECONDS, min(MAX_SLEEP_SECONDS, cadence))

    # ----------------------------------------------------------- persistence

    async def _persist(self, db: AsyncSession, bot: RunningBot, result: Pass) -> None:
        """Write one pass. Called inside the caller's transaction, never its own."""
        symbol_id = await self.market_data.symbol_id_for(db, result.symbol)  # type: ignore[attr-defined]
        if symbol_id is None:
            return

        # Risk decisions are written by `RiskService.record_verdict`, which
        # carries the decision id, the codes and the configuration version.
        # Writing a second row here would put two records of one decision in
        # the same table, and a reader could not tell which was authoritative.
        if self.risk is None and result.risk is not None:
            db.add(
                RiskEvent(
                    decision=str(result.risk.decision),
                    reason=result.risk.reason[:2000],
                    occurred_at=result.at.replace(tzinfo=None),
                    snapshot={"bot_id": bot.id, "run_id": bot.run_id},
                    mode="paper",
                    paper_account_id=bot.account_id,
                )
            )

        order = result.order
        if order is not None and result.outcome is not Outcome.duplicate_signal:
            row = Order(
                intent_id=order.intent_id,
                mode="paper",
                paper_account_id=bot.account_id,
                symbol_id=symbol_id,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                requested_price=order.requested_price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                status=str(order.status),
                source="pipeline",
                submitted_at=(order.submitted_at or result.at).replace(tzinfo=None),
                sizing=order.sizing_snapshot,
            )
            db.add(row)
            await db.flush()
            for at, status, detail in order.events:
                db.add(
                    OrderEvent(
                        order_id=row.id,
                        event_type=status[:24],
                        occurred_at=at.replace(tzinfo=None),
                        comment=detail[:500] or None,
                    )
                )
            if order.fill is not None:
                db.add(
                    Execution(
                        order_id=row.id,
                        executed_at=order.fill.at.replace(tzinfo=None),
                        price=order.fill.price,
                        quantity=order.fill.quantity,
                        commission=order.fill.commission,
                        slippage_points=order.fill.slippage_points,
                        # Never 'broker'. A paper fill is a simulator fill.
                        fill_source="simulator",
                    )
                )

        await self._persist_positions(db, bot, result, symbol_id)

    async def _persist_positions(
        self, db: AsyncSession, bot: RunningBot, result: Pass, symbol_id: str
    ) -> None:
        open_rows = {
            row.id: row
            for row in (
                await db.scalars(
                    select(Position).where(
                        Position.paper_account_id == bot.account_id,
                        Position.status == "open",
                        Position.symbol_id == symbol_id,
                    )
                )
            ).all()
        }
        live = bot.engine.portfolio.positions.get(result.symbol)

        # L31. The journal row is written here because the simulator is the only
        # thing that knows what closed: the paper engine books its own fills and
        # writes no `position_events`, so the L31 sweep would find a closed
        # position with no confirmed close and correctly refuse to invent one.
        #
        # Attribution is linked where it can be established and left NULL where
        # it cannot. One simulated close against one open row is the ordinary
        # shape and is linked; anything else records the ambiguity rather than
        # picking a row, because a trade attributed to the wrong position is
        # worse than one attributed to none.
        linkable = len(result.closed) == 1 and len(open_rows) == 1
        linked_id = next(iter(open_rows), None) if linkable else None
        for trade in result.closed:
            db.add(
                Trade(
                    mode="paper",
                    status="closed",
                    symbol_id=symbol_id,
                    # §30. Carried on the trade, not reached through the
                    # position: a deleted position must not take the account
                    # attribution of a historical trade with it.
                    paper_account_id=bot.account_id,
                    position_id=linked_id,
                    strategy_version_id=(
                        open_rows[linked_id].strategy_version_id if linked_id else None
                    ),
                    side=trade.side,
                    volume=trade.quantity,
                    entry_price=trade.entry_price,
                    exit_price=trade.exit_price,
                    opened_at=trade.opened_at.replace(tzinfo=None),
                    closed_at=trade.closed_at.replace(tzinfo=None),
                    gross_profit=trade.gross_pnl,
                    commission=trade.commission,
                    swap=Decimal("0"),
                    net_profit=trade.net_pnl,
                    # §17. L21's vocabulary, from the simulator's own reason
                    # where it recorded one -- never guessed at.
                    exit_reason=journal_exit(trade.exit_reason),
                    currency=bot.engine.portfolio.currency,
                    source="pipeline",
                    data_quality=(
                        None
                        if linkable
                        else {
                            "checked": True,
                            "findings": [
                                {
                                    "code": "position_not_linked",
                                    "severity": "warning",
                                    "detail": (
                                        f"{len(result.closed)} simulated close(s) against "
                                        f"{len(open_rows)} open position row(s); which "
                                        "closed which cannot be established, so no link "
                                        "was guessed."
                                    ),
                                }
                            ],
                            "errors": 0,
                            "warnings": 1,
                        }
                    ),
                )
            )
        if result.closed:
            for row in open_rows.values():
                row.status = "closed"
                row.closed_at = result.at.replace(tzinfo=None)

        if live is not None and (not open_rows or result.closed):
            db.add(
                Position(
                    mode="paper",
                    paper_account_id=bot.account_id,
                    symbol_id=symbol_id,
                    side=live.side,
                    quantity=live.quantity,
                    # What it opened at. `quantity` is what is open NOW, and
                    # the two diverge the moment anything is scaled out.
                    initial_quantity=live.quantity,
                    entry_price=live.average_entry,
                    stop_loss=live.stop_loss,
                    take_profit=live.take_profit,
                    status="open",
                    opened_at=live.opened_at.replace(tzinfo=None),
                    source="pipeline",
                )
            )
        elif live is not None:
            for row in open_rows.values():
                row.quantity = live.quantity
                row.entry_price = live.average_entry
                row.stop_loss = live.stop_loss
                row.take_profit = live.take_profit

    async def _set_run_status(self, bot: RunningBot, status: str, reason: str | None) -> None:
        async with self.sessions() as db:
            row = await db.get(BotRun, bot.run_id)
            if row is None:
                return
            row.status = status
            if reason:
                row.stop_reason = reason
            if status in {str(s) for s in FINISHED}:
                row.ended_at = utcnow()
                row.summary = {**(row.summary or {}), **bot.engine.state()}
            row.last_heartbeat_at = utcnow()
            await self._bot_event(db, bot.run_id, f"bot_{status}", {"reason": reason})
            await db.commit()

    @staticmethod
    async def _bot_event(
        db: AsyncSession, run_id: str, event_type: str, payload: dict[str, object]
    ) -> None:
        db.add(
            BotEvent(
                bot_run_id=run_id,
                event_type=event_type[:32],
                level="info",
                occurred_at=utcnow(),
                payload=payload,
            )
        )

    # -------------------------------------------------------------- realtime

    async def _publish_pass(self, bot: RunningBot, result: Pass) -> None:
        if result.outcome in NO_ORDER and result.outcome not in (
            Outcome.risk_vetoed,
            Outcome.risk_halted,
            Outcome.kill_switch,
            Outcome.market_data_stale,
        ):
            return
        mapping = {
            Outcome.filled: EventType.ORDER_FILLED,
            Outcome.position_closed: EventType.POSITION_CLOSED,
            Outcome.risk_vetoed: EventType.RISK_REJECTED,
            Outcome.risk_halted: EventType.RISK_REJECTED,
            Outcome.kill_switch: EventType.RISK_REJECTED,
            Outcome.market_data_stale: EventType.SYSTEM_ALERT,
        }
        await self._publish(
            bot, mapping.get(result.outcome, EventType.SYSTEM_ALERT), result.as_dict()
        )

    async def _publish(
        self, bot: RunningBot, event_type: object, payload: dict[str, object]
    ) -> None:
        if self.hub is None:
            return
        try:
            await self.hub.publish(  # type: ignore[attr-defined]
                Event(
                    type=str(event_type),
                    payload={
                        **payload,
                        "bot_id": bot.id,
                        "paper_account_id": bot.account_id,
                        "execution_mode": "PAPER",
                    },
                    channel=str(Channel(Scope.user, bot.user_id)),
                    source="paper",
                )
            )
        except Exception:  # noqa: BLE001 - a lost event is not a lost trade
            log.warning(
                "paper event not published",
                extra={"event": "paper_publish_failed", "bot_id": bot.id},
            )

    async def shutdown(self) -> None:
        for bot in list(self.live.values()):
            if bot._task is not None and not bot._task.done():
                bot._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await bot._task


def _no_raw_signal(signal: object) -> object:
    """The paper engine always supplies a `SignalContext`; nothing else may.

    Raising here rather than building a context from a raw signal is the point:
    a context built from a signal alone would have no bars, so the AI layer
    would refuse for want of features and the reason would read as a model
    problem rather than a wiring one.
    """
    raise TypeError(
        f"the AI seat was handed a {type(signal).__name__} rather than a SignalContext. "
        "The paper engine builds one from the closed bars the strategy saw; nothing "
        "else may, because a context assembled elsewhere is a window nobody checked."
    )
