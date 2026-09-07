"""Which domain events become notifications, and what they become.

Sections 4, 5, 6 and 8. One entry per event type: its category, its severity,
who it is addressed to, and which entity it is about.

**This is a routing table, not a producer.** Every type here already exists in
`app.realtime.catalogue` -- L34 invents no event, publishes no event and asks
no service to publish one. Section 6 and section 65: *do not invent events*.
The catalogue there is the vocabulary; this is the subset of it a person is
told about, and what they are told.

**Most of these have no producer yet, and that is stated rather than hidden.**
`app.realtime.catalogue.PRODUCED_NOW` is the platform's own answer to which
types are emitted today, and `live_now()` reads it rather than keeping a
second list that could drift. An entry for `BOT_ERROR` is a contract L22's
emitter will satisfy; until then no BOT_ERROR notification can exist, because
no BOT_ERROR event does. `GET /v1/notifications/contract` reports the split so
"quiet" can be told apart from "not built" -- the same distinction
`/v1/realtime/catalogue` already draws for events.

**Severity is fixed per type except where the event genuinely carries it.**
Four types are graded from their own payload: a risk alert that says
`breached`, a health change that says `unavailable` and a monitoring alert
that names its own severity are different operational facts under one type,
and flattening them would either cry wolf or hide a breach. Everything else
is a constant, because a severity computed from a payload is a severity that
can be computed wrongly.

**Nothing here decides anything about trading.** A severity is a colour and a
category is a shelf. `app/notifications` imports no risk engine, no order
manager and no broker adapter, and a test parses every module in the package
to keep that true.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.notifications.contract import Audience, Category, Severity
from app.realtime.catalogue import PRODUCED_NOW, EventType

#: Payload values that mean "this crossed the line", per type. Read from the
#: event rather than recomputed: section 19 is explicit that the notification
#: layer must not calculate an authoritative risk figure of its own, and the
#: cheapest way to obey it is never to hold a threshold here.
_BREACH_WORDS = frozenset({"breach", "breached", "exceeded", "violated", "blocked", "halted"})
_BAD_HEALTH = frozenset({"unavailable", "critical", "failed", "reconciliation_required"})
_DEGRADED_HEALTH = frozenset({"degraded", "stale", "warning", "recovering"})


def _word(payload: dict[str, Any], *keys: str) -> str:
    """The first of these keys present, lowercased. '' when none is."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def _risk_severity(payload: dict[str, Any]) -> Severity:
    """ERROR when the engine says a limit was breached, WARNING otherwise.

    Section 41. The distinction is the engine's, read from its own words. This
    function never compares a figure to a threshold -- doing so would be the
    second copy of the risk rules section 19 forbids, and the copy would be
    the one that drifts.
    """
    said = _word(payload, "outcome", "state", "status", "kind", "reason")
    if any(word in said for word in _BREACH_WORDS):
        return Severity.error
    if payload.get("kill_switch") is True or "kill_switch" in said:
        return Severity.critical
    return Severity.warning


def _health_severity(payload: dict[str, Any]) -> Severity:
    """Graded from the health word the producer used."""
    said = _word(payload, "health", "to", "status", "state")
    if said in _BAD_HEALTH:
        return Severity.error
    if said in _DEGRADED_HEALTH:
        return Severity.warning
    if said in ("healthy", "ok", "recovered", "normal"):
        return Severity.success
    return Severity.info


def _alert_severity(payload: dict[str, Any]) -> Severity:
    """L29 grades its own alerts. Read it; do not re-derive it.

    `app.monitoring.alerts.NOTIFICATION_SEVERITY` maps five check severities
    onto three words. Those three words arrive here on the event, and are
    widened onto L34's five rather than being recomputed from the finding --
    which this module cannot see and should not.
    """
    said = _word(payload, "severity")
    if said in ("critical",):
        return Severity.critical
    if said in ("serious", "error"):
        return Severity.error
    if said in ("warning",):
        return Severity.warning
    return Severity.info


def _security_severity(payload: dict[str, Any]) -> Severity:
    """L39 grades its own events. Read the word; do not re-derive it.

    `app/security/events.py` deliberately uses L34's five words rather than a
    sixth vocabulary, so this is a lookup and not a translation. It floors at
    WARNING: a security event that reached the notification path at all is one
    somebody chose to publish, and rendering it as INFO would file it beside a
    bot starting up.
    """
    said = _word(payload, "severity")
    if said in ("critical",):
        return Severity.critical
    if said in ("error",):
        return Severity.error
    return Severity.warning


def _drawdown_severity(payload: dict[str, Any]) -> Severity:
    """WARNING unless L30 says the threshold it names was a hard one."""
    said = _word(payload, "severity", "level", "threshold_kind")
    if said in ("critical", "breach", "breached"):
        return Severity.error
    return Severity.warning


@dataclass(frozen=True)
class Rule:
    """One event type's notification treatment.

    `entity_type` and `entity_keys` are how section 35 is honoured: the entity
    is named, the URL is not. The frontend resolves `("trade", "abc")` into
    whatever the journal route happens to be today, so renaming a page does
    not invalidate every notification already stored.

    `cooldown_seconds` is section 21's debounce, and it is None for everything
    that is a discrete fact. A trade closes once; there is nothing to debounce.
    It is set only where a *condition* is reported repeatedly by more than one
    observer -- which is section 20's fifty workers noticing one disconnect.
    """

    category: Category
    audience: Audience
    severity: Severity | Callable[[dict[str, Any]], Severity]
    entity_type: str | None = None
    entity_keys: tuple[str, ...] = ()
    cooldown_seconds: int | None = None

    def severity_for(self, payload: dict[str, Any]) -> Severity:
        if callable(self.severity):
            return self.severity(payload)
        return self.severity

    def entity_id_from(self, payload: dict[str, Any]) -> str | None:
        for key in self.entity_keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None


#: Ten minutes. Long enough that a burst of observers collapses into one
#: notification, short enough that a condition still open after it is said
#: again -- the balance `app.monitoring.alerts.deduplicate` already struck for
#: L29, restated here because L34 deduplicates on a different key.
STATE_COOLDOWN = 600

RULES: dict[EventType, Rule] = {
    # --- Trading ------------------------------------------------------------
    # L31's journal rows. The three that exist today.
    EventType.TRADE_RECORDED: Rule(
        Category.trading, Audience.owner, Severity.info, "trade", ("trade_id",)
    ),
    EventType.TRADE_UPDATED: Rule(
        Category.trading, Audience.owner, Severity.info, "trade", ("trade_id",)
    ),
    # Section 45. Not "the trade was wrong" -- the venue and the journal
    # disagree, and somebody has to look. ERROR, never a claim about which is
    # right, because the platform does not know.
    EventType.TRADE_RECONCILIATION_REQUIRED: Rule(
        Category.trading, Audience.owner, Severity.error, "trade", ("trade_id",)
    ),
    # --- Orders (L19 emits these) ------------------------------------------
    # Section 43: the low-value internal transitions are deliberately absent.
    # ORDER_CREATED, ORDER_UPDATED, ORDER_SUBMITTED and ORDER_ACKNOWLEDGED are
    # states an order passes through on its way to a fill; notifying on each
    # would mean five notifications per order and a muted notification centre.
    EventType.ORDER_FILLED: Rule(
        Category.trading, Audience.owner, Severity.success, "order", ("order_id",)
    ),
    EventType.ORDER_PARTIALLY_FILLED: Rule(
        Category.trading, Audience.owner, Severity.info, "order", ("order_id",)
    ),
    EventType.ORDER_REJECTED: Rule(
        Category.trading, Audience.owner, Severity.warning, "order", ("order_id",)
    ),
    EventType.ORDER_CANCELLED: Rule(
        Category.trading, Audience.owner, Severity.info, "order", ("order_id",)
    ),
    EventType.ORDER_FAILED: Rule(
        Category.trading, Audience.owner, Severity.error, "order", ("order_id",)
    ),
    # Sections 44 and 45. The whole reason this type exists separately from
    # ORDER_FAILED: we do not know what the venue did. The template says so.
    EventType.ORDER_UNKNOWN: Rule(
        Category.trading, Audience.owner, Severity.error, "order", ("order_id",)
    ),
    EventType.POSITION_OPENED: Rule(
        Category.trading, Audience.owner, Severity.info, "position", ("position_id",)
    ),
    EventType.POSITION_CLOSED: Rule(
        Category.trading, Audience.owner, Severity.info, "position", ("position_id",)
    ),
    # --- Risk ---------------------------------------------------------------
    # RISK_APPROVED is catalogued and deliberately NOT here: an approval on
    # every signal is the flood that makes a notification centre useless, and
    # it is already recorded as a risk decision row that the risk page reads.
    EventType.RISK_REJECTED: Rule(
        Category.risk, Audience.owner, Severity.warning, "account", ("account_id",)
    ),
    EventType.RISK_ALERT: Rule(
        Category.risk, Audience.owner, _risk_severity, "account", ("account_id",)
    ),
    # --- Portfolio ----------------------------------------------------------
    # PORTFOLIO_UPDATED and EXPOSURE_UPDATED are catalogued and NOT here.
    # Section 43 again: a figure moving is the normal state of a portfolio, and
    # a notification per tick is a chart with extra steps.
    EventType.DRAWDOWN_ALERT: Rule(
        Category.portfolio, Audience.owner, _drawdown_severity, "account", ("account_id",)
    ),
    EventType.PORTFOLIO_HEALTH_CHANGED: Rule(
        Category.portfolio,
        Audience.owner,
        _health_severity,
        "account",
        ("account_id",),
        cooldown_seconds=STATE_COOLDOWN,
    ),
    # --- Bots ---------------------------------------------------------------
    EventType.BOT_STARTED: Rule(Category.bots, Audience.owner, Severity.info, "bot", ("bot_id",)),
    EventType.BOT_PAUSED: Rule(Category.bots, Audience.owner, Severity.info, "bot", ("bot_id",)),
    EventType.BOT_STOPPED: Rule(Category.bots, Audience.owner, Severity.info, "bot", ("bot_id",)),
    EventType.BOT_ERROR: Rule(Category.bots, Audience.owner, Severity.error, "bot", ("bot_id",)),
    EventType.BOT_RECOVERING: Rule(
        Category.bots, Audience.owner, Severity.warning, "bot", ("bot_id",)
    ),
    # --- Broker -------------------------------------------------------------
    # Section 20's example, and the reason `cooldown_seconds` exists: fifty
    # workers noticing one disconnected terminal is one problem, not fifty.
    EventType.BROKER_DISCONNECTED: Rule(
        Category.broker,
        Audience.owner,
        Severity.error,
        "account",
        ("account_id",),
        cooldown_seconds=STATE_COOLDOWN,
    ),
    EventType.BROKER_CONNECTED: Rule(
        Category.broker,
        Audience.owner,
        Severity.success,
        "account",
        ("account_id",),
        cooldown_seconds=STATE_COOLDOWN,
    ),
    EventType.BROKER_RECONNECTING: Rule(
        Category.broker,
        Audience.owner,
        Severity.warning,
        "account",
        ("account_id",),
        cooldown_seconds=STATE_COOLDOWN,
    ),
    # --- AI: reviews (L33) --------------------------------------------------
    EventType.TRADE_REVIEW_COMPLETED: Rule(
        Category.ai, Audience.owner, Severity.info, "trade_review", ("review_id",)
    ),
    EventType.TRADE_REVIEW_FAILED: Rule(
        Category.ai, Audience.owner, Severity.warning, "trade", ("trade_id",)
    ),
    EventType.TRADE_PATTERN_DETECTED: Rule(
        Category.ai, Audience.owner, Severity.info, "account", ("account_id",)
    ),
    # --- AI: model lifecycle (L28) -----------------------------------------
    # Addressed to OPERATORS: a model version is not an account's, and there is
    # no owner to deliver it to. `manage_ai_models` is the permission the REST
    # surface and the `model` realtime scope already gate this on, so the
    # audience is the same set of people either way.
    EventType.MODEL_REGISTERED: Rule(
        Category.ai, Audience.operators, Severity.info, "model_version", ("model_version_id",)
    ),
    EventType.MODEL_PAPER_ACTIVATED: Rule(
        Category.ai, Audience.operators, Severity.success, "model_version", ("model_version_id",)
    ),
    EventType.MODEL_ACTIVATED: Rule(
        Category.ai, Audience.operators, Severity.success, "model_version", ("model_version_id",)
    ),
    EventType.MODEL_ROLLED_BACK: Rule(
        Category.ai, Audience.operators, Severity.warning, "model_version", ("model_version_id",)
    ),
    EventType.MODEL_RETIRED: Rule(
        Category.ai, Audience.operators, Severity.info, "model_version", ("model_version_id",)
    ),
    EventType.MODEL_REJECTED: Rule(
        Category.ai, Audience.operators, Severity.warning, "model_version", ("model_version_id",)
    ),
    # TRAINING_JOB_UPDATED and VALIDATION_RUN_UPDATED are catalogued and NOT
    # here on purpose. The catalogue's own note says a training run publishes a
    # dozen stage changes; one notification per stage is a log format.
    # --- Monitoring (L29) ---------------------------------------------------
    EventType.MODEL_HEALTH_CHANGED: Rule(
        Category.monitoring,
        Audience.operators,
        _health_severity,
        "model_version",
        ("model_version_id",),
        cooldown_seconds=STATE_COOLDOWN,
    ),
    EventType.MODEL_ALERT_CREATED: Rule(
        Category.monitoring,
        Audience.operators,
        _alert_severity,
        "model_version",
        ("model_version_id",),
    ),
    EventType.MODEL_ALERT_RECOVERED: Rule(
        Category.monitoring,
        Audience.operators,
        Severity.success,
        "model_version",
        ("model_version_id",),
    ),
    # --- Platform -----------------------------------------------------------
    # Addressed to OPERATORS rather than everyone, changed at L37 by the level
    # that actually supplies the producer. L34 catalogued this as a
    # user-facing platform notice; what L37 publishes onto it is infrastructure
    # health -- a stale feed, a stale worker, an order whose broker state could
    # not be established. Section 56 of L37: ordinary users should see only
    # what their permissions cover, and a read-only user cannot act on a
    # degraded event bus. `Audience.everyone` remains in the enum for a genuine
    # platform-wide notice.
    EventType.SYSTEM_ALERT: Rule(
        Category.system,
        Audience.operators,
        _health_severity,
        "system",
        ("component", "check"),
        cooldown_seconds=STATE_COOLDOWN,
    ),
    # --- Security (L39) -----------------------------------------------------
    #
    # `Category.security` was defined at L34 -- "authentication and privilege
    # events" -- with no rule, because there was no event type to route. These
    # two are it, and their audiences differ for the same reason their scopes
    # do.
    #
    # The platform-wide one goes to operators, not to everyone: a read-only
    # user cannot act on a burst of failed logins, and telling them turns a
    # security signal into background noise for the people who can. The
    # per-account one goes to `owner`, which the service resolves to the user
    # the event is about -- the whole point of "somebody changed your password"
    # is that it reaches the person who did not.
    #
    # `_security_severity` reads the severity the producer already decided
    # rather than inventing a second one. `app/security/events.py` uses L34's
    # own five words for exactly this reason.
    EventType.SECURITY_ALERT: Rule(
        Category.security,
        Audience.operators,
        _security_severity,
        "security",
        ("event",),
        cooldown_seconds=STATE_COOLDOWN,
    ),
    EventType.ACCOUNT_SECURITY_ALERT: Rule(
        Category.security,
        Audience.owner,
        _security_severity,
        "security",
        ("event",),
        # No cooldown. Each of these is a discrete thing that happened to one
        # account -- a password changed, sessions revoked -- and collapsing two
        # of them would hide the second, which is the one that matters when the
        # first was the attacker.
    ),
}

#: Types that are catalogued as events and deliberately produce no
#: notification, with the reason. Served by the contract endpoint, so "why am I
#: not told about this?" has an answer that is not "nobody thought about it".
NOT_NOTIFIED: dict[EventType, str] = {
    EventType.MARKET_UPDATE: "a quote per tick is a chart, not a notification",
    EventType.SIGNAL_CREATED: "a strategy's own output; the signals page is where it belongs",
    EventType.SIGNAL_UPDATED: "as above",
    EventType.ORDER_CREATED: "an intermediate order state on the way to a fill (section 43)",
    EventType.ORDER_UPDATED: "as above",
    EventType.ORDER_SUBMITTED: "as above",
    EventType.ORDER_ACKNOWLEDGED: "as above",
    EventType.POSITION_UPDATED: "a bracket or size change; the positions page shows it live",
    EventType.RISK_APPROVED: "an approval per signal is the flood that mutes a notification centre",
    EventType.PORTFOLIO_UPDATED: "a figure moving is the normal state of a portfolio",
    EventType.EXPOSURE_UPDATED: "as above",
    EventType.TRAINING_JOB_UPDATED: "a run publishes a dozen stage changes; one each is a log",
    EventType.VALIDATION_RUN_UPDATED: "as above",
    EventType.NOTIFICATION_CREATED: "this IS the notification; notifying about it would loop",
}


def rule_for(event_type: str) -> Rule | None:
    """The treatment for this type, or None if it produces no notification.

    None is the honest answer for an uncatalogued type as well as for one that
    is deliberately silent: in both cases nothing is created, and the consumer
    logs the type rather than guessing at a category.
    """
    try:
        return RULES.get(EventType(event_type))
    except ValueError:
        return None


def live_now() -> frozenset[EventType]:
    """The rules whose event actually has a producer today.

    Read from `app.realtime.catalogue.PRODUCED_NOW` rather than kept as a
    second list. Two lists of the same fact drift, and the one that drifts is
    always the one somebody is reading.
    """
    return frozenset(RULES) & PRODUCED_NOW


def as_dict() -> list[dict[str, Any]]:
    """The routing table as data, for `GET /v1/notifications/contract`."""
    live = live_now()
    out: list[dict[str, Any]] = []
    for event_type, rule in RULES.items():
        out.append(
            {
                "event_type": str(event_type),
                "category": str(rule.category),
                "audience": str(rule.audience),
                "severity": ("from the event" if callable(rule.severity) else str(rule.severity)),
                "entity_type": rule.entity_type,
                "cooldown_seconds": rule.cooldown_seconds,
                "producing_now": event_type in live,
            }
        )
    return sorted(out, key=lambda row: (row["category"], row["event_type"]))


__all__ = [
    "NOT_NOTIFIED",
    "RULES",
    "STATE_COOLDOWN",
    "Rule",
    "as_dict",
    "live_now",
    "rule_for",
]
