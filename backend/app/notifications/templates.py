"""Turning an event into a sentence a person can act on.

Sections 22, 23, 44 and 45, and the one rule this whole file exists to obey:

    **A figure that is not on the event does not appear in the message.**

Not as a zero, not as "unknown P&L", not as a plausible default. The line is
left out. A notification that says *"closed with +$0.00"* because the payload
carried no `net_profit` is a false statement about a trade, and it is
indistinguishable by eye from a true one -- which is exactly the failure the
platform's own top-level rule exists to prevent.

**Every trading message is stamped with its environment.** Section 23:
`[PAPER] Trade recorded`. Where the platform genuinely does not know, the
stamp is `[UNKNOWN]` rather than the safe-looking word, because a live trade
shown as paper is the mistake that costs money and a paper trade shown as
unknown is one that costs a second look.

**Uncertain broker state is described as uncertain.** Sections 44 and 45. An
order whose venue state the OMS could not establish produces *"the broker's
state for this order could not be established; it needs reconciliation"* and
never *"order failed"* -- the OMS has a separate ORDER_FAILED type for the
case where it does know, and conflating them would tell a user their order is
dead when it may be live at the venue.

Rendering is pure: payload in, strings out. No session, no clock, no service.
"""

from __future__ import annotations

from typing import Any

from app.notifications.catalogue import Rule
from app.notifications.contract import Category, Severity

#: Payload keys a template may read. Everything else on an event is ignored,
#: so widening an event's payload cannot silently widen what is emailed. The
#: stored `context` is this intersection, which is also what a channel adapter
#: is shown -- section 40's "do not dump the whole thing into the message".
READABLE_KEYS: frozenset[str] = frozenset(
    {
        "account_id",
        "action",
        "bot_id",
        "bot_name",
        "check",
        "closed_at",
        "component",
        "confidence",
        "count",
        "detail",
        "direction",
        "drawdown",
        "environment",
        "event",
        "exit_reason",
        "from",
        "health",
        "kill_switch",
        "message",
        "model",
        "model_key",
        "model_version",
        "model_version_id",
        "net_profit",
        "order_id",
        "outcome",
        "position_id",
        "quantity",
        "r_multiple",
        "reason",
        "resource",
        "review_id",
        "rule",
        "scope",
        "severity",
        "side",
        "state",
        "status",
        "strategy",
        "strategy_id",
        "subject",
        "symbol",
        "symbol_id",
        "threshold",
        "title",
        "to",
        "trade_id",
        "value",
        "volume",
        "window_seconds",
    }
)

#: How a stored environment is shown. `unknown` is spelled out rather than
#: omitted: an unlabelled trading notification would read as whichever
#: environment the reader assumed.
_LABELS = {
    "backtest": "BACKTEST",
    "paper": "PAPER",
    "demo": "DEMO",
    "live": "LIVE",
    "unknown": "UNKNOWN",
}

#: Categories whose messages must carry an environment stamp. Section 23 is
#: about trading activity; a model registration is not a trade and stamping it
#: `[PAPER]` would say something untrue about where it applies.
STAMPED: frozenset[Category] = frozenset(
    {Category.trading, Category.risk, Category.portfolio, Category.bots, Category.broker}
)


def label(environment: str | None) -> str:
    return _LABELS.get((environment or "unknown").lower(), "UNKNOWN")


def stamp(title: str, *, environment: str | None, category: Category) -> str:
    """`[PAPER] Trade recorded`. Section 23."""
    if category not in STAMPED:
        return title
    return f"[{label(environment)}] {title}"


def context_of(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload narrowed to what a template may read.

    Applied on the way in, not trusted at the call site. An event that grows a
    field cannot start emailing it, and nothing that is not on this list can
    reach a channel adapter.
    """
    return {k: v for k, v in payload.items() if k in READABLE_KEYS and v is not None}


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _lines(*parts: str | None) -> str:
    """Join the parts that exist. An absent part leaves no trace."""
    return " ".join(p for p in parts if p)


def _subject(payload: dict[str, Any]) -> str:
    """What the message is about, from whichever identifier the event carried.

    Never a fabricated name. A trade whose event carried no symbol is described
    by its id, because the id is a fact and a symbol would be a guess.
    """
    for key in ("symbol", "bot_name", "model_key", "model", "component", "subject"):
        value = _text(payload, key)
        if value:
            return value
    for key in ("trade_id", "order_id", "position_id", "bot_id", "account_id", "model_version_id"):
        value = _text(payload, key)
        if value:
            return value[:8]
    return ""


def _pnl(payload: dict[str, Any]) -> str | None:
    """The recorded result, or nothing.

    Two separate figures, and neither is derived from the other: `net_profit`
    is currency, `r_multiple` is risk units, and the platform has recorded
    trades carrying one and not the other. An absent figure produces no
    clause -- section 22.
    """
    net = _text(payload, "net_profit")
    r = _text(payload, "r_multiple")
    if net and r:
        return f"Result {net} ({r}R)."
    if net:
        return f"Result {net}."
    if r:
        return f"Result {r}R."
    return None


def _reason(payload: dict[str, Any]) -> str | None:
    for key in ("reason", "exit_reason", "detail", "message"):
        value = _text(payload, key)
        if value:
            return value if value.endswith(".") else f"{value}."
    return None


# ============================================================ per-type bodies
#
# Keyed by event type. A type with no entry falls back to `_generic`, which
# states the event and the entity and nothing else -- honest, if plain. That
# fallback is what makes a new catalogued type safe to route before anybody
# writes it a sentence.


def _trade_recorded(p: dict[str, Any]) -> tuple[str, str]:
    side = _text(p, "side")
    subject = _subject(p)
    what = _lines(side.upper() if side else None, subject).strip()
    return (
        "Trade recorded",
        _lines(
            f"{what} was journalled." if what else "A trade was journalled.",
            _pnl(p),
            _reason(p),
        ),
    )


def _trade_updated(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Trade corrected",
        _lines(f"The journal row for {_subject(p)} was corrected.".strip(), _pnl(p)),
    )


def _trade_reconcile(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Trade needs reconciliation",
        _lines(
            f"The venue disagrees with the journalled trade {_subject(p)}.".strip(),
            "Neither record is assumed correct; the trade needs reconciling.",
            _reason(p),
        ),
    )


def _order_filled(p: dict[str, Any]) -> tuple[str, str]:
    return ("Order filled", _lines(f"{_subject(p)} filled.".strip(), _reason(p)))


def _order_partial(p: dict[str, Any]) -> tuple[str, str]:
    qty = _text(p, "quantity")
    return (
        "Order partially filled",
        _lines(
            f"{_subject(p)} filled in part.".strip(),
            f"Filled quantity {qty}." if qty else None,
        ),
    )


def _order_rejected(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Order rejected",
        _lines(f"The venue refused {_subject(p)}.".strip(), _reason(p)),
    )


def _order_cancelled(p: dict[str, Any]) -> tuple[str, str]:
    return ("Order cancelled", _lines(f"{_subject(p)} was cancelled.".strip(), _reason(p)))


def _order_failed(p: dict[str, Any]) -> tuple[str, str]:
    """The send failed and we know it did. Distinct from ORDER_UNKNOWN."""
    return (
        "Order failed to send",
        _lines(
            f"{_subject(p)} was not accepted by the venue.".strip(),
            _reason(p),
        ),
    )


def _order_unknown(p: dict[str, Any]) -> tuple[str, str]:
    """Sections 44 and 45. The wording here is the point of the section.

    It does not say failed, it does not say filled, and it does not offer a
    likelihood. The OMS reconciles against the broker; until it has, the only
    true sentence is that the state is not established.
    """
    return (
        "Order state requires reconciliation",
        _lines(
            f"The broker's state for {_subject(p)} could not be established.".strip(),
            "The order may or may not have reached the venue. It is being reconciled;"
            " no conclusion has been drawn.",
        ),
    )


def _position_opened(p: dict[str, Any]) -> tuple[str, str]:
    side = _text(p, "side")
    return (
        "Position opened",
        _lines(f"{(side.upper() + ' ') if side else ''}{_subject(p)} is open.".strip()),
    )


def _position_closed(p: dict[str, Any]) -> tuple[str, str]:
    return ("Position closed", _lines(f"{_subject(p)} closed.".strip(), _pnl(p), _reason(p)))


def _risk_rejected(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Risk engine refused a trade",
        _lines("The risk engine vetoed a proposed trade.", _reason(p)),
    )


def _risk_alert(p: dict[str, Any]) -> tuple[str, str]:
    """Section 41. What the engine said, and nothing about what it did.

    The message reports the alert. It does not state that trading was blocked
    unless the engine's own payload says so -- section 41 asks for exactly that
    restraint, because "new trading blocked" is a claim about enforcement and
    only the RiskEngine can make it.
    """
    blocked = p.get("kill_switch") is True or "block" in str(p.get("outcome", "")).lower()
    return (
        "Risk limit breached" if blocked else "Risk limit alert",
        _lines(
            _text(p, "rule") and f"Rule: {_text(p, 'rule')}." or None,
            _reason(p),
            "The risk engine reports that new trading is blocked." if blocked else None,
        )
        or "The risk engine raised an alert.",
    )


def _drawdown(p: dict[str, Any]) -> tuple[str, str]:
    value = _text(p, "drawdown") or _text(p, "value")
    threshold = _text(p, "threshold")
    return (
        "Drawdown threshold crossed",
        _lines(
            f"Drawdown is {value}." if value else "A drawdown threshold was crossed.",
            f"Threshold {threshold}." if threshold else None,
        ),
    )


def _portfolio_health(p: dict[str, Any]) -> tuple[str, str]:
    to = _text(p, "health") or _text(p, "to") or _text(p, "status")
    return (
        "Portfolio valuation health changed",
        _lines(
            f"The portfolio view is now {to}." if to else "The portfolio view changed state.",
            _reason(p),
            "Figures on screen may not match what is held until this clears."
            if to and to.lower() in ("stale", "reconciliation_required")
            else None,
        ),
    )


def _bot(title: str, sentence: str):  # noqa: ANN202 - closure factory
    def render(p: dict[str, Any]) -> tuple[str, str]:
        name = _text(p, "bot_name") or (_text(p, "bot_id") or "")[:8]
        strategy = _text(p, "strategy") or _text(p, "strategy_id")
        return (
            title,
            _lines(
                f"Bot {name} {sentence}.".strip() if name else f"A bot {sentence}.",
                f"Strategy {strategy}." if strategy else None,
                _reason(p),
            ),
        )

    return render


def _broker_disconnected(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Broker disconnected",
        _lines(
            "The broker adapter lost its connection.",
            _reason(p),
            "Order state at the venue is not being observed while this lasts.",
        ),
    )


def _broker_connected(p: dict[str, Any]) -> tuple[str, str]:
    return ("Broker connected", "The broker adapter is connected.")


def _broker_reconnecting(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Broker reconnecting",
        _lines("The broker adapter is reconnecting.", _reason(p)),
    )


def _review_completed(p: dict[str, Any]) -> tuple[str, str]:
    """Section 40. A pointer, not the review.

    The outcome and the confidence are two recorded fields; the narrative,
    the lessons and the follow-up questions stay in the platform. Section 40
    asks not to put excessive AI output in a notification, and an emailed
    review is a review that leaves the platform's access control behind.
    """
    outcome = _text(p, "outcome")
    confidence = _text(p, "confidence")
    return (
        "Trade review ready",
        _lines(
            f"A review of trade {(_text(p, 'trade_id') or '')[:8]} is ready.".strip(),
            f"Recorded outcome: {outcome}." if outcome else None,
            f"Confidence {confidence}." if confidence else None,
            "Open it in the platform to read the full review.",
        ),
    )


def _review_failed(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Trade review could not be produced",
        _lines(
            "A trade review failed to generate.",
            _reason(p),
            "The trade itself is unaffected; review generation never alters a trade.",
        ),
    )


def _pattern(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Pattern observed across trades",
        _lines(
            _text(p, "title") or "An aggregate pattern was observed across your trades.",
            _reason(p),
            "An observation, not a recommendation.",
        ),
    )


def _model(title: str, sentence: str):  # noqa: ANN202 - closure factory
    def render(p: dict[str, Any]) -> tuple[str, str]:
        key = _text(p, "model_key") or _text(p, "model") or ""
        version = _text(p, "model_version")
        named = " ".join(x for x in (key, version) if x)
        return (
            title,
            _lines(
                f"{named} {sentence}.".strip() if named else f"A model version {sentence}.",
                _reason(p),
            ),
        )

    return render


def _model_health(p: dict[str, Any]) -> tuple[str, str]:
    to = _text(p, "health") or _text(p, "to")
    key = _text(p, "model_key") or _text(p, "model") or "a model version"
    return (
        "Model health changed",
        _lines(f"{key} is now {to}." if to else f"{key} changed health state.", _reason(p)),
    )


def _model_alert(p: dict[str, Any]) -> tuple[str, str]:
    """Section 39. L29 measured it; this repeats what it said.

    The check, the subject and the severity come off the event. Nothing here
    recomputes drift -- section 19's rule applied to monitoring: the layer that
    owns the measurement is the only one that may state it.
    """
    check = _text(p, "check")
    subject = _text(p, "subject")
    return (
        "Model monitoring alert",
        _lines(
            f"{check} on {subject}."
            if check and subject
            else (check or "A monitoring condition opened."),
            _text(p, "title"),
            _reason(p),
        ),
    )


def _model_recovered(p: dict[str, Any]) -> tuple[str, str]:
    check = _text(p, "check")
    subject = _text(p, "subject")
    return (
        "Model monitoring condition cleared",
        _lines(
            f"{check} on {subject} has cleared." if check and subject else "A condition cleared.",
        ),
    )


def _system(p: dict[str, Any]) -> tuple[str, str]:
    component = _text(p, "component")
    return (
        _text(p, "title") or "Platform notice",
        _lines(
            f"Component: {component}." if component else None,
            _reason(p),
        )
        or "The platform published a notice.",
    )


def _generic(p: dict[str, Any]) -> tuple[str, str]:
    """The fallback: state the fact, invent nothing.

    Reached by a catalogued type that has a routing rule and no sentence yet.
    Plain is the correct failure here -- a template that guessed at wording
    would be inventing the very thing this module refuses to invent.
    """
    return ("Platform event", _lines(_text(p, "title"), _reason(p)) or "An event was recorded.")


#: The security event names, as a sentence rather than as a constant. The
#: mapping is here and not in `app/security/events.py` because that module must
#: not import the notification package -- and because wording is presentation.
_SECURITY_WORDS = {
    "LOGIN_FAILED": "a sign-in attempt failed",
    "LOGIN_RATE_LIMITED": "sign-in attempts were rate limited",
    "PASSWORD_CHANGED": "the account password was changed",
    "PASSWORD_RESET_REQUESTED": "a password reset was requested",
    "SESSIONS_REVOKED": "the account's sessions were signed out",
    "PERMISSION_DENIED": "a request was refused for lack of permission",
    "CSRF_FAILED": "a request failed the CSRF check",
    "RATE_LIMITED": "requests were rate limited",
    "STEP_UP_GRANTED": "a dangerous action was re-authenticated",
    "STEP_UP_FAILED": "a re-authentication did not match",
    "STEP_UP_MISSING": "a dangerous action was attempted without re-authentication",
    "ADMIN_ACTION": "an administrative action was taken",
    "DANGEROUS_ACTION": "a dangerous action was taken",
    "WEBHOOK_REJECTED": "a webhook alert was refused",
    "KILL_SWITCH_ENGAGED": "the kill switch was engaged",
    "SAFE_MODE_ENGAGED": "the platform entered safe mode",
}


def _security(p: dict[str, Any]) -> tuple[str, str]:
    """L39. Says what class of thing happened and how often. Never who.

    A SECURITY_ALERT is published on the `system` scope, which `catalogue.py`
    says never carries private figures -- so the producer sends a count and a
    class, and this renderer has nothing to leak even if it tried. The absence
    of an address or an email address here is a property of the payload
    contract, not of this function's discretion.
    """
    said = (_text(p, "event") or "").upper()
    sentence = _SECURITY_WORDS.get(said, "a security condition was recorded")
    count = p.get("count")
    window = p.get("window_seconds")
    if isinstance(count, int) and count > 1:
        head = f"{count} times: {sentence}"
        if isinstance(window, int) and window > 0:
            head += f", within {window} seconds"
    else:
        head = sentence[0].upper() + sentence[1:]
    return (
        "Security",
        _lines(
            head.rstrip(".") + ".",
            _text(p, "detail"),
            f"Action: {_text(p, 'action')}." if _text(p, "action") else None,
            f"Scope: {_text(p, 'scope')}." if _text(p, "scope") else None,
        )
        or "A security event was recorded.",
    )


def _account_security(p: dict[str, Any]) -> tuple[str, str]:
    """The same facts, addressed to the person they happened to.

    The second sentence is the point of the notification: somebody who did not
    do this needs to know what to do next, and "check with an administrator" is
    the honest instruction on a platform with no self-service session list.
    """
    title, body = _security(p)
    return (
        "Security: your account",
        _lines(
            body,
            "If this was not you, change the password and ask an administrator "
            "to sign out every session on the account.",
        ),
    )


RENDERERS: dict[str, Any] = {
    "SECURITY_ALERT": _security,
    "ACCOUNT_SECURITY_ALERT": _account_security,
    "TRADE_RECORDED": _trade_recorded,
    "TRADE_UPDATED": _trade_updated,
    "TRADE_RECONCILIATION_REQUIRED": _trade_reconcile,
    "ORDER_FILLED": _order_filled,
    "ORDER_PARTIALLY_FILLED": _order_partial,
    "ORDER_REJECTED": _order_rejected,
    "ORDER_CANCELLED": _order_cancelled,
    "ORDER_FAILED": _order_failed,
    "ORDER_UNKNOWN": _order_unknown,
    "POSITION_OPENED": _position_opened,
    "POSITION_CLOSED": _position_closed,
    "RISK_REJECTED": _risk_rejected,
    "RISK_ALERT": _risk_alert,
    "DRAWDOWN_ALERT": _drawdown,
    "PORTFOLIO_HEALTH_CHANGED": _portfolio_health,
    "BOT_STARTED": _bot("Bot started", "started"),
    "BOT_PAUSED": _bot("Bot paused", "was paused"),
    "BOT_STOPPED": _bot("Bot stopped", "stopped"),
    "BOT_ERROR": _bot("Bot failed", "failed"),
    "BOT_RECOVERING": _bot("Bot recovering", "is reconciling after a restart"),
    "BROKER_DISCONNECTED": _broker_disconnected,
    "BROKER_CONNECTED": _broker_connected,
    "BROKER_RECONNECTING": _broker_reconnecting,
    "TRADE_REVIEW_COMPLETED": _review_completed,
    "TRADE_REVIEW_FAILED": _review_failed,
    "TRADE_PATTERN_DETECTED": _pattern,
    "MODEL_REGISTERED": _model("Model registered", "was registered as a candidate"),
    "MODEL_PAPER_ACTIVATED": _model("Model deployed to paper", "was deployed to paper"),
    "MODEL_ACTIVATED": _model("Model activated", "is now the active version"),
    "MODEL_ROLLED_BACK": _model("Model rolled back", "was withdrawn; the previous version is back"),
    "MODEL_RETIRED": _model("Model retired", "was retired"),
    "MODEL_REJECTED": _model("Model rejected", "was refused"),
    "MODEL_HEALTH_CHANGED": _model_health,
    "MODEL_ALERT_CREATED": _model_alert,
    "MODEL_ALERT_RECOVERED": _model_recovered,
    "SYSTEM_ALERT": _system,
}


def render(
    event_type: str,
    payload: dict[str, Any],
    *,
    rule: Rule,
    # `str | None`, because `NotificationService._environment` genuinely
    # returns None for a notification about something that is not a trading
    # environment. `label()` and `stamp()` have always accepted None; only
    # this signature was narrower than its callers, which mypy caught at L43.
    environment: str | None,
    severity: Severity,
) -> tuple[str, str, dict[str, Any]]:
    """`(title, body, context)`. Pure; the same inputs always give the same text.

    `severity` is passed rather than re-derived so the sentence and the colour
    can never disagree -- one of them would be wrong and there would be no way
    to tell which.
    """
    context = context_of(payload)
    renderer = RENDERERS.get(event_type, _generic)
    title, body = renderer(context)
    if severity is Severity.critical and not title.lower().startswith("critical"):
        title = f"Critical: {title}"
    return stamp(title, environment=environment, category=rule.category), body.strip(), context


__all__ = ["READABLE_KEYS", "RENDERERS", "STAMPED", "context_of", "label", "render", "stamp"]
