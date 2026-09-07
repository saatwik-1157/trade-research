"""The automated execution orchestrator: an external signal to an order.

This is the wiring for signals that arrive from OUTSIDE — a TradingView alert
recorded by the L09 gateway, or a manual instruction. It is deliberately not a
second `PaperEngine`:

  * `app/paper/engine.py` (L16) drives BARS from `app.marketdata`, and runs a
    `Strategy` that produces the signal itself.
  * `app/execution/pipeline.py` (L20) drives a `Signal` row that already
    exists, and runs no strategy at all -- the decision was made elsewhere.

They share every gate. Neither computes a risk limit, a quantity or a fill of
its own; both call `app.risk`, `app.sizing` and an OMS, and both report in the
`Outcome` vocabulary so their counters can be added together.

    Signal
      -> validate the signal          (untrusted external input)
      -> validate the strategy        (exists, enabled, not paused)
      -> AI seat                      (advisory; may only decline)
      -> RISK                         (the only producer of an Approval)
      -> SIZING                       (the only producer of a quantity)
      -> RISK binds the sized order   (request_hash, checked by the OMS)
      -> OMS                          (the only path to a venue)
      -> BrokerAdapter

**Nothing here talks to a venue.** The OMS does. This module holds no adapter
and imports none, which a test asserts by parsing every module in the package.

**Every stage fails closed.** A stage that raises produces a refusal carrying
its reason, never a pass. The AI seat is the one component allowed to be
absent — its absence means "no opinion", and no opinion is not approval,
because approval is the Risk Engine's and nothing else's.

**One correlation id per attempt.** `execution_id` is minted once when a
signal is picked up and travels through the risk decision, the sizing result,
the order's intent, the logs and the events, so "why did this trade happen"
has one string to follow.

**The browser is not in this path.** The orchestrator is driven by
`ExecutionWorker`, which is a `app.workers.Worker` — the same supervised loop
every other background job uses. Closing a browser stops nothing.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from app.ai.decision import SignalContext
from app.execution.ai import AiFilter, AiVerdict
from app.execution.limits import LimitsLoader
from app.execution.outcome import NO_ORDER, Outcome
from app.execution.portfolio import PortfolioSnapshot, to_portfolio_state
from app.execution.store import OrderStore
from app.oms.order import ManagedOrder
from app.oms.registry import NoOrderManager, OrderManagerRegistry
from app.oms.service import OrderRefused, ReconciliationRequired
from app.oms.state import OrderStatus
from app.risk.engine import OrderProposal, PortfolioState, RiskEngine, RiskVerdict
from app.sizing.calculator import SizingMethod, SizingRequest, SizingResult
from app.sizing.calculator import calculate as size_order
from app.symbols.service import ContractSpec

log = logging.getLogger("app.execution")

#: How old a signal may be before it is refused. An alert that took four
#: minutes to arrive describes a market that has moved; acting on it is acting
#: on a price nobody quoted. Measured against the signal's OWN timestamp, not
#: against when we happened to read it.
DEFAULT_MAX_SIGNAL_AGE_SECONDS = 120.0


def _ai_context(signal: IncomingSignal) -> SignalContext:
    """One externally-arriving signal as the context the AI layer may see.

    §17. `bars` is empty and deliberately so: nothing in this module reads
    market data, and building a window here would be the one place a
    look-ahead could enter the orchestrator.
    """
    return SignalContext(
        strategy_key=signal.strategy_id or signal.source,
        symbol=signal.symbol,
        timeframe=str(signal.advisory.get("timeframe") or ""),
        bar_time=signal.signal_time,
        side=signal.side,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        metadata={"source": signal.source, "signal_key": signal.signal_key},
    )


class PipelineError(Exception):
    """A pipeline that cannot run at all. Never resolved to a default."""


@dataclass(frozen=True)
class IncomingSignal:
    """One externally-arriving signal, normalized.

    This is the orchestrator's input and is deliberately narrow: a side, an
    instrument, a time, and whatever levels the source supplied. It carries no
    quantity, because a quantity from outside is a request rather than a
    decision -- `app.sizing` produces the number that gets traded.
    """

    signal_id: str
    signal_key: str
    source: str  # "tradingview" | "strategy" | "manual"
    symbol: str
    side: str  # "buy" | "sell"
    signal_time: datetime
    account_id: str
    mode: str
    strategy_id: str | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    auth_strength: str = "weak"
    #: What the source suggested. Recorded, never obeyed.
    advisory: dict[str, object] = field(default_factory=dict)

    # ------------------------------------------------------------- L45 F-1
    #: The bot this signal was routed to, and the per-trade risk budget that
    #: bot configures. Both come from the `bots` row, never from the alert:
    #: a payload that could set its own risk budget could set any number.
    #:
    #: `risk_amount` None means NOT SET -- the pipeline's own configuration
    #: applies -- and is deliberately not "no budget". Still a REQUEST: the
    #: RiskEngine evaluates the resulting order and can veto it, and
    #: `BotLimits.effective` can only ever make a bot's figure tighter.
    bot_id: str | None = None
    risk_amount: Decimal | None = None


@dataclass(frozen=True)
class StrategyState:
    """What the platform knows about the strategy a signal named."""

    exists: bool
    enabled: bool
    paused: bool = False
    reason: str = ""


@dataclass
class ExecutionResult:
    """One attempt, start to finish. Every stage that ran is on it."""

    execution_id: str
    signal_key: str
    outcome: Outcome
    at: datetime
    detail: str = ""
    ai: AiVerdict | None = None
    risk: RiskVerdict | None = None
    sizing: SizingResult | None = None
    order: ManagedOrder | None = None

    @property
    def created_order(self) -> bool:
        return self.outcome not in NO_ORDER

    def as_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "signal_key": self.signal_key,
            "outcome": str(self.outcome),
            "at": self.at.isoformat(),
            "detail": self.detail,
            "created_order": self.created_order,
            "ai": self.ai.as_dict() if self.ai else None,
            # L44. The risk verdict was captured on this object and DROPPED
            # here, so every serialised decision -- the recent-executions view,
            # the shadow decision record, anything an operator reads after the
            # fact -- showed what the AI thought and what was sized, and not
            # why the RiskEngine approved or vetoed.
            #
            # That is the one verdict a trading platform must never lose: it is
            # the mandatory gate, and "risk approved this" with no record of
            # which checks ran is indistinguishable from "risk was not asked".
            # Rendered inline rather than by calling a method on RiskVerdict,
            # because the execution package should not dictate the risk
            # package's serialisation.
            "risk": (
                {
                    "decision": str(self.risk.decision),
                    "approved": self.risk.approved,
                    "reason": self.risk.reason,
                    "at": self.risk.at.isoformat(),
                    "failed_checks": [
                        {"limit": str(c.limit), "detail": c.detail} for c in self.risk.failed
                    ],
                    "not_enforced": list(self.risk.not_enforced),
                }
                if self.risk is not None
                else None
            ),
            "sizing": self.sizing.as_dict() if self.sizing else None,
            "order": self.order.as_dict() if self.order is not None else None,
        }


class SafeModeLatch(Protocol):
    """What the pipeline needs from L38's latch, and nothing more.

    A Protocol rather than importing `app.recovery.safe_mode.SafeMode`: the
    execution package must not depend on the recovery package, and typing it as
    `object` (as it was until L43) meant mypy could not check the two attribute
    accesses below -- which are the only two the pipeline makes.

    Note what is absent. There is no `release`, no `engage` and no setter of any
    kind, so the pipeline can ASK whether safe mode is engaged and cannot change
    it. Rule 4 of the safety model -- recovery cannot bypass the OMS, and
    execution cannot release safe mode -- is a property of this Protocol.
    """

    @property
    def engaged(self) -> bool: ...

    @property
    def reasons(self) -> Sequence[Any]: ...


class ExecutionPipeline:
    """Signal -> order. Holds no adapter and no venue of its own."""

    def __init__(
        self,
        *,
        managers: OrderManagerRegistry,
        risk: RiskEngine,
        spec_for: object,
        strategy_state: object,
        sizing_method: SizingMethod = SizingMethod.fixed_risk,
        risk_amount: Decimal | None = None,
        risk_percent: Decimal | None = None,
        quantity: Decimal | None = None,
        equity: Decimal | None = None,
        ai: AiFilter | None = None,
        max_signal_age_seconds: float = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
        # L38's safe-mode latch, or None. Optional so a pipeline constructed by
        # a test or by a level that predates L38 behaves exactly as it did. It
        # can only REFUSE: rule 2 says recovery cannot bypass the RiskEngine,
        # so this is an additional gate in front of the existing ones and never
        # a replacement for one.
        safe_mode: SafeModeLatch | None = None,
        # L45 C-1's fix. The durable half of this pipeline: it answers "has
        # this intent already produced an order?" from the `orders` table
        # rather than from a dict a restart empties, and it writes each order
        # down before anything is transmitted.
        #
        # Optional ONLY so a pipeline built by a test or by a level that
        # predates L45 behaves exactly as it did. A pipeline without one is
        # NOT durable across a restart, `status()` says so in as many words,
        # and a regression test asserts the DEPLOYED wiring supplies one --
        # because "the seat exists but nobody sat in it" is the exact shape of
        # the defect this fixes.
        store: OrderStore | None = None,
        # L53. What the RiskEngine is evaluated against.
        #
        # Without it the engine sees `PortfolioState(equity=None)` and every
        # portfolio-level limit is unenforceable -- not because the numbers are
        # wrong, but because the engine is never told them.
        # `one_position_per_symbol` is ON by default and could not fire.
        portfolio: PortfolioSnapshot | None = None,
        # L55. The account's CONFIGURED limits, resolved per pass.
        #
        # Without it this pipeline evaluates every signal against
        # `RiskLimits()` -- 17 of its 20 limits unset -- so an operator who
        # configures a daily loss cap, a position cap or an exposure cap gets
        # it enforced on the API order path and NOT on the automated one.
        #
        # `app/main.py` has described this as the intended design since L22:
        # "the engine's limits are replaced per pass by the worker's caller
        # once a bot configuration exists". The caller was never written.
        limits_for: LimitsLoader | None = None,
    ) -> None:
        self.safe_mode = safe_mode
        self.store = store
        self.portfolio = portfolio
        self.limits_for = limits_for
        self.managers = managers
        self.risk = risk
        # Callables rather than services, so the pipeline can be driven from a
        # worker, a request or a test without dragging a session in.
        self.spec_for = spec_for
        self.strategy_state = strategy_state
        self.sizing_method = sizing_method
        self.risk_amount = risk_amount
        self.risk_percent = risk_percent
        self.quantity = quantity
        self.equity = equity
        self.ai = ai
        self.max_signal_age_seconds = max_signal_age_seconds
        self.seen: set[str] = set()
        self.counts: dict[str, int] = {}

    # ================================================================ one pass

    async def process(
        self, signal: IncomingSignal, *, now: datetime | None = None
    ) -> ExecutionResult:
        """Run one signal through every gate. Never raises."""
        now = now or datetime.now(UTC)
        execution_id = str(uuid4())
        try:
            return await self._process(signal, now, execution_id)
        except Exception as exc:  # noqa: BLE001 - fail closed, always
            # A stage that raised is a refusal, not a pass. Every path out of
            # this method is a recorded decision, because a bot loop needs
            # something it can record rather than an exception to classify.
            log.exception(
                "the execution pipeline raised; the signal was refused",
                extra={
                    "event": "execution_pipeline_error",
                    "execution_id": execution_id,
                    "signal_key": signal.signal_key,
                },
            )
            return self._record(
                ExecutionResult(
                    execution_id=execution_id,
                    signal_key=signal.signal_key,
                    outcome=Outcome.strategy_error,
                    at=now,
                    detail=f"{type(exc).__name__}: {exc}"[:300],
                )
            )

    async def _process(
        self, signal: IncomingSignal, now: datetime, execution_id: str
    ) -> ExecutionResult:
        def refuse(outcome: Outcome, detail: str, **extra: object) -> ExecutionResult:
            return self._record(
                ExecutionResult(
                    execution_id=execution_id,
                    signal_key=signal.signal_key,
                    outcome=outcome,
                    at=now,
                    detail=detail[:300],
                    **extra,  # type: ignore[arg-type]
                )
            )

        # ------------------------------------------------ 0. safe mode (L38)
        #
        # Before everything, and it can only REFUSE. Rule 2 of L38: recovery
        # cannot bypass the RiskEngine -- so this is an additional gate in
        # front of the existing ones, never a replacement for one. The engine
        # still vetoes whatever it would have vetoed; safe mode simply means
        # nothing gets as far as asking it.
        if self.safe_mode is not None and self.safe_mode.engaged:
            reasons = "; ".join(
                f"{latch.reason}: {latch.detail}" for latch in self.safe_mode.reasons
            )
            return refuse(
                Outcome.safe_mode,
                f"the platform is in safe mode, so no new signal is executed. {reasons}",
            )

        # ---------------------------------------------- 1. the signal itself
        problem = self._validate(signal, now)
        if problem is not None:
            outcome, detail = problem
            return refuse(outcome, detail)

        # ------------------------------------------------- 2. idempotency
        #
        # In-process first, so a repeated alert never reaches the venue's
        # rate limit. The OMS's `intent_id` and the unique constraint behind
        # it are the real guarantee; this is the cheap one in front of them.
        if signal.signal_key in self.seen:
            return refuse(
                Outcome.duplicate_signal,
                f"signal {signal.signal_key} has already been processed",
            )

        # ----------------------------------------------- 3. the strategy
        #
        # **L56.** The resolver may be async: reading whether a strategy is
        # switched off means reading the database, and `strategies.is_active`
        # is where that lives. A sync resolver still works unchanged, which is
        # what every test and every level before this one passes.
        state = self.strategy_state(signal.strategy_id)  # type: ignore[operator]
        if inspect.isawaitable(state):
            state = await state
        if not state.exists:
            return refuse(
                Outcome.strategy_unknown,
                f"no strategy {signal.strategy_id!r} on this platform; refusing rather "
                "than executing a signal nobody can attribute",
            )
        if not state.enabled or state.paused:
            # A disabled strategy is a decision somebody made. Consuming the
            # signal is right: re-offering it every pass would mean the moment
            # it is re-enabled, a queue of stale signals fires at once.
            self.seen.add(signal.signal_key)
            return refuse(
                Outcome.strategy_disabled,
                state.reason or f"strategy {signal.strategy_id} is switched off",
            )

        # ------------------------------------------------- 4. the AI seat
        #
        # It runs BEFORE risk and can only decline. Its absence means "no
        # opinion", which is not approval -- approval is the Risk Engine's.
        # Handed a normalized `SignalContext` (L27 §17), like the paper engine.
        # It carries NO bars: an externally-arriving signal comes with no market
        # window, and this module does not fetch one -- a seat that could fetch
        # a bar could fetch tomorrow's. A configuration whose mode runs
        # inference will therefore refuse for want of features, which is the
        # honest outcome and is what §27 asks be recorded rather than hidden.
        ai_verdict: AiVerdict | None = None
        if self.ai is not None:
            ai_verdict = self.ai.score(_ai_context(signal))
            if not ai_verdict.accept:
                self.seen.add(signal.signal_key)
                return refuse(
                    Outcome.ai_rejected,
                    ai_verdict.reason or "the AI filter declined this signal",
                    ai=ai_verdict,
                )

        # ------------------------------------------------------ 5. the venue
        try:
            manager = self.managers.get(signal.account_id)
        except NoOrderManager as exc:
            return refuse(Outcome.no_venue, str(exc), ai=ai_verdict)
        if manager.mode != signal.mode:
            return refuse(
                Outcome.source_unauthorized,
                f"the signal is for {signal.mode} and the account's manager runs "
                f"{manager.mode}; refusing rather than executing in an environment the "
                "signal did not describe",
                ai=ai_verdict,
            )

        # ------------------------------------------------- 6. the instrument
        spec: ContractSpec | None = await self.spec_for(signal.symbol)  # type: ignore[operator]
        if spec is None:
            return refuse(
                Outcome.spec_incomplete,
                f"no usable contract spec for {signal.symbol}; a size computed from an "
                "absent tick value is an order for the wrong amount",
                ai=ai_verdict,
            )

        # ---------------------------------------------------------- 7. SIZING
        if signal.entry_price is None:
            return refuse(
                Outcome.signal_invalid,
                "no entry price on the signal and none can be invented; refusing to "
                "size against a guess",
                ai=ai_verdict,
            )
        sizing = size_order(
            SizingRequest(
                method=self.sizing_method,
                spec=spec,
                quantity=self.quantity,
                # The routed bot's budget wins over the pipeline's default when
                # it has one. L22's design, which `app/main.py` describes as
                # "replaced per pass by the worker's caller once a bot
                # configuration exists" -- and which nothing ever wired.
                risk_amount=signal.risk_amount or self.risk_amount,
                equity=self.equity,
                risk_percent=self.risk_percent,
                side=signal.side,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
        )
        if sizing.refused or sizing.volume is None:
            return refuse(
                Outcome.sizing_refused,
                sizing.gap or "sizing refused",
                ai=ai_verdict,
                sizing=sizing,
            )

        # ------------------------------------------------------------ 8. RISK
        proposal = OrderProposal(
            symbol=spec.internal_symbol,
            side=signal.side,
            mode=manager.mode,
            volume=sizing.volume,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            risk_amount=sizing.risk_actual,
            account_id=signal.account_id,
            strategy_id=signal.strategy_id,
            signal_id=signal.signal_id,
            signal_time=signal.signal_time,
        )
        # **L53.** The account's real portfolio, not just its equity. An
        # unreadable one leaves every field None, and a None a limit needs is a
        # veto -- so a portfolio this cannot read makes trading more
        # conservative, never less.
        approval, verdict = (await self._engine_for(signal, spec)).approve(
            proposal,
            await self._portfolio_state(signal.account_id, manager.mode),
            now=now,
            recent_signal_ids=tuple(self.seen),
        )
        if approval is None:
            # A vetoed signal is still consumed: re-proposing the identical
            # signal next pass would re-run the same veto forever.
            self.seen.add(signal.signal_key)
            outcome = Outcome.risk_vetoed
            if any(str(c.limit).startswith("kill_switch") for c in verdict.failed):
                outcome = Outcome.kill_switch
            elif str(verdict.decision) == "halt":
                outcome = Outcome.risk_halted
            return refuse(outcome, verdict.reason, ai=ai_verdict, sizing=sizing, risk=verdict)

        # ------------------------------------------------------------- 9. OMS
        async with self.managers.lock(signal.account_id):
            try:
                # The guard first, in its own arm: it refuses only because an
                # order for this intent already exists, which is a duplicate
                # rather than a rejection, and reporting it as a rejection
                # would put it in the wrong counter.
                manager.guard_resend(signal.signal_key)
                # Then the SAME question asked of the database. **L45 C-1.**
                # `guard_resend` consults `by_intent`, a dict every new
                # process starts empty, so it passed silently after any
                # restart -- and a restart is a deploy, a crash, an OOM kill
                # or a rolling replacement. The `orders` row is the only
                # answer to this question that outlives the process.
                await self._guard_resend_durably(signal.signal_key)
            except ReconciliationRequired as exc:
                return refuse(
                    Outcome.execution_unknown,
                    str(exc),
                    ai=ai_verdict,
                    sizing=sizing,
                    risk=verdict,
                )
            except OrderRefused as exc:
                self.seen.add(signal.signal_key)
                return refuse(
                    Outcome.duplicate_signal,
                    str(exc),
                    ai=ai_verdict,
                    sizing=sizing,
                    risk=verdict,
                )

            try:
                submission = manager.create(
                    approval,
                    intent_id=signal.signal_key,
                    account_id=signal.account_id,
                    sizing_snapshot={**sizing.as_dict(), "execution_id": execution_id},
                    at=now,
                )
            except ReconciliationRequired as exc:
                # The venue may be holding an order for this intent. Nothing is
                # sent, and the signal is NOT consumed: it becomes sendable
                # again only once reconciliation has settled the old one.
                return refuse(
                    Outcome.execution_unknown,
                    str(exc),
                    ai=ai_verdict,
                    sizing=sizing,
                    risk=verdict,
                )
            except OrderRefused as exc:
                self.seen.add(signal.signal_key)
                return refuse(
                    Outcome.execution_rejected,
                    str(exc),
                    ai=ai_verdict,
                    sizing=sizing,
                    risk=verdict,
                )

            if submission.duplicate:
                self.seen.add(signal.signal_key)
                return refuse(
                    Outcome.duplicate_signal,
                    f"intent {signal.signal_key} already has order {submission.order.id}",
                    ai=ai_verdict,
                    sizing=sizing,
                    risk=verdict,
                    order=submission.order,
                )
            order = submission.order

            # Section 18's ordering, which the API order route has always
            # honoured and this pipeline never did: **the row exists before
            # anything is transmitted.** That asymmetry between the manual
            # path and the automated one was C-1.
            try:
                await self._persist_order(order)
            except Exception as exc:  # noqa: BLE001 - fail closed, always
                # Nothing has been sent. The created order is removed rather
                # than left in `by_intent`, where it would make the next pass
                # refuse a real signal as a duplicate of an order that never
                # existed, and the signal is NOT consumed into `seen`.
                self._unwind(manager, order)
                log.error(
                    "an order could not be recorded and was not sent",
                    extra={
                        "event": "order_not_recorded",
                        "execution_id": execution_id,
                        "signal_key": signal.signal_key,
                        "order_id": order.id,
                    },
                )
                return refuse(
                    Outcome.not_recorded,
                    f"the order could not be recorded, so it was not sent: "
                    f"{type(exc).__name__}: {exc}"[:300],
                    ai=ai_verdict,
                    sizing=sizing,
                    risk=verdict,
                )

            self.seen.add(signal.signal_key)
            await manager.submit(order, at=now)
            # And again after the venue has spoken, so the recorded state is
            # what actually happened. A failure here is logged and swallowed:
            # the order is already at the venue, and raising would turn a
            # storage problem into a second, unrecorded send.
            try:
                await self._persist_order(order)
            except Exception:  # noqa: BLE001 - the order exists; never resend
                log.exception(
                    "an order was sent and its post-send state could not be "
                    "recorded; reconcile it against the venue -- do NOT resend",
                    extra={
                        "event": "order_state_not_recorded",
                        "execution_id": execution_id,
                        "order_id": order.id,
                        "status": str(order.status),
                    },
                )

        return self._record(
            ExecutionResult(
                execution_id=execution_id,
                signal_key=signal.signal_key,
                outcome=_outcome_for(order.status),
                at=now,
                detail=order.reject_reason or f"order {order.id} is {order.status}",
                ai=ai_verdict,
                risk=verdict,
                sizing=sizing,
                order=order,
            )
        )

    # ============================================================= validation

    def _validate(self, signal: IncomingSignal, now: datetime) -> tuple[Outcome, str] | None:
        """Everything checkable about the signal before anything else runs.

        External input is untrusted (§36), and "untrusted" means the checks
        happen here rather than being inferred from the fact that a row
        exists: the gateway wrote the row, and the gateway can be wrong.
        """
        if signal.side not in ("buy", "sell"):
            return (Outcome.signal_invalid, f"unknown side {signal.side!r}")
        if not signal.symbol:
            return (Outcome.signal_invalid, "no symbol on the signal")
        if not signal.signal_key:
            return (Outcome.signal_invalid, "no signal key; idempotency is not possible")
        if signal.mode == "live":
            # This platform does not execute live. The refusal is here as well
            # as in the Risk Engine because a defence that exists once is a
            # defence that can be removed once.
            return (
                Outcome.source_unauthorized,
                "live execution is not enabled on this platform",
            )
        if signal.signal_time.tzinfo is None:
            return (Outcome.signal_invalid, "the signal time carries no timezone")
        if signal.signal_time > now + timedelta(seconds=5):
            return (
                Outcome.signal_invalid,
                f"the signal is stamped {signal.signal_time}, in the future",
            )
        age = (now - signal.signal_time).total_seconds()
        if age > self.max_signal_age_seconds:
            return (
                Outcome.signal_stale,
                f"the signal is {age:.0f}s old against a {self.max_signal_age_seconds:.0f}s "
                "limit; a late alert describes a market that has moved",
            )
        if signal.source == "tradingview" and signal.auth_strength == "weak":
            # A weak-auth alert is recorded by the gateway and is not a basis
            # for an order. That is a deliberate difference between "we heard
            # it" and "we act on it".
            return (
                Outcome.source_unauthorized,
                "the alert was not authenticated strongly enough to execute on",
            )
        return None

    # ================================================================ counters

    def _record(self, result: ExecutionResult) -> ExecutionResult:
        key = str(result.outcome)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.counts["signals_received"] = self.counts.get("signals_received", 0) + 1
        if result.created_order:
            self.counts["orders_created"] = self.counts.get("orders_created", 0) + 1
        else:
            self.counts["signals_rejected"] = self.counts.get("signals_rejected", 0) + 1
        log.info(
            "execution pass",
            extra={
                "event": "execution_pass",
                "execution_id": result.execution_id,
                "signal_key": result.signal_key,
                "outcome": str(result.outcome),
                "created_order": result.created_order,
                "detail": result.detail[:200],
            },
        )
        return result

    async def _engine_for(self, signal: IncomingSignal, spec: Any) -> RiskEngine:
        """The engine this pass is evaluated by. **L55.**

        The account's configured limits, combined with the strategy's and the
        symbol's by `RiskService`, which takes the more restrictive at every
        field -- so this can only ever be TIGHTER than the pipeline's own
        engine, never looser.

        **The kill switches are carried across.** They live on the engine, and
        a per-pass engine built without them would be a per-pass engine that
        cannot be halted. That would turn a limits improvement into a safety
        regression, which is the shape of defect this session has spent its
        time removing.

        Falls back to the pipeline's own engine when no loader is wired, so a
        pipeline built by a test or by a level predating L55 behaves as it did.
        A loader that RAISES also falls back -- and the fallback is the
        configured engine, never a bare one.
        """
        if self.limits_for is None:
            return self.risk
        try:
            limits = await self.limits_for(
                account_id=signal.account_id,
                strategy_id=signal.strategy_id,
                symbol=getattr(spec, "internal_symbol", None) or signal.symbol,
            )
        except Exception:  # noqa: BLE001 - never turn a config read into a crash
            log.exception(
                "the account's risk limits could not be resolved; falling back to the "
                "pipeline's own engine, which is never looser than a bare one",
                extra={
                    "event": "risk_limits_unresolved",
                    "account_id": signal.account_id,
                    "signal_key": signal.signal_key,
                },
            )
            return self.risk
        if limits is None:
            return self.risk
        return RiskEngine(limits, self.risk.switches)

    async def _portfolio_state(self, account_id: str, mode: str) -> PortfolioState:
        """What risk is evaluated against. **L53.**

        Falls back to the pipeline's own configured equity when no snapshot
        provider is wired, which is exactly what this did before -- so a
        pipeline built by a test or by a level predating L53 behaves as it did.
        """
        if self.portfolio is None:
            return PortfolioState(equity=self.equity)
        raw = await self.portfolio.state_for(account_id, mode=mode)
        if not raw:
            return PortfolioState(equity=self.equity)
        return to_portfolio_state(raw)

    # ============================================================== durability

    @property
    def durable(self) -> bool:
        """Whether this pipeline's orders survive a restart. **L45 C-1.**"""
        return self.store is not None

    async def _guard_resend_durably(self, intent_id: str) -> None:
        """`guard_resend`, asked of the database instead of a dict.

        Raises the same two exceptions as the in-memory guard, so the caller's
        existing arms classify the refusal identically -- a duplicate stays a
        duplicate and an unresolved order stays `execution_unknown`.

        A pipeline with no store cannot answer this and does not pretend to.
        """
        if self.store is None:
            return
        prior = await self.store.prior_order(intent_id)
        if prior is None:
            return
        if prior.needs_reconciliation:
            raise ReconciliationRequired(
                f"intent {intent_id} has order {prior.id} recorded in "
                f"`{prior.status}`; the venue may be holding it. This process did "
                "not create it -- the record outlived the restart that emptied "
                "every in-memory guard. Reconcile before sending anything for this "
                "intent, because retrying an uncertain submission is how one signal "
                "becomes two positions"
            )
        if prior.resendable:
            # `failed` means the request provably never reached the venue, so
            # the OMS's in-memory guard permits a fresh order for the intent.
            # The RECORD does not, and the record is the stricter authority:
            # `orders.intent_id` is UNIQUE, so a second order for this intent
            # has nowhere to be written and the insert would fail after the
            # decision to send had already been taken.
            #
            # Refusing here rather than at the insert is the whole point. The
            # alternative is an IntegrityError raised between `create` and
            # `submit` -- a failure at the least recoverable moment, for a
            # reason the schema knew all along.
            raise OrderRefused(
                f"intent {intent_id} already produced order {prior.id}, recorded as "
                f"`{prior.status}`. That send provably did not reach the venue, so "
                "it would be safe to send again -- but one intent has one order row "
                "by construction (`orders.intent_id` is UNIQUE), so a retry needs a "
                "fresh intent rather than a second order for this one"
            )
        raise OrderRefused(
            f"intent {intent_id} already produced order {prior.id}, recorded as "
            f"`{prior.status}`; a new order for it would be a second order for "
            "one signal"
        )

    async def _persist_order(self, order: ManagedOrder) -> None:
        """Write the order down. A no-op without a store, and never silent
        about it -- `status()` reports `durable: false`."""
        if self.store is None:
            return
        await self.store.record(order)

    def _unwind(self, manager: object, order: ManagedOrder) -> None:
        """Forget an order that was created and could not be recorded.

        Sound only because nothing was transmitted; `OrderManager.discard`
        re-checks that itself and refuses otherwise. A failure to unwind is
        logged rather than raised: the pass is already being refused, and the
        worst case is a duplicate refusal on the next pass rather than a
        second send.
        """
        try:
            manager.discard(order)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.exception(
                "an unrecorded order could not be discarded from the OMS",
                extra={"event": "order_unwind_failed", "order_id": order.id},
            )

    def status(self) -> dict[str, object]:
        return {
            "counts": dict(self.counts),
            "seen_signals": len(self.seen),
            "durable": self.durable,
            "portfolio_aware": self.portfolio is not None,
            "limits_source": (
                "the account's configured limits, resolved per pass and combined "
                "with the strategy's and the symbol's, taking the more restrictive"
                if self.limits_for is not None
                else "the pipeline's OWN engine only. An operator's configured "
                "account limits are NOT applied on this path."
            ),
            "portfolio": (
                "The RiskEngine is evaluated against the account's real portfolio: "
                "open positions, exposure, realised P&L, drawdown."
                if self.portfolio is not None
                else "NOT PORTFOLIO AWARE. The RiskEngine sees only equity, so every "
                "portfolio-level limit is unenforceable -- including "
                "one_position_per_symbol, which is on by default."
            ),
            "durability": (
                "Orders are written to the `orders` table before they are sent, and "
                "an intent that already has a recorded order is refused. Both "
                "survive a restart."
                if self.durable
                else "NOT DURABLE. This pipeline has no order store, so its "
                "duplicate and unresolved-order guards are in-memory only and a "
                "restart empties them. The deployed pipeline has one."
            ),
            "max_signal_age_seconds": self.max_signal_age_seconds,
            "authority": (
                "This pipeline orchestrates. It computes no risk limit, no quantity and "
                "no fill: app.risk, app.sizing and the OMS do, and it holds no broker "
                "adapter at all."
            ),
            "ai": (
                "advisory and optional. Absent means no opinion, and no opinion is not approval."
            ),
            "retry_policy": (
                "A signal whose order is unresolved is NOT consumed and NOT resent: the "
                "OMS refuses a second order for that intent until reconciliation settles "
                "the first."
            ),
        }


def _outcome_for(status: OrderStatus) -> Outcome:
    """The pass's outcome, read from what the venue actually did."""
    return {
        OrderStatus.filled: Outcome.filled,
        OrderStatus.partially_filled: Outcome.partially_filled,
        OrderStatus.rejected: Outcome.execution_rejected,
        OrderStatus.failed: Outcome.execution_rejected,
        OrderStatus.unknown: Outcome.execution_unknown,
        OrderStatus.cancelled: Outcome.execution_rejected,
        OrderStatus.expired: Outcome.execution_rejected,
    }.get(status, Outcome.order_submitted)
