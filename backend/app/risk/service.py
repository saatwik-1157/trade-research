"""The Risk Service: configuration, state, persistence, and the race guard.

`RiskEngine` is a pure function of (proposal, portfolio, limits). That is what
makes it testable, and it is also what makes it insufficient on its own: a pure
function has no memory, and three of this level's requirements are about memory.

**Locks latch and survive a restart.** A breached daily loss is a *state*, not
a failing check. Recomputing the check from a portfolio snapshot would let an
unrealised bounce reopen trading on a limit that was already breached. So the
breach latches into `risk_states`, and `load()` reads it back before the
process evaluates anything.

**Approvals reserve.** Two orders that are each individually safe can be
collectively unsafe: with 40% exposure and a 50% limit, two 8% orders both pass
against the same stale snapshot and land at 56%. So an approval **reserves** its
exposure and its risk until the order fills or the approval expires, and the
next evaluation counts the reservation. The evaluation and the reservation
happen under one lock per account, so they cannot interleave.

**Failure is closed.** If a check raises, if the configuration will not load,
if the decision cannot be persisted -- the answer is a refusal. There is no
path through this module that approves an order because something went wrong.
`test_the_risk_engine_fails_closed` drives each of those faults.

The asyncio lock is honest about its scope: it serialises within this process,
which is where the bots run. It is not a distributed lock, and the limitation
is documented rather than papered over -- a second backend process would need
`SELECT ... FOR UPDATE` on the state row, and this deployment does not have one.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.risk import RiskEvent, RiskRule
from app.risk.config import (
    Layer,
    Resolved,
    Scope,
    check_valid,
    resolve,
)
from app.risk.decision import (
    APPROVAL_TTL_SECONDS,
    CheckRecord,
    RejectionCode,
    RiskDecision,
    RiskOutcome,
    WarningCode,
    expiry,
    request_hash,
)
from app.risk.engine import (
    KillSwitches,
    LimitKind,
    OrderProposal,
    PortfolioState,
    RiskEngine,
    RiskLimits,
)
from app.risk.engine import RiskDecision as EngineDecision
from app.risk.state import (
    AccountRiskState,
    RiskState,
    day_start,
)

log = logging.getLogger("app.risk.service")

# Which rejection code a failed limit reports. Total over LimitKind; a test
# asserts it, so a limit added later cannot fall back to a generic string.
CODE_FOR: dict[LimitKind, RejectionCode] = {
    LimitKind.kill_switch_global: RejectionCode.kill_switch_active,
    LimitKind.kill_switch_account: RejectionCode.kill_switch_active,
    LimitKind.kill_switch_strategy: RejectionCode.kill_switch_active,
    LimitKind.trading_mode: RejectionCode.trading_mode_blocked,
    LimitKind.market_open: RejectionCode.market_closed,
    LimitKind.market_data: RejectionCode.market_data_stale,
    LimitKind.signal_freshness: RejectionCode.signal_too_old,
    LimitKind.duplicate_signal: RejectionCode.duplicate_signal,
    LimitKind.max_open_positions: RejectionCode.max_open_positions_exceeded,
    LimitKind.one_position_per_symbol: RejectionCode.position_already_open,
    LimitKind.max_trades_per_day: RejectionCode.trade_frequency_exceeded,
    LimitKind.max_daily_loss: RejectionCode.max_daily_loss_exceeded,
    LimitKind.max_weekly_loss: RejectionCode.max_weekly_loss_exceeded,
    LimitKind.max_consecutive_losses: RejectionCode.max_consecutive_losses_reached,
    LimitKind.max_correlated_exposure: RejectionCode.max_correlated_exposure_exceeded,
    LimitKind.max_drawdown: RejectionCode.max_drawdown_exceeded,
    LimitKind.max_exposure: RejectionCode.max_exposure_exceeded,
    LimitKind.max_leverage: RejectionCode.max_leverage_exceeded,
    LimitKind.max_risk_per_trade: RejectionCode.max_risk_per_trade_exceeded,
    LimitKind.spread: RejectionCode.spread_too_wide,
    LimitKind.position_size: RejectionCode.invalid_quantity,
    LimitKind.stop_loss_required: RejectionCode.stop_loss_required,
    LimitKind.account_state: RejectionCode.account_state_invalid,
    LimitKind.bot_state: RejectionCode.bot_not_running,
    LimitKind.strategy_state: RejectionCode.strategy_disabled,
    LimitKind.symbol_state: RejectionCode.symbol_not_tradable,
    LimitKind.max_position_size: RejectionCode.max_position_size_exceeded,
    LimitKind.max_concentration: RejectionCode.max_concentration_exceeded,
    LimitKind.margin: RejectionCode.insufficient_margin,
    LimitKind.trade_frequency: RejectionCode.trade_frequency_exceeded,
    LimitKind.cooldown: RejectionCode.cooldown_active,
    LimitKind.risk_reward: RejectionCode.risk_reward_too_low,
}

# Limits whose breach stops the session rather than just this order, and the
# state each one latches into.
LATCHING: dict[LimitKind, RiskState] = {
    LimitKind.max_daily_loss: RiskState.daily_loss_locked,
    # Its own state, not the daily one. `refresh()` clears a daily lock when
    # the day ends, so a week latched there would release itself overnight
    # while the week was still breached.
    LimitKind.max_weekly_loss: RiskState.weekly_loss_locked,
    LimitKind.max_drawdown: RiskState.drawdown_locked,
    # max_consecutive_losses is deliberately ABSENT. A streak clears
    # itself on the next win, so latching it would turn a pause that
    # ends by itself into a lock that needs a human.
}

# A signal is at most one bar old by construction. Two bars of slack covers
# processing without letting a genuinely stale signal through, and it replaces
# the fixed 300s ceiling that vetoed every H1 strategy at L16.
SIGNAL_AGE_BARS = 2
DEFAULT_SIGNAL_AGE_SECONDS = 600.0

RULE_TYPES = (
    "max_positions",
    "one_per_symbol",
    "max_daily_loss",
    "max_exposure_currency",
    "max_risk_per_trade",
    "kill_switch",
    "mode_gate",
    "limits",
)


class RiskUnavailable(Exception):
    """The engine could not reach a decision. Always a refusal, never a pass."""


@dataclass
class Reservation:
    """Exposure an approval has claimed but no fill has consumed yet.

    Without this, two orders evaluated against the same snapshot both see the
    same headroom and both take it. Counted into the next evaluation and
    released on fill, on cancel, or when the approval expires -- so a crashed
    caller cannot hold a reservation forever.
    """

    decision_id: str
    account_id: str
    exposure: Decimal
    risk_amount: Decimal
    positions: int
    at: datetime
    expires_at: datetime

    def live(self, now: datetime) -> bool:
        return now <= self.expires_at


@dataclass
class RiskRequest:
    """What the caller wants to do, plus the facts the checks need."""

    proposal: OrderProposal
    portfolio: PortfolioState = field(default_factory=PortfolioState)
    execution_mode: str = "paper"
    timeframe_seconds: int | None = None
    recent_signal_ids: tuple[str, ...] = ()
    order_type: str = "market"

    def bound_fields(self) -> dict[str, object]:
        p = self.proposal
        return {
            "account_id": p.account_id,
            "symbol": p.symbol,
            "side": p.side,
            "volume": p.volume,
            "order_type": self.order_type,
            "entry_price": p.entry_price,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "strategy_id": p.strategy_id,
            "mode": self.execution_mode,
        }


class RiskService:
    """The central safety authority. One per process; all state on the instance."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        *,
        live_trading: bool = False,
        trading_mode: str = "paper",
        day_boundary_hour: int = 0,
    ) -> None:
        self.sessions = sessions
        self.live_trading = live_trading
        self.trading_mode = trading_mode
        self.day_boundary_hour = day_boundary_hour
        self.states: dict[str, AccountRiskState] = {}
        self.switches = KillSwitches()
        self._reservations: dict[str, list[Reservation]] = defaultdict(list)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._recent: deque[RiskDecision] = deque(maxlen=200)
        self._config_cache: dict[str, Resolved] = {}
        self.loaded = False

    # ================================================== restart recovery

    def engine_for_close(self) -> RiskEngine:
        """The engine a CLOSE is approved by. **L45 C-2.**

        Built on default limits rather than an account's stored ones, and that
        is sound here for one specific reason: `RiskEngine.approve_close`
        enforces exactly one check, `LimitKind.trading_mode`, which reads the
        proposal's own mode and not the configured limits. Every other limit is
        evaluated, recorded and stamped `not_enforced`, because a limit that
        exists to bound the risk of TAKING a position must not be able to
        refuse a reduction of it.

        The kill switches ARE passed. Their halt is recorded on the close's
        verdict, and so reaches the order's risk snapshot, without being able
        to trap an open position behind it -- a switch that stopped an operator
        closing would be the opposite of an emergency control.
        """
        return RiskEngine(RiskLimits(), self.switches)

    async def load(self, db: AsyncSession) -> None:
        """Read the latched state back before evaluating anything.

        A process that died holding a daily-loss lock comes back holding it.
        Nothing here reconstructs a lock by recomputing a check: the lock is the
        record, and recomputing would be a different question.
        """
        rows = (await db.scalars(select(RiskRule).where(RiskRule.is_enabled))).all()
        accounts: set[str] = set()
        strategies: set[str] = set()
        global_stop = False
        global_reason = ""
        for row in rows:
            params = row.params or {}
            if row.rule_type == "kill_switch":
                if row.scope == "global":
                    global_stop = True
                    global_reason = str(params.get("reason", "kill switch"))
                elif row.scope in ("paper_account", "broker_account") and row.scope_ref:
                    accounts.add(row.scope_ref)
                elif row.scope == "strategy" and row.scope_ref:
                    strategies.add(row.scope_ref)
            elif row.rule_type == "mode_gate" and params.get("state"):
                key = self._key(row.scope, row.scope_ref)
                state = RiskState(str(params["state"]))
                locked_day = params.get("locked_day")
                self.states[key] = AccountRiskState(
                    scope=row.scope,
                    scope_ref=row.scope_ref,
                    state=state,
                    reason=str(params.get("reason", "")),
                    since=_parse(params.get("since")),
                    set_by=params.get("set_by"),
                    locked_day=_parse(locked_day),
                )
        self.switches = KillSwitches(
            global_stop=global_stop,
            global_reason=global_reason,
            accounts=frozenset(accounts),
            strategies=frozenset(strategies),
        )
        self.loaded = True
        log.info(
            "risk state recovered",
            extra={
                "event": "risk_state_recovered",
                "locks": len([s for s in self.states.values() if s.blocking]),
                "kill_switches": len(accounts) + len(strategies) + int(global_stop),
            },
        )

    @staticmethod
    def _key(scope: str, ref: str | None) -> str:
        return f"{scope}:{ref or '*'}"

    def state_for(self, account_id: str | None) -> AccountRiskState:
        key = self._key("paper_account", account_id)
        if key not in self.states:
            self.states[key] = AccountRiskState(scope="paper_account", scope_ref=account_id)
        return self.states[key]

    # ================================================== configuration

    async def configuration(
        self,
        db: AsyncSession,
        *,
        account_id: str | None = None,
        strategy_id: str | None = None,
        symbol: str | None = None,
        extra: RiskLimits | None = None,
    ) -> Resolved:
        """The effective limits for one order, most restrictive wins.

        `extra` is the caller's own limits -- a bot's frozen configuration --
        and it is combined like any other layer, so a bot can tighten the
        account's limits and can never loosen them.
        """
        rows = (
            await db.scalars(
                select(RiskRule)
                .where(RiskRule.is_enabled, RiskRule.rule_type == "limits")
                .order_by(RiskRule.priority)
            )
        ).all()
        applicable = [
            row
            for row in rows
            if row.scope == "global"
            or (row.scope in ("paper_account", "broker_account") and row.scope_ref == account_id)
            or (row.scope == "strategy" and row.scope_ref == strategy_id)
            or (row.scope == "symbol" and row.scope_ref == symbol)
        ]
        layers = [
            Layer(
                scope=Scope(row.scope),
                scope_ref=row.scope_ref,
                params=dict(row.params or {}),
                version=row.priority or 1,
                name=row.name,
            )
            for row in applicable
        ]
        if extra is not None:
            layers.append(
                Layer(
                    scope=Scope.strategy,
                    scope_ref=strategy_id or "caller",
                    params={
                        f.name: getattr(extra, f.name)
                        for f in fields(extra)
                        if getattr(extra, f.name) is not None
                    },
                    version=1,
                    name="caller",
                )
            )
        return resolve(layers)

    async def set_limits(
        self,
        db: AsyncSession,
        *,
        scope: Scope,
        scope_ref: str | None,
        limits: dict[str, Any],
        name: str,
        actor: str,
    ) -> RiskRule:
        """Store one layer. Validated before it is written, never after."""
        from app.risk.config import to_json

        check_valid(resolve([Layer(scope=scope, scope_ref=scope_ref, params=limits)]).limits)
        limits = to_json(limits)
        row = await db.scalar(
            select(RiskRule).where(
                RiskRule.scope == str(scope),
                RiskRule.scope_ref == scope_ref,
                RiskRule.rule_type == "limits",
            )
        )
        if row is None:
            row = RiskRule(
                name=name,
                scope=str(scope),
                scope_ref=scope_ref,
                rule_type="limits",
                params=limits,
                priority=1,
            )
            db.add(row)
        else:
            # A new version, never an edit of the old one: historical decisions
            # reference the version they were made under and must keep meaning
            # what they meant.
            row.params = limits
            row.priority = (row.priority or 1) + 1
            row.name = name
        row.is_enabled = True
        await db.flush()
        log.info(
            "risk configuration changed",
            extra={
                "event": "risk_config_changed",
                "scope": str(scope),
                "scope_ref": scope_ref,
                "version": row.priority,
                "actor": actor,
            },
        )
        return row

    # ================================================== kill switches

    async def engage_kill_switch(
        self, db: AsyncSession, *, scope: Scope, scope_ref: str | None, reason: str, actor: str
    ) -> dict[str, object]:
        row = RiskRule(
            name=f"kill switch {scope}:{scope_ref or '*'}",
            scope=str(scope),
            scope_ref=scope_ref,
            rule_type="kill_switch",
            params={"reason": reason, "engaged_by": actor, "at": _now().isoformat()},
            is_enabled=True,
            priority=0,
        )
        db.add(row)
        await db.flush()
        await self.load(db)
        log.warning(
            "kill switch engaged",
            extra={
                "event": "risk_kill_switch",
                "scope": str(scope),
                "scope_ref": scope_ref,
                "actor": actor,
            },
        )
        return {
            "engaged": True,
            "scope": str(scope),
            "scope_ref": scope_ref,
            "reason": reason,
            "positions": "preserved. A kill switch stops new orders; it never closes a position.",
        }

    async def release_kill_switch(
        self, db: AsyncSession, *, scope: Scope, scope_ref: str | None, actor: str
    ) -> dict[str, object]:
        rows = (
            await db.scalars(
                select(RiskRule).where(
                    RiskRule.rule_type == "kill_switch",
                    RiskRule.scope == str(scope),
                    RiskRule.scope_ref == scope_ref,
                    RiskRule.is_enabled,
                )
            )
        ).all()
        for row in rows:
            row.is_enabled = False
            row.params = {
                **(row.params or {}),
                "released_by": actor,
                "released_at": _now().isoformat(),
            }
        await db.flush()
        await self.load(db)
        return {"released": len(rows), "scope": str(scope), "scope_ref": scope_ref}

    # ================================================== locks

    async def lock(
        self,
        db: AsyncSession,
        *,
        account_id: str | None,
        state: RiskState,
        reason: str,
        at: datetime,
        set_by: str = "risk_engine",
    ) -> AccountRiskState:
        current = self.state_for(account_id)
        day = day_start(at, boundary_hour=self.day_boundary_hour)
        current.lock(state, reason, at, set_by=set_by, day=day)
        await self._persist_state(db, current)
        log.warning(
            "risk state locked",
            extra={
                "event": "risk_locked",
                "account_id": account_id,
                "state": str(state),
                "reason": reason[:200],
            },
        )
        return current

    async def clear_lock(
        self, db: AsyncSession, *, account_id: str | None, authorised_by: str
    ) -> AccountRiskState:
        """An authorised release. Refuses if the caller has no name."""
        current = self.state_for(account_id)
        current.clear(_now(), authorised_by=authorised_by, force=True)
        await self._persist_state(db, current)
        return current

    async def _persist_state(self, db: AsyncSession, state: AccountRiskState) -> None:
        row = await db.scalar(
            select(RiskRule).where(
                RiskRule.scope == state.scope,
                RiskRule.scope_ref == state.scope_ref,
                RiskRule.rule_type == "mode_gate",
            )
        )
        params = {
            "state": str(state.state),
            "reason": state.reason,
            "since": state.since.isoformat() if state.since else None,
            "set_by": state.set_by,
            "locked_day": state.locked_day.isoformat() if state.locked_day else None,
        }
        if row is None:
            db.add(
                RiskRule(
                    name=f"risk state {state.scope}:{state.scope_ref or '*'}",
                    scope=state.scope,
                    scope_ref=state.scope_ref,
                    rule_type="mode_gate",
                    params=params,
                    is_enabled=True,
                    priority=0,
                )
            )
        else:
            row.params = params
            row.is_enabled = True
        await db.flush()

    # ================================================== reservations

    def _live_reservations(self, account_id: str | None, now: datetime) -> list[Reservation]:
        if account_id is None:
            return []
        held = [r for r in self._reservations[account_id] if r.live(now)]
        self._reservations[account_id] = held
        return held

    def release(self, decision_id: str, account_id: str | None) -> bool:
        """Free a reservation when the order fills, is cancelled, or is dropped."""
        if account_id is None:
            return False
        before = len(self._reservations[account_id])
        self._reservations[account_id] = [
            r for r in self._reservations[account_id] if r.decision_id != decision_id
        ]
        return len(self._reservations[account_id]) < before

    def _with_reservations(
        self, portfolio: PortfolioState, held: list[Reservation]
    ) -> PortfolioState:
        """Fold outstanding approvals into the state the checks see.

        This is the whole race guard: the second of two concurrent orders is
        evaluated against a book that already contains the first.
        """
        if not held:
            return portfolio
        extra_positions = sum(r.positions for r in held)
        extra_exposure = sum((r.exposure for r in held), Decimal(0))
        exposure = dict(portfolio.exposure_by_currency)
        for currency in exposure:
            exposure[currency] = exposure[currency] + extra_exposure
        if not exposure and extra_exposure:
            exposure = {"__reserved__": extra_exposure}
        return PortfolioState(
            **{
                **{f.name: getattr(portfolio, f.name) for f in fields(portfolio)},
                "open_positions": (
                    None
                    if portfolio.open_positions is None
                    else portfolio.open_positions + extra_positions
                ),
                "exposure_by_currency": exposure,
            }
        )

    # ================================================== evaluation

    async def check(self, db: AsyncSession, request: RiskRequest) -> RiskDecision:
        """A preview. Evaluates and reserves nothing, writes nothing.

        `POST /v1/risk/check` is this, and it is side-effect free by
        construction rather than by promise: it never touches the reservation
        table, never latches a lock and never writes an event.
        """
        return await self._evaluate(db, request, persist=False, reserve=False)

    async def evaluate(self, db: AsyncSession, request: RiskRequest) -> RiskDecision:
        """The real thing: reserves on approval and records the decision."""
        return await self._evaluate(db, request, persist=True, reserve=True)

    async def _evaluate(
        self, db: AsyncSession, request: RiskRequest, *, persist: bool, reserve: bool
    ) -> RiskDecision:
        account_id = request.proposal.account_id
        lock = self._locks[account_id or "__global__"]
        async with lock:
            try:
                return await self._decide(db, request, persist=persist, reserve=reserve)
            except Exception as exc:  # noqa: BLE001 - fail closed, always
                log.error(
                    "risk engine failed; refusing the order",
                    extra={
                        "event": "risk_engine_error",
                        "error": type(exc).__name__,
                        "account_id": account_id,
                        "symbol": request.proposal.symbol,
                    },
                )
                return self._error_decision(request, f"{type(exc).__name__}: {exc}")

    def _error_decision(self, request: RiskRequest, detail: str) -> RiskDecision:
        """A failure is a refusal. There is no fail-open path in this module."""
        now = _now()
        return RiskDecision(
            outcome=RiskOutcome.error,
            at=now,
            request_hash=request_hash(request.bound_fields()),
            checks=(
                CheckRecord(
                    "risk_engine",
                    False,
                    detail[:300],
                    code=RejectionCode.risk_engine_error,
                ),
            ),
            account_id=request.proposal.account_id,
            strategy_id=request.proposal.strategy_id,
            symbol=request.proposal.symbol,
            side=request.proposal.side,
            execution_mode=request.execution_mode,
            requested_quantity=request.proposal.volume,
            reason=f"the risk engine could not reach a decision: {detail}"[:500],
            codes=(RejectionCode.risk_engine_error,),
        )

    async def _decide(
        self, db: AsyncSession, request: RiskRequest, *, persist: bool, reserve: bool
    ) -> RiskDecision:
        now = _now()
        proposal = request.proposal
        account_id = proposal.account_id
        digest = request_hash(request.bound_fields())

        if not self.loaded:
            # Never evaluate against state that has not been read back. A
            # process that skipped recovery would silently ignore every lock.
            await self.load(db)

        config = await self.configuration(
            db,
            account_id=account_id,
            strategy_id=proposal.strategy_id,
            symbol=proposal.symbol,
        )
        limits = self._with_signal_age(config.limits, request)

        # A latched lock is checked before anything else and cannot be argued
        # with by a passing check.
        state = self.state_for(account_id)
        state.refresh(now, boundary_hour=self.day_boundary_hour)
        if state.blocking:
            code = state.code or RejectionCode.account_disabled
            return self._decision(
                request,
                RiskOutcome.halted,
                now,
                digest,
                (CheckRecord(f"risk_state.{state.state}", False, state.reason, code=code),),
                config,
                f"{state.state}: {state.reason}",
                (code,),
            )

        engine = RiskEngine(limits, self.switches)
        held = self._live_reservations(account_id, now)
        portfolio = self._with_reservations(request.portfolio, held)

        verdict = engine.evaluate(
            proposal, portfolio, now=now, recent_signal_ids=request.recent_signal_ids
        )
        records = tuple(
            CheckRecord(
                name=str(c.limit),
                passed=c.passed,
                detail=c.detail,
                enforced=c.enforced,
                code=None if c.passed else CODE_FOR.get(c.limit, RejectionCode.risk_engine_error),
            )
            for c in verdict.checks
        )
        failed = [c for c in records if not c.passed]
        warnings = [f"{WarningCode.limit_not_enforced}:{c.name}" for c in records if not c.enforced]
        if held:
            warnings.append(f"{len(held)} outstanding approval(s) counted into this evaluation")

        if not failed:
            decision = self._decision(
                request,
                RiskOutcome.approved,
                now,
                digest,
                records,
                config,
                "all checks passed",
                (),
                warnings=tuple(warnings),
                approved_quantity=proposal.volume,
                expires=True,
            )
            if reserve and account_id and proposal.volume is not None:
                self._reservations[account_id].append(
                    Reservation(
                        decision_id=decision.decision_id,
                        account_id=account_id,
                        exposure=_exposure_of(proposal),
                        risk_amount=proposal.risk_amount or Decimal(0),
                        positions=1,
                        at=now,
                        expires_at=decision.expires_at or expiry(now),
                    )
                )
            if persist:
                await self._record(db, decision)
            return decision

        codes = tuple(dict.fromkeys(c.code for c in failed if c.code))
        latched = [
            LATCHING[c.limit] for c in verdict.checks if not c.passed and c.limit in LATCHING
        ]
        outcome = (
            RiskOutcome.halted
            if latched or verdict.decision is EngineDecision.halt
            else (RiskOutcome.rejected)
        )
        decision = self._decision(
            request,
            outcome,
            now,
            digest,
            records,
            config,
            verdict.reason,
            codes,
            warnings=tuple(warnings),
        )
        if latched and persist:
            # The breach becomes a state, so the next order is refused by the
            # lock rather than by re-running a check against a moved snapshot.
            await self.lock(
                db,
                account_id=account_id,
                state=latched[0],
                reason=verdict.reason[:400],
                at=now,
            )
        if persist:
            await self._record(db, decision)
        return decision

    def _with_signal_age(self, limits: RiskLimits, request: RiskRequest) -> RiskLimits:
        """Derive the freshness ceiling from the timeframe when none is set.

        A signal is at most one bar old by construction, so the fixed 300s
        ceiling this project shipped vetoed every H1 strategy -- which it did,
        live, at L16. Two bars of slack covers processing.
        """
        if limits.max_signal_age_seconds is not None:
            return limits
        if request.proposal.signal_time is None:
            # A manual order is not a signal and has no age. Deriving a limit
            # here would veto every hand-placed order for having no timestamp,
            # which is a different thing from having a stale one. A caller who
            # states a limit explicitly still gets the "no timestamp" veto,
            # because then they have asked for freshness.
            return limits
        seconds = (
            float(request.timeframe_seconds * SIGNAL_AGE_BARS)
            if request.timeframe_seconds
            else DEFAULT_SIGNAL_AGE_SECONDS
        )
        return RiskLimits(
            **{
                **{f.name: getattr(limits, f.name) for f in fields(limits)},
                "max_signal_age_seconds": seconds,
            }
        )

    def _decision(
        self,
        request: RiskRequest,
        outcome: RiskOutcome,
        now: datetime,
        digest: str,
        records: tuple[CheckRecord, ...],
        config: Resolved,
        reason: str,
        codes: tuple[RejectionCode, ...],
        *,
        warnings: tuple[str, ...] = (),
        approved_quantity: Decimal | None = None,
        expires: bool = False,
    ) -> RiskDecision:
        decision = RiskDecision(
            outcome=outcome,
            at=now,
            request_hash=digest,
            checks=records,
            account_id=request.proposal.account_id,
            strategy_id=request.proposal.strategy_id,
            symbol=request.proposal.symbol,
            side=request.proposal.side,
            execution_mode=request.execution_mode,
            requested_quantity=request.proposal.volume,
            approved_quantity=approved_quantity,
            reason=reason[:500],
            codes=codes,
            warnings=warnings,
            configuration_version=config.version,
            expires_at=expiry(now, APPROVAL_TTL_SECONDS) if expires else None,
        )
        self._recent.appendleft(decision)
        return decision

    async def _record(self, db: AsyncSession, decision: RiskDecision) -> None:
        """Persist the decision. A failure to record is a failure to approve.

        Raised rather than swallowed, so `_evaluate` turns it into a refusal:
        an approval nobody can audit is not an approval this platform makes.
        """
        db.add(
            RiskEvent(
                decision=_engine_decision(decision),
                reason=decision.reason[:2000],
                occurred_at=decision.at.replace(tzinfo=None),
                snapshot=decision.as_dict(),
                decision_id=decision.decision_id,
                paper_account_id=decision.account_id,
                mode=decision.execution_mode[:8],
                request_hash=decision.request_hash,
                configuration_version=decision.configuration_version,
            )
        )
        await db.flush()

    # ================================================== gate for a runner

    async def precheck(
        self, db: AsyncSession, *, account_id: str | None
    ) -> AccountRiskState | None:
        """The latched lock, if one is blocking. None means "carry on".

        Called by a runner before it does any work, so a locked account costs
        one state read rather than a full evaluation it was always going to
        refuse. It is not a substitute for the evaluation: the lock is checked
        again inside `_decide`, because a runner that forgot to call this must
        not thereby get an approval.
        """
        if not self.loaded:
            await self.load(db)
        state = self.state_for(account_id)
        state.refresh(_now(), boundary_hour=self.day_boundary_hour)
        return state if state.blocking else None

    async def limits_for(
        self,
        db: AsyncSession,
        *,
        account_id: str | None,
        strategy_id: str | None,
        symbol: str | None,
        caller: RiskLimits | None = None,
    ) -> Resolved:
        """The effective limits a runner must evaluate under.

        A runner never uses its own configuration directly. It passes it as a
        layer and gets back the combination, so a bot cannot loosen the
        account's limits by holding a copy of its own.
        """
        if not self.loaded:
            await self.load(db)
        return await self.configuration(
            db,
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            extra=caller,
        )

    async def record_verdict(
        self,
        db: AsyncSession,
        verdict: object,
        *,
        account_id: str | None,
        execution_mode: str,
        configuration_version: int,
        order_type: str = "market",
    ) -> RiskDecision:
        """Persist a verdict a runner obtained from the pure engine.

        The engine mints the `Approval` the OMS requires; this turns the same
        verdict into the durable, coded, versioned record. Both describe one
        decision -- there is no second evaluation and no chance of the two
        disagreeing.
        """
        proposal = verdict.proposal  # type: ignore[attr-defined]
        request = RiskRequest(
            proposal=proposal,
            execution_mode=execution_mode,
            order_type=order_type,
        )
        records = tuple(
            CheckRecord(
                name=str(c.limit),
                passed=c.passed,
                detail=c.detail,
                enforced=c.enforced,
                code=None if c.passed else CODE_FOR.get(c.limit, RejectionCode.risk_engine_error),
            )
            for c in verdict.checks  # type: ignore[attr-defined]
        )
        failed = [c for c in records if not c.passed]
        latched = [
            LATCHING[c.limit]
            for c in verdict.checks  # type: ignore[attr-defined]
            if not c.passed and c.limit in LATCHING
        ]
        if not failed:
            outcome = RiskOutcome.approved
        elif latched or verdict.decision is EngineDecision.halt:  # type: ignore[attr-defined]
            outcome = RiskOutcome.halted
        else:
            outcome = RiskOutcome.rejected
        decision = RiskDecision(
            outcome=outcome,
            at=verdict.at,  # type: ignore[attr-defined]
            request_hash=request_hash(request.bound_fields()),
            checks=records,
            account_id=account_id,
            strategy_id=proposal.strategy_id,
            symbol=proposal.symbol,
            side=proposal.side,
            execution_mode=execution_mode,
            requested_quantity=proposal.volume,
            approved_quantity=proposal.volume if not failed else None,
            reason=str(verdict.reason)[:500],  # type: ignore[attr-defined]
            codes=tuple(dict.fromkeys(c.code for c in failed if c.code)),
            warnings=tuple(
                f"{WarningCode.limit_not_enforced}:{c.name}" for c in records if not c.enforced
            ),
            configuration_version=configuration_version,
            expires_at=expiry(verdict.at) if not failed else None,  # type: ignore[attr-defined]
        )
        self._recent.appendleft(decision)
        if latched:
            await self.lock(
                db,
                account_id=account_id,
                state=latched[0],
                reason=decision.reason[:400],
                at=decision.at,
            )
        await self._record(db, decision)
        return decision

    # ================================================== reporting

    def recent(self, limit: int = 50) -> list[dict[str, object]]:
        return [d.as_dict() for d in list(self._recent)[:limit]]

    def status(self) -> dict[str, object]:
        return {
            "loaded": self.loaded,
            "trading_mode": self.trading_mode,
            "live_trading": self.live_trading,
            "kill_switches": {
                "global": self.switches.global_stop,
                "global_reason": self.switches.global_reason,
                "accounts": sorted(self.switches.accounts),
                "strategies": sorted(self.switches.strategies),
            },
            "states": [s.as_dict() for s in self.states.values() if s.blocking],
            "outstanding_approvals": {
                account: len([r for r in held if r.live(_now())])
                for account, held in self._reservations.items()
                if held
            },
            "day_boundary_hour_utc": self.day_boundary_hour,
            "failure_mode": (
                "closed. A check that raises, a configuration that will not load and a "
                "decision that cannot be persisted all produce a refusal."
            ),
            "concurrency": (
                "One asyncio lock per account serialises evaluation and reservation "
                "within this process, which is where the bots run. It is not a "
                "distributed lock; a second backend process would need row-level "
                "locking, and this deployment does not have one."
            ),
        }


def _exposure_of(proposal: OrderProposal) -> Decimal:
    if proposal.volume is None or proposal.entry_price is None:
        return Decimal(0)
    return abs(proposal.volume * proposal.entry_price)


def _engine_decision(decision: RiskDecision) -> str:
    """Map onto the `risk_events.decision` CHECK, which has approve/veto/halt."""
    if decision.approved:
        return "approve"
    return "halt" if decision.outcome is RiskOutcome.halted else "veto"


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
