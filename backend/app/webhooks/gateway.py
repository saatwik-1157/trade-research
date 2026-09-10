"""The TradingView webhook gateway: validate, authenticate, deduplicate, record.

The pipeline, in order, and the order matters:

    body cap -> parse -> authenticate -> validate -> age check
             -> idempotency -> symbol -> strategy -> Signal -> event

Authentication runs before validation so a caller without the secret learns
nothing about the schema. Idempotency runs before the symbol lookup so a
replayed alert costs one indexed read rather than a resolution pass. The
Signal is written and committed **before** the event is published, so an event
never refers to a row that does not exist -- and a publish that fails leaves a
durable signal rather than losing it.

**This gateway cannot trade**, and the reason is worth stating precisely
because it has changed. It writes a `Signal` with status `new` and publishes
`SIGNAL_CREATED`.

It used to be true that nothing downstream existed. That is no longer the
case: the strategy engine, `app.risk`, `app.sizing` and `app.oms` are all
built, and `app.execution.pipeline` runs a signal through every one of them
in order. What still holds is that **none of it is reachable from here**.
This module imports no risk engine, no sizing calculator, no order manager
and no broker adapter; the only `app.execution` import is `routing`, which
decides which account a signal belongs to and cannot send anything anywhere.

The consumer is `ExecutionWorker`, which claims `new` signals from the table
on its own supervised loop. So a row written here reaches a venue only by
being picked up in a separate process, through the pipeline, past the
RiskEngine's veto. The gap between "recorded" and "traded" is a poll, not
a call.

The response says what the *webhook layer* did -- accepted, duplicate,
rejected, unauthorized -- and never claims a fill, a position or a trade. A
200 here means "recorded", nothing more.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event
from app.execution.routing import Route, route_for
from app.models.signals import Signal, WebhookEvent
from app.models.strategies import Strategy, StrategyVersion
from app.realtime.catalogue import EventType, Scope
from app.realtime.channels import Channel
from app.symbols import service as symbols
from app.symbols.errors import SymbolError
from app.webhooks.schema import Alert, PayloadError, check_age, parse_alert, redact

log = logging.getLogger("app.webhooks")

# TradingView's published webhook source addresses. Only meaningful when the
# port is exposed directly: behind a tunnel the source is the tunnel's, which
# is why this is off by default rather than a silent lie about who called.
TRADINGVIEW_IPS = frozenset({"52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"})


class Outcome(StrEnum):
    accepted = "accepted"
    duplicate = "duplicate"
    rejected = "rejected"
    unauthorized = "unauthorized"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    detail: str
    signal_id: str | None = None
    webhook_event_id: str | None = None
    # What the webhook layer did. Deliberately not a trading status: a 200
    # here means recorded, never filled.
    http_status: int = 200

    def as_dict(self) -> dict[str, object]:
        return {
            "status": str(self.outcome),
            "detail": self.detail,
            "signal_id": self.signal_id,
            "webhook_event_id": self.webhook_event_id,
            "note": "a recorded signal, not a trade; nothing was executed",
        }


class Unauthorized(Exception):
    """The secret did not match. The reason is never elaborated to the caller."""


def authenticate(payload: dict, raw_body: str, secret: str) -> str:
    """Constant-time check of the shared secret. Returns the auth strength.

    TradingView cannot set headers, so the secret travels in the body. It is
    accepted from a named field, or inline in a plain-text alert -- the second
    is weaker, because a secret matched by substring cannot be told apart from
    one that merely appears somewhere, and the strength is recorded so a later
    policy can require `strong`.

    **An unset secret refuses everything.** The CLI receiver warns and
    continues, which is right for a tool an operator is watching; a server
    endpoint that accepted anything because it was misconfigured would be an
    open write path into the signal table.
    """
    if not secret:
        raise Unauthorized("no webhook secret is configured; the receiver refuses all alerts")
    supplied = str(payload.get("secret") or payload.get("passphrase") or "")
    if supplied and hmac.compare_digest(supplied, secret):
        return "strong"
    if secret in raw_body:
        return "weak"
    raise Unauthorized("bad or missing secret")


#: The alert keys that carry a stop and a target, in the order they are
#: preferred. `parse_alert` has already normalised the values to text.
_STOP_KEYS = ("stop_loss", "sl")
_TARGET_KEYS = ("take_profit", "tp")


def _first(advisory: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = advisory.get(key)
        if value not in (None, ""):
            return value
    return None


def _bracket_meta(alert: Alert, routing: Route) -> dict[str, object]:
    """The platform's bracket for this signal, and where it came from.

    **The fifth and last link of L45 F-1, and the only one that is a policy
    rather than a fact.** `fixed_risk` sizing needs a stop distance and does
    not invent one; nothing on this path produces a bracket; an ATR-derived one
    cannot be computed while `market_bars` covers one instrument.

    So the alert's own suggestion is the only bracket that exists -- and it is
    quarantined under `advisory_ignored` deliberately, because under
    `fixed_risk` a TIGHTER stop produces a LARGER position. Using it is
    therefore a decision an operator makes for one bot at a time
    (`bots.use_alert_bracket`, false by default), never a default.

    `bracket_source` is always written, including when there is no bracket, so
    "where did this order's stop come from" has an answer on the record rather
    than being reconstructed from which keys happen to be present.
    """
    if not routing.use_alert_bracket:
        return {
            "bracket_source": "none",
            "bracket_detail": (
                "this bot has not opted into the alert's bracket, and the platform "
                "has no bracket policy of its own; sizing will refuse this signal"
            ),
        }
    stop = _first(alert.advisory, _STOP_KEYS)
    target = _first(alert.advisory, _TARGET_KEYS)
    if stop is None:
        return {
            "bracket_source": "alert",
            "bracket_detail": (
                "this bot uses the alert's bracket and the alert carried no stop; "
                "sizing will refuse this signal"
            ),
        }
    return {
        "bracket_source": "alert",
        # The PLATFORM's fields now, promoted out of the quarantine by an
        # explicit per-bot decision. `advisory_ignored` keeps the originals, so
        # what was suggested and what was used stay separately auditable.
        "stop_loss": stop,
        "take_profit": target,
        "bracket_detail": "the stop and target were taken from the alert, per bot policy",
    }


@dataclass
class WebhookGateway:
    secret: str
    mode: str = "paper"
    max_age_seconds: int = 120
    future_tolerance_seconds: int = 30
    restrict_to_tradingview_ips: bool = False

    # ------------------------------------------------------------- helpers

    def _check_source(self, source_ip: str | None) -> None:
        if not self.restrict_to_tradingview_ips:
            return
        if source_ip not in TRADINGVIEW_IPS:
            raise Unauthorized("source address is not a published TradingView address")

    async def _existing(self, db: AsyncSession, key: str) -> WebhookEvent | None:
        return await db.scalar(select(WebhookEvent).where(WebhookEvent.idempotency_key == key))

    async def _strategy_version_id(self, db: AsyncSession, alert: Alert) -> str | None:
        """Map a named strategy to a version, refusing an unknown name.

        An alert may omit the strategy: that is the "external alert" case, and
        it is supported explicitly rather than by accident -- the signal is
        recorded with `strategy_version_id` NULL and its source says where it
        came from. What is *not* supported is naming a strategy that does not
        exist, because a payload that could select an arbitrary strategy could
        select a privileged one.
        """
        if not alert.strategy:
            return None
        strategy = await db.scalar(select(Strategy).where(Strategy.key == alert.strategy))
        if strategy is None:
            raise PayloadError(
                f"unknown strategy {alert.strategy!r}; register it before sending alerts for it"
            )
        statement = select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id)
        if alert.strategy_version:
            if not alert.strategy_version.isdigit():
                raise PayloadError("strategy_version must be a version number")
            statement = statement.where(StrategyVersion.version == int(alert.strategy_version))
        else:
            statement = statement.order_by(StrategyVersion.version.desc())
        version = await db.scalar(statement)
        if version is None:
            raise PayloadError(f"strategy {alert.strategy!r} has no matching version")
        return version.id

    # -------------------------------------------------------------- intake

    async def receive(
        self,
        db: AsyncSession,
        payload: dict,
        raw_body: str,
        *,
        source_ip: str | None = None,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[Result, Event | None]:
        """Run the pipeline. Returns the result and the event to publish.

        The event is *returned* rather than published here so the caller can
        commit the signal first. An event announcing a row that is not yet
        durable is an event a subscriber can act on before it can read it.
        """
        now = now or datetime.now(UTC)
        self._check_source(source_ip)
        auth_strength = authenticate(payload, raw_body, self.secret)
        # Redacted immediately, before anything is stored or logged. Nothing
        # downstream of this line has the secret in hand.
        safe_payload = redact(payload, self.secret)

        try:
            alert = parse_alert(payload)
            check_age(
                alert.signal_time,
                now,
                max_age_seconds=self.max_age_seconds,
                future_tolerance_seconds=self.future_tolerance_seconds,
            )
        except PayloadError as exc:
            return (
                await self._record_rejection(
                    db, safe_payload, str(exc), auth_strength, source_ip, now
                ),
                None,
            )

        key = alert.idempotency_key()
        existing = await self._existing(db, key)
        if existing is not None:
            # One alert, one logical signal, however many times it arrives.
            log.info(
                "duplicate alert",
                extra={
                    "event": "webhook_duplicate",
                    "idempotency_key": key,
                    "webhook_event_id": existing.id,
                    "correlation_id": correlation_id,
                },
            )
            return (
                Result(
                    Outcome.duplicate,
                    "already received; no second signal was created",
                    signal_id=existing.signal_id,
                    webhook_event_id=existing.id,
                ),
                None,
            )

        try:
            symbol = await symbols.resolve_source(db, "tradingview", alert.ticker)
        except SymbolError as exc:
            # Never guessed at. An unmapped ticker would otherwise be resolved
            # to whatever the broker happens to call something similar.
            return (
                await self._record_rejection(
                    db, safe_payload, str(exc), auth_strength, source_ip, now, key=key
                ),
                None,
            )

        try:
            strategy_version_id = await self._strategy_version_id(db, alert)
        except PayloadError as exc:
            return (
                await self._record_rejection(
                    db, safe_payload, str(exc), auth_strength, source_ip, now, key=key
                ),
                None,
            )

        event_row = WebhookEvent(
            provider="tradingview",
            idempotency_key=key,
            received_at=now.replace(tzinfo=None),
            source_ip=source_ip,
            auth_strength=auth_strength,
            payload=safe_payload if isinstance(safe_payload, dict) else {"body": safe_payload},
            status=str(Outcome.accepted),
        )
        try:
            # A SAVEPOINT, not a bare flush. Rolling the whole transaction back
            # on a lost race leaves `event_row` in the session's identity map as
            # a row that was never inserted, and the next flush tries to UPDATE
            # it: `StaleDataError: expected to update 1 row(s); 0 were matched`.
            # That was the second failure the concurrency test found, and it
            # appeared only once the first was fixed -- the unguarded flush had
            # been raising before the session could get into that state.
            #
            # `begin_nested` rolls back to the savepoint and expunges the failed
            # object, leaving the outer transaction usable, which is what lets
            # the duplicate answer be returned on the same session.
            async with db.begin_nested():
                db.add(event_row)
                await db.flush()
        except IntegrityError:
            # Two identical alerts racing, and this is the FIRST place the race
            # can be lost -- `webhook_events.idempotency_key` is unique too, and
            # this flush used to be the only unguarded one on the path. The
            # `signals.signal_key` handler below covers a race that gets past
            # here; a race that loses at this insert reached an unhandled
            # IntegrityError and became a 500.
            #
            # That mattered more than an ugly log line: TradingView RETRIES on a
            # 5xx, so the one condition this gateway exists to absorb -- the same
            # alert delivered twice -- answered in the way most likely to
            # produce a third delivery. Found by the L40 concurrency test, which
            # is the only thing that runs the two inserts at once.
            # The savepoint rollback usually detaches it already; the guard is
            # for the case where it did not, because `expunge` on an absent
            # instance raises and would turn this refusal back into a 500.
            if event_row in db:
                db.expunge(event_row)
            existing = await self._existing(db, key)
            return (
                Result(
                    Outcome.duplicate,
                    "already received; no second signal was created",
                    signal_id=existing.signal_id if existing else None,
                    webhook_event_id=existing.id if existing else None,
                ),
                None,
            )

        # WHICH BOT, and therefore which account. **L45 F-1.**
        #
        # An alert names a strategy and must not name an account: a payload
        # that could select an account could select somebody else's. The bot
        # is the join, and `route_for` refuses ambiguity rather than choosing.
        #
        # A signal that cannot be routed is still RECORDED. It simply will not
        # execute, and the reason is written down beside it instead of being
        # rediscovered later from an empty `orders` table.
        routing = await route_for(db, strategy_version_id=strategy_version_id, mode=self.mode)
        routing_meta: dict[str, object] = (
            {
                "account_id": routing.account_id,
                "bot_id": routing.bot_id,
                # None means NOT SET, and the pipeline's own configuration
                # applies. It is not "no risk budget".
                "risk_amount": (
                    str(routing.risk_amount) if routing.risk_amount is not None else None
                ),
                **_bracket_meta(alert, routing),
            }
            if isinstance(routing, Route)
            else {"not_executable": routing.reason}
        )

        signal = Signal(
            signal_key=key,
            source="tradingview",
            strategy_version_id=strategy_version_id,
            webhook_event_id=event_row.id,
            symbol_id=symbol.id,
            direction=alert.direction,
            # No confidence is asserted. The AI layer may lower one; nothing
            # here may invent one, and an alert does not carry evidence.
            confidence=None,
            mode=self.mode,
            signal_time=alert.signal_time.astimezone(UTC).replace(tzinfo=None),
            received_at=now.replace(tzinfo=None),
            auth_strength="strong" if auth_strength == "strong" else "weak",
            status="new",
            meta={
                "ticker": alert.ticker,
                "exchange": alert.exchange,
                "timeframe": alert.timeframe,
                "action": str(alert.action),
                "price_reported": str(alert.price) if alert.price is not None else None,
                # Recorded so the alert can be audited against what the
                # platform later did. Never obeyed: the size and the bracket
                # an alert suggests are quarantined under this name so no
                # consumer picks them up by accident.
                "advisory_ignored": alert.advisory,
                "extra": alert.extra,
                "correlation_id": correlation_id,
                # ------------------------------------------------- L45 F-1
                # The facts the execution worker needs, written by the one
                # component that has already resolved them.
                #
                # `app/main.py::_to_incoming_signal` reads exactly these keys.
                # None of them was ever written, so it returned None for every
                # alert this platform has ever received and the worker retired
                # each one as `signal_not_executable`. The webhook half and the
                # pipeline half were both verified; nothing joined them.
                #
                # `internal_symbol` is the platform's own code, not the
                # ticker: `alert.ticker` is "OANDA:EURUSD" and the pipeline
                # needs "EURUSD" to find a contract spec.
                "internal_symbol": symbol.code,
                # The strategy KEY, not the version id. The pipeline passes it
                # to `strategy_state()`, which is keyed by `Strategy.key`, and
                # it is already validated to exist by `_strategy_version_id`.
                "strategy_id": alert.strategy,
                **routing_meta,
            },
        )
        db.add(signal)
        try:
            await db.flush()
        except IntegrityError:
            # Two identical alerts racing. The unique key on `signal_key` is
            # the arbiter, and losing the race is a duplicate, not an error.
            #
            # The rollback undoes the `webhook_events` insert as well, and both
            # rows stay in the session's identity map believing they are
            # persistent -- so the next flush emits an UPDATE for a row that
            # does not exist and raises `StaleDataError`. Expunging them is what
            # makes the rollback complete; this handler has been here since L09
            # and the defect only shows under real concurrency, which is why the
            # L40 test is the first thing to hit it.
            await db.rollback()
            for orphan in (signal, event_row):
                if orphan in db:
                    db.expunge(orphan)
            existing = await self._existing(db, key)
            return (
                Result(
                    Outcome.duplicate,
                    "already received; no second signal was created",
                    signal_id=existing.signal_id if existing else None,
                    webhook_event_id=existing.id if existing else None,
                ),
                None,
            )
        event_row.signal_id = signal.id
        await db.flush()

        log.info(
            "alert accepted",
            extra={
                "event": "webhook_accepted",
                "signal_id": signal.id,
                "webhook_event_id": event_row.id,
                "symbol": symbol.code,
                "direction": alert.direction,
                "auth_strength": auth_strength,
                "correlation_id": correlation_id,
                # The payload itself is never logged: it carried the secret a
                # moment ago and a log line is the easiest place to leak one.
            },
        )

        published = Event(
            type=str(EventType.SIGNAL_CREATED),
            payload={
                "signal_id": signal.id,
                "symbol": symbol.code,
                "direction": alert.direction,
                "source": "tradingview",
                "signal_time": alert.signal_time.astimezone(UTC).isoformat(),
                "status": "new",
            },
            channel=str(
                Channel(Scope.strategy, strategy_version_id)
                if strategy_version_id
                else Channel(Scope.symbol, symbol.code)
            ),
            correlation_id=correlation_id,
            source="webhook",
        )
        return (
            Result(
                Outcome.accepted,
                "signal recorded",
                signal_id=signal.id,
                webhook_event_id=event_row.id,
            ),
            published,
        )

    async def _record_rejection(
        self,
        db: AsyncSession,
        safe_payload: object,
        reason: str,
        auth_strength: str,
        source_ip: str | None,
        now: datetime,
        *,
        key: str | None = None,
    ) -> Result:
        """Record why an authenticated alert was refused.

        Rejections are stored because "we never received it" and "we received
        it and would not act on it" are different answers to the same question,
        and only one of them means the sender should look at its own config.

        The key falls back to a time-based one so two different bad payloads do
        not collide on the unique index.
        """
        row = WebhookEvent(
            provider="tradingview",
            idempotency_key=key or f"tv:rej:{now.timestamp():.6f}",
            received_at=now.replace(tzinfo=None),
            source_ip=source_ip,
            auth_strength=auth_strength,
            payload=safe_payload if isinstance(safe_payload, dict) else {"body": safe_payload},
            status=str(Outcome.rejected),
            error=reason[:500],
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return Result(Outcome.duplicate, "already received", http_status=200)
        log.warning(
            "alert rejected",
            extra={
                "event": "webhook_rejected",
                "reason": reason[:200],
                "webhook_event_id": row.id,
                "auth_strength": auth_strength,
            },
        )
        return Result(Outcome.rejected, reason, webhook_event_id=row.id, http_status=422)
