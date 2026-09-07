"""The TradingView alert payload, and the vocabulary it may use.

TradingView alerts are **untrusted external input**. The endpoint is reachable
from the internet by necessity, the secret travels in the body because
TradingView cannot set headers, and the payload is whatever someone typed into
an alert box. So every field is validated and nothing is inferred.

Two rules shape the vocabulary:

  * **Only listed actions are executable words.** An unrecognised action is
    rejected, never interpreted. "Arbitrary text as an executable trading
    command" is the failure mode this exists to prevent.
  * **Quantity and risk from the payload are recorded, never obeyed.** They are
    kept in `metadata` so the alert can be audited against what the platform
    later did, and the platform recomputes size itself through Risk -> Sizing.
    An alert that could set its own lot size is an alert that can set its own
    risk limit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

MAX_BODY_BYTES = 64 * 1024  # an alert is a few hundred bytes
MAX_FIELD = 128


class PayloadError(Exception):
    """A payload that cannot be accepted. The reason is safe to return."""


class Action(StrEnum):
    """What the alert says to do, before normalization."""

    buy = "BUY"
    sell = "SELL"
    long = "LONG"
    short = "SHORT"
    close = "CLOSE"
    exit = "EXIT"
    alert = "ALERT"


# The platform's own direction vocabulary, which is what `signals.direction`
# stores. CLOSE and EXIT are `flat`: they say "be out", not "sell", and
# turning a close into a sell would open a short on a flat account.
DIRECTION: dict[Action, str] = {
    Action.buy: "buy",
    Action.long: "buy",
    Action.sell: "sell",
    Action.short: "sell",
    Action.close: "flat",
    Action.exit: "flat",
    # A bare notification. It becomes a signal so it is recorded and visible,
    # with a direction that cannot open anything.
    Action.alert: "flat",
}


def parse_action(raw: object) -> Action:
    if not isinstance(raw, str) or not raw.strip():
        raise PayloadError("action is required")
    try:
        return Action(raw.strip().upper())
    except ValueError as exc:
        raise PayloadError(
            f"unsupported action {str(raw)[:32]!r}; expected one of "
            f"{', '.join(a.value for a in Action)}"
        ) from exc


def parse_timestamp(raw: object) -> datetime:
    """An ISO-8601 or epoch stamp, normalized to aware UTC.

    A missing timestamp is refused rather than defaulted to "now": defaulting
    would make every replayed alert look fresh, which is precisely what the
    age check exists to catch.
    """
    if raw is None or raw == "":
        raise PayloadError("time is required; a missing stamp cannot be aged")
    if isinstance(raw, int | float):
        # TradingView's {{timenow}} is milliseconds since the epoch.
        seconds = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if not isinstance(raw, str):
        raise PayloadError("time must be a string or a number")
    text = raw.strip()
    if text.isdigit():
        return parse_timestamp(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError(f"time {text[:32]!r} is not an ISO-8601 timestamp") from exc
    # A naive stamp is read as UTC and said so, rather than as local time: the
    # receiver's timezone is not the alert's, and guessing produces an offset
    # that only shows up on someone else's server.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decimal(raw: object, name: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise PayloadError(f"{name} is not a number") from exc
    if not value.is_finite():
        raise PayloadError(f"{name} is not a finite number")
    return value


def _text(raw: object, name: str, *, required: bool = False) -> str | None:
    if raw is None or raw == "":
        if required:
            raise PayloadError(f"{name} is required")
        return None
    if not isinstance(raw, str):
        raise PayloadError(f"{name} must be a string")
    value = raw.strip()
    if len(value) > MAX_FIELD:
        raise PayloadError(f"{name} is longer than {MAX_FIELD} characters")
    return value


@dataclass(frozen=True)
class Alert:
    """A validated alert. Every field here was checked; none was inferred."""

    ticker: str
    action: Action
    signal_time: datetime
    exchange: str | None = None
    timeframe: str | None = None
    price: Decimal | None = None
    strategy: str | None = None
    strategy_version: str | None = None
    alert_id: str | None = None
    # Recorded, never obeyed. The platform recomputes size and brackets.
    advisory: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def direction(self) -> str:
        return DIRECTION[self.action]

    def fingerprint(self) -> str:
        """A deterministic identity for this alert.

        Used when the sender supplies no id of its own. It hashes the fields
        that make an alert *this* alert -- symbol, action, its own timestamp
        and its strategy -- so TradingView's retry of the same alert collapses
        onto one row, while a genuinely new alert one bar later does not.

        The received time is deliberately excluded: including it would make
        every retry unique, which is the same as having no idempotency at all.
        """
        material = json.dumps(
            {
                "ticker": self.ticker,
                "action": str(self.action),
                "time": self.signal_time.astimezone(UTC).isoformat(),
                "strategy": self.strategy or "",
                "timeframe": self.timeframe or "",
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def idempotency_key(self) -> str:
        """The sender's id when it gave one, otherwise the fingerprint."""
        if self.alert_id:
            return f"tv:id:{self.alert_id}"[:128]
        return f"tv:fp:{self.fingerprint()}"[:128]


# Keys carrying the shared secret. Stripped before anything is stored or
# logged, at any depth. Mirrors `tools/tv_webhook._redact`.
SECRET_KEYS = frozenset({"secret", "passphrase", "password", "token", "api_key", "apikey"})

# Keys whose values the platform records but never acts on.
ADVISORY_KEYS = (
    "quantity",
    "qty",
    "size",
    "lots",
    "volume",
    "stop_loss",
    "sl",
    "take_profit",
    "tp",
    "risk",
)


def parse_alert(payload: dict) -> Alert:
    """A raw alert body -> a validated `Alert`, or a `PayloadError`.

    `ticker` and `action` and `time` are required. Everything else is optional
    because TradingView alert templates vary, and a required field that is
    often absent trains a sender to put a placeholder there.
    """
    if not isinstance(payload, dict):
        raise PayloadError("the alert body must be a JSON object")

    ticker = _text(payload.get("ticker") or payload.get("symbol"), "ticker", required=True)
    assert ticker is not None  # _text raises when required and absent
    action = parse_action(payload.get("action") or payload.get("side"))
    signal_time = parse_timestamp(payload.get("time") or payload.get("timestamp"))

    advisory: dict[str, str] = {}
    for key in ADVISORY_KEYS:
        if payload.get(key) not in (None, ""):
            # Kept as text: it is evidence of what was asked for, not a number
            # anything will compute with.
            advisory[key] = str(payload[key])[:MAX_FIELD]

    known = {
        "ticker",
        "symbol",
        "action",
        "side",
        "time",
        "timestamp",
        "exchange",
        "timeframe",
        "interval",
        "price",
        "close",
        "strategy",
        "strategy_version",
        "id",
        "alert_id",
        "signal_id",
        *ADVISORY_KEYS,
        *SECRET_KEYS,
    }
    extra = {
        str(k)[:MAX_FIELD]: str(v)[:MAX_FIELD]
        for k, v in payload.items()
        if k not in known and k.lower() not in SECRET_KEYS
    }

    return Alert(
        ticker=ticker,
        action=action,
        signal_time=signal_time,
        exchange=_text(payload.get("exchange"), "exchange"),
        timeframe=_text(payload.get("timeframe") or payload.get("interval"), "timeframe"),
        price=_decimal(payload.get("price") or payload.get("close"), "price"),
        strategy=_text(payload.get("strategy"), "strategy"),
        strategy_version=_text(payload.get("strategy_version"), "strategy_version"),
        alert_id=_text(
            payload.get("id") or payload.get("alert_id") or payload.get("signal_id"), "id"
        ),
        advisory=advisory,
        extra=extra,
    )


def check_age(
    signal_time: datetime,
    now: datetime,
    *,
    max_age_seconds: int,
    future_tolerance_seconds: int,
) -> None:
    """Refuse an alert that is too old or stamped ahead.

    A stale alert is not a late alert -- the bar it referred to has closed and
    the price it named is gone, so acting on it is acting on a market that no
    longer exists. Executing one is how a webhook retry storm becomes a
    position.
    """
    age = (now - signal_time).total_seconds()
    if age > max_age_seconds:
        raise PayloadError(
            f"alert is {int(age)}s old; the maximum accepted age is {max_age_seconds}s"
        )
    if -age > future_tolerance_seconds:
        raise PayloadError(f"alert is stamped {int(-age)}s in the future; check the sender's clock")


def redact(value: object, secret: str = "") -> object:
    """Strip the shared secret from keys and from string values, at any depth.

    Stripping the `secret` key alone is not enough: a plain-text alert carries
    the secret inline in its message, and a nested object can carry it at any
    depth. This is `tools/tv_webhook._redact`'s rule, applied before anything
    reaches the database or a log line.
    """
    if isinstance(value, dict):
        return {k: redact(v, secret) for k, v in value.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, str) and secret and secret in value:
        return value.replace(secret, "[redacted]").strip()
    return value


def within(now: datetime, seconds: int) -> datetime:
    return now - timedelta(seconds=seconds)
