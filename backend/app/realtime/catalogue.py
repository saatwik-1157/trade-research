"""The event catalogue: every type the platform may publish, and where it goes.

This is a contract, not an implementation. Most of these types have no
producer yet -- there is no OMS to emit ORDER_FILLED and no bot manager to
emit BOT_STARTED -- and declaring them now is what lets L19 and L22 publish
without also getting to invent the envelope, the channel and the audience.

Two rules the catalogue enforces, both tested:

  * **Every type has exactly one channel scope.** A type whose scope is
    `account` may only ever be published on an `account:{id}` channel. An
    event cannot be routed somewhere its own definition does not allow, so a
    private fill cannot be published on `system` by a caller in a hurry.
  * **Nothing is published on a type that is not here.** An unknown type is
    refused at publish time rather than delivered and ignored, because a
    subscriber that never fires looks exactly like a market that never moved.

Nothing in this module can execute anything. It names messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Scope(StrEnum):
    """Who an event is for. Decides which channel may carry it."""

    system = "system"  # everyone signed in; never carries private figures
    user = "user"  # one user's own notifications
    account = "account"  # one trading account: orders, positions, portfolio
    symbol = "symbol"  # one instrument: market data, public
    strategy = "strategy"  # one strategy's signals
    bot = "bot"  # one bot's lifecycle
    # One model family. Added at L28: a lifecycle event is not an account's, a
    # strategy's or a bot's, and delivering it on one of those channels would
    # send it to whoever happened to be subscribed there.
    model = "model"  # one model family: training, validation, lifecycle


class EventType(StrEnum):
    # --- Market data (L08) -------------------------------------------------
    MARKET_UPDATE = "MARKET_UPDATE"

    # --- Signals (L09, L12, L16) -------------------------------------------
    SIGNAL_CREATED = "SIGNAL_CREATED"
    SIGNAL_UPDATED = "SIGNAL_UPDATED"

    # --- Orders (L19) ------------------------------------------------------
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_UPDATED = "ORDER_UPDATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_FAILED = "ORDER_FAILED"
    # Not an error to tidy away. We do not know what the venue did, and the
    # OMS resolves it by reconciling, never by retrying.
    ORDER_UNKNOWN = "ORDER_UNKNOWN"

    # --- Positions (L21) ---------------------------------------------------
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_CLOSED = "POSITION_CLOSED"

    # --- Bots (L22) --------------------------------------------------------
    BOT_STARTED = "BOT_STARTED"
    BOT_PAUSED = "BOT_PAUSED"
    BOT_STOPPED = "BOT_STOPPED"
    BOT_ERROR = "BOT_ERROR"
    BOT_RECOVERING = "BOT_RECOVERING"

    # --- Risk (L17) --------------------------------------------------------
    # RISK_APPROVED is published as well as RISK_REJECTED. A veto that leaves
    # no trace is indistinguishable from a check that never ran, and so is an
    # approval.
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_ALERT = "RISK_ALERT"

    # --- Broker (L10, L38) -------------------------------------------------
    BROKER_CONNECTED = "BROKER_CONNECTED"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    BROKER_RECONNECTING = "BROKER_RECONNECTING"

    # --- AI models (L25, L26, L28) -----------------------------------------
    #
    # Two progress types rather than one per stage: a training run publishes a
    # dozen stage changes and cataloguing each would make the vocabulary a log
    # format. The specific event is in the payload; what the catalogue carries
    # is who may receive it.
    TRAINING_JOB_UPDATED = "TRAINING_JOB_UPDATED"
    VALIDATION_RUN_UPDATED = "VALIDATION_RUN_UPDATED"
    # The lifecycle transitions, one type each, because these are the ones an
    # operator reacts to differently -- the same reasoning that keeps
    # ORDER_FILLED apart from ORDER_CANCELLED.
    MODEL_REGISTERED = "MODEL_REGISTERED"
    MODEL_PAPER_ACTIVATED = "MODEL_PAPER_ACTIVATED"
    MODEL_ACTIVATED = "MODEL_ACTIVATED"
    MODEL_ROLLED_BACK = "MODEL_ROLLED_BACK"
    MODEL_RETIRED = "MODEL_RETIRED"
    MODEL_REJECTED = "MODEL_REJECTED"
    # L29 monitoring. Health CHANGED rather than health reported: a state that
    # is the same as last run is not an event, and publishing one every run is
    # the flood the alert deduplication exists to prevent.
    MODEL_HEALTH_CHANGED = "MODEL_HEALTH_CHANGED"
    MODEL_ALERT_CREATED = "MODEL_ALERT_CREATED"
    MODEL_ALERT_RECOVERED = "MODEL_ALERT_RECOVERED"

    # --- Portfolio (L30) ---------------------------------------------------
    #
    # Four types rather than one, for the reason the order vocabulary is split:
    # an operator does something different about each. A figure moving is
    # routine, an exposure change is a position opening or closing, a drawdown
    # crossing a threshold is a reason to look, and the view going STALE or
    # RECONCILIATION_REQUIRED means the numbers on the screen are no longer
    # what is held.
    PORTFOLIO_UPDATED = "PORTFOLIO_UPDATED"
    EXPOSURE_UPDATED = "EXPOSURE_UPDATED"
    DRAWDOWN_ALERT = "DRAWDOWN_ALERT"
    PORTFOLIO_HEALTH_CHANGED = "PORTFOLIO_HEALTH_CHANGED"

    # --- Trade journal (L31) -----------------------------------------------
    #
    # THREE types, not the brief's six. `trade.opened` and
    # `trade.partially_closed` are POSITION events and already exist above as
    # POSITION_OPENED and POSITION_UPDATED; publishing them again under a trade
    # name would give the platform two vocabularies for one fact, which is the
    # duplication section 41 warns against. What is genuinely new is the
    # JOURNAL ROW: it was written, it was corrected, or the venue disagrees
    # with it.
    TRADE_RECORDED = "TRADE_RECORDED"
    TRADE_UPDATED = "TRADE_UPDATED"
    TRADE_RECONCILIATION_REQUIRED = "TRADE_RECONCILIATION_REQUIRED"

    # --- Trade review (L33) ------------------------------------------------
    #
    # Section 49 asks for the events L34 will consume, and for the notification
    # system NOT to be built here. These are the interface: a review finished, a
    # review failed, or an aggregate pattern was observed. Nothing in L33 sends
    # a notification.
    TRADE_REVIEW_COMPLETED = "TRADE_REVIEW_COMPLETED"
    TRADE_REVIEW_FAILED = "TRADE_REVIEW_FAILED"
    TRADE_PATTERN_DETECTED = "TRADE_PATTERN_DETECTED"

    # --- Platform ----------------------------------------------------------
    SYSTEM_ALERT = "SYSTEM_ALERT"
    NOTIFICATION_CREATED = "NOTIFICATION_CREATED"

    # --- Security (L39) ----------------------------------------------------
    #
    # Two types rather than one, and the split is an authorization decision
    # rather than a taxonomy. A security alert is the easiest place in the
    # whole catalogue to break the `system` scope's rule that it "never carries
    # private figures": the natural sentence is "12 failed logins for
    # alice@example.com from 203.0.113.9", which publishes an email address and
    # an address to every signed-in browser.
    #
    # So the platform-wide one carries a COUNT AND A CLASS and never a subject,
    # and anything that names a person is user-scoped and goes to that person.
    # L34 defined `Category.security` and left it with no rule because there
    # was no type to route; these are it.
    SECURITY_ALERT = "SECURITY_ALERT"
    ACCOUNT_SECURITY_ALERT = "ACCOUNT_SECURITY_ALERT"


@dataclass(frozen=True)
class EventSpec:
    """One catalogue entry: its scope, and the level that starts producing it."""

    scope: Scope
    level: int
    what: str


CATALOGUE: dict[EventType, EventSpec] = {
    EventType.MARKET_UPDATE: EventSpec(Scope.symbol, 8, "a normalised quote or bar"),
    EventType.SIGNAL_CREATED: EventSpec(Scope.strategy, 9, "a validated signal"),
    EventType.SIGNAL_UPDATED: EventSpec(Scope.strategy, 12, "a signal changed state"),
    EventType.ORDER_CREATED: EventSpec(Scope.account, 19, "an order intent exists"),
    EventType.ORDER_UPDATED: EventSpec(Scope.account, 19, "an order changed state"),
    EventType.ORDER_SUBMITTED: EventSpec(Scope.account, 19, "sent to the venue"),
    EventType.ORDER_ACKNOWLEDGED: EventSpec(Scope.account, 19, "the venue took it"),
    EventType.ORDER_PARTIALLY_FILLED: EventSpec(Scope.account, 19, "part filled"),
    EventType.ORDER_FILLED: EventSpec(Scope.account, 19, "filled, as the venue reported"),
    EventType.ORDER_CANCELLED: EventSpec(Scope.account, 19, "cancelled"),
    EventType.ORDER_REJECTED: EventSpec(Scope.account, 19, "the venue refused, and said so"),
    EventType.ORDER_FAILED: EventSpec(Scope.account, 19, "the send itself failed"),
    EventType.ORDER_UNKNOWN: EventSpec(Scope.account, 19, "we do not know; reconcile"),
    EventType.POSITION_OPENED: EventSpec(Scope.account, 21, "a position exists"),
    EventType.POSITION_UPDATED: EventSpec(Scope.account, 21, "bracket or size changed"),
    EventType.POSITION_CLOSED: EventSpec(Scope.account, 21, "closed, confirmed by the venue"),
    EventType.BOT_STARTED: EventSpec(Scope.bot, 22, "a bot run began"),
    EventType.BOT_PAUSED: EventSpec(Scope.bot, 22, "paused by an operator"),
    EventType.BOT_STOPPED: EventSpec(Scope.bot, 22, "the run ended"),
    EventType.BOT_ERROR: EventSpec(Scope.bot, 22, "the run failed"),
    EventType.BOT_RECOVERING: EventSpec(Scope.bot, 22, "reconciling after a restart"),
    EventType.RISK_APPROVED: EventSpec(Scope.account, 17, "the engine approved"),
    EventType.RISK_REJECTED: EventSpec(Scope.account, 17, "the engine vetoed"),
    EventType.RISK_ALERT: EventSpec(Scope.account, 17, "a limit is close or breached"),
    EventType.BROKER_CONNECTED: EventSpec(Scope.account, 10, "the adapter connected"),
    EventType.BROKER_DISCONNECTED: EventSpec(Scope.account, 10, "the adapter lost the terminal"),
    EventType.BROKER_RECONNECTING: EventSpec(Scope.account, 10, "reconnecting"),
    EventType.TRAINING_JOB_UPDATED: EventSpec(Scope.model, 25, "a training job changed state"),
    EventType.VALIDATION_RUN_UPDATED: EventSpec(Scope.model, 26, "a validation run changed state"),
    EventType.MODEL_REGISTERED: EventSpec(Scope.model, 28, "a candidate was accepted"),
    EventType.MODEL_PAPER_ACTIVATED: EventSpec(Scope.model, 28, "deployed to paper"),
    EventType.MODEL_ACTIVATED: EventSpec(Scope.model, 28, "the version a scope resolves to"),
    EventType.MODEL_ROLLED_BACK: EventSpec(Scope.model, 28, "withdrawn; the previous one is back"),
    EventType.MODEL_RETIRED: EventSpec(Scope.model, 28, "deliberately withdrawn"),
    EventType.MODEL_REJECTED: EventSpec(Scope.model, 28, "refused; never deleted"),
    EventType.MODEL_HEALTH_CHANGED: EventSpec(Scope.model, 29, "the health state moved"),
    EventType.MODEL_ALERT_CREATED: EventSpec(Scope.model, 29, "a monitoring condition opened"),
    EventType.MODEL_ALERT_RECOVERED: EventSpec(Scope.model, 29, "a condition cleared"),
    EventType.PORTFOLIO_UPDATED: EventSpec(Scope.account, 30, "balance, equity or P&L moved"),
    EventType.EXPOSURE_UPDATED: EventSpec(Scope.account, 30, "gross or net exposure changed"),
    EventType.DRAWDOWN_ALERT: EventSpec(Scope.account, 30, "drawdown crossed a threshold"),
    EventType.PORTFOLIO_HEALTH_CHANGED: EventSpec(
        Scope.account, 30, "stale, reconciliation required, or recovered"
    ),
    EventType.TRADE_RECORDED: EventSpec(Scope.account, 31, "a completed trade was journalled"),
    EventType.TRADE_UPDATED: EventSpec(Scope.account, 31, "a journal row was corrected"),
    EventType.TRADE_RECONCILIATION_REQUIRED: EventSpec(
        Scope.account, 31, "the venue disagrees with a journal row"
    ),
    EventType.TRADE_REVIEW_COMPLETED: EventSpec(Scope.account, 33, "a trade review finished"),
    EventType.TRADE_REVIEW_FAILED: EventSpec(
        Scope.account, 33, "a trade review could not be produced"
    ),
    EventType.TRADE_PATTERN_DETECTED: EventSpec(
        Scope.account, 33, "an aggregate pattern was observed across trades"
    ),
    EventType.SYSTEM_ALERT: EventSpec(Scope.system, 7, "a platform-wide notice"),
    EventType.NOTIFICATION_CREATED: EventSpec(Scope.user, 34, "a notification for one user"),
    EventType.SECURITY_ALERT: EventSpec(Scope.system, 39, "a security condition, counted"),
    EventType.ACCOUNT_SECURITY_ALERT: EventSpec(Scope.user, 39, "a security event on one account"),
}

# Types with a producer today. Everything else is a declared contract whose
# emitter arrives with its level, and `producing_level` says which.
PRODUCED_NOW: frozenset[EventType] = frozenset(
    {
        EventType.SYSTEM_ALERT,
        # L28 fixed the call that was supposed to emit these. `Hub.publish`
        # takes ONE argument and `TrainingService._publish` passed two, so every
        # training and validation event since L25 raised a TypeError that the
        # surrounding `except Exception` swallowed and logged as a warning. The
        # events never reached the bus and nothing noticed, because a subscriber
        # that never fires looks exactly like a market that never moved -- which
        # is the sentence `Hub.publish` itself uses to explain why it refuses an
        # uncatalogued type.
        EventType.TRAINING_JOB_UPDATED,
        EventType.VALIDATION_RUN_UPDATED,
        EventType.MODEL_REGISTERED,
        EventType.MODEL_PAPER_ACTIVATED,
        EventType.MODEL_ACTIVATED,
        EventType.MODEL_ROLLED_BACK,
        EventType.MODEL_RETIRED,
        EventType.MODEL_REJECTED,
        EventType.MODEL_HEALTH_CHANGED,
        EventType.MODEL_ALERT_CREATED,
        EventType.MODEL_ALERT_RECOVERED,
        # L30. Account-scoped, so `channels.py` already authorizes them: a
        # portfolio event carries the same figures the account channel already
        # carries, and inventing a `portfolio` scope would have meant a second
        # authorization rule for the same audience.
        EventType.PORTFOLIO_UPDATED,
        EventType.EXPOSURE_UPDATED,
        EventType.DRAWDOWN_ALERT,
        EventType.PORTFOLIO_HEALTH_CHANGED,
        # L31. Account-scoped for the same reason the portfolio events are: a
        # trade carries the figures the account channel already carries, and a
        # `journal` scope would be a second authorization rule for one audience.
        EventType.TRADE_RECORDED,
        EventType.TRADE_UPDATED,
        EventType.TRADE_RECONCILIATION_REQUIRED,
        # L33. Account-scoped like every other trade-shaped event, so
        # `channels.py` authorizes them with the rule it already had.
        EventType.TRADE_REVIEW_COMPLETED,
        EventType.TRADE_REVIEW_FAILED,
        EventType.TRADE_PATTERN_DETECTED,
        # L34. The type and the `user` scope have been here since L07 waiting
        # for a producer; `app/notifications/channels/inapp.py` is it. No new
        # type, no new scope, and no new authorization rule -- `channels.py`
        # has authorized `user:{id}` to exactly that user since L07.
        EventType.NOTIFICATION_CREATED,
        # L39. `app/api/v1/security.py` and `app/security/stepup.py` produce
        # them. No new scope and no new authorization rule: `system` and `user`
        # have both been authorized by `channels.py` since L07, which is the
        # whole reason the split above is expressible without one.
        EventType.SECURITY_ALERT,
        EventType.ACCOUNT_SECURITY_ALERT,
    }
)


def spec_for(event_type: str) -> EventSpec:
    """The catalogue entry, or a KeyError naming the unknown type."""
    try:
        return CATALOGUE[EventType(event_type)]
    except ValueError as exc:
        raise KeyError(f"{event_type!r} is not in the event catalogue") from exc


def is_known(event_type: str) -> bool:
    try:
        EventType(event_type)
    except ValueError:
        return False
    return True


def scope_of(event_type: str) -> Scope:
    return spec_for(event_type).scope


def as_dict() -> list[dict[str, object]]:
    """The catalogue as data, for the API and for the frontend to check against."""
    return [
        {
            "type": str(t),
            "scope": str(s.scope),
            "produced_from_level": s.level,
            "producing_now": t in PRODUCED_NOW,
            "what": s.what,
        }
        for t, s in CATALOGUE.items()
    ]
