"""The email channel, and the provider port behind it.

Sections 25 and 26.

**There was no email integration to reuse.** The audit found one thing close
to it: `app.auth.reset.ResetDelivery`, a Protocol whose only implementation
refuses, with a docstring saying delivery "needs the notification engine
(level 34)". That port is the shape this module fills, and `SMTPEmailProvider`
is what finally lets a password reset be delivered -- see
`reset_delivery_from` at the end of this file.

**Provider behind an abstraction, per section 25.** `EmailProvider` is the
port; `SMTPEmailProvider` is the one implementation. Nothing above this file
knows what SMTP is, so a hosted provider added later replaces one class.

**Why stdlib `smtplib` in a thread rather than an async SMTP client.** The
backend's runtime dependencies are deliberately short and each one is argued
for in `requirements.txt`. One blocking call per email, dispatched with
`asyncio.to_thread` and bounded by a timeout, is not worth a dependency: the
delivery worker is already off the request path, and email volume here is
notifications, not a mailing list.

**Failures are classified, not counted.** Section 30 asks to tell a temporary
provider failure from an invalid configuration, and `smtplib` gives enough to
do it honestly: a 4xx and a socket error are retryable, a 5xx and an
authentication refusal are not. Retrying a bad password every thirty seconds
until the end of time is how a queue stops draining -- and how an account gets
locked.

**No secret is logged, returned or put in a delivery row.** The provider holds
the password inside itself; `describe()` reports host and port and whether
credentials are set, never what they are. The failure reason stored on a
delivery is `smtplib`'s status line, which carries no credential.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

from app.notifications.channels.base import ChannelState
from app.notifications.contract import (
    Channel,
    DeliveryResult,
    DeliveryStatus,
    NotificationEnvelope,
    Severity,
)

log = logging.getLogger("app.notifications.email")

DEFAULT_TIMEOUT = 15.0


class EmailError(Exception):
    """A send that did not happen, with whether trying again could help."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmailProvider(Protocol):
    """Section 25's abstraction. One method, no provider vocabulary in it."""

    name: str

    @property
    def configured(self) -> bool: ...

    async def send(self, *, to: str, subject: str, text: str) -> str | None:
        """Returns a provider message id when there is one. Raises `EmailError`."""
        ...

    def describe(self) -> dict[str, Any]: ...


@dataclass
class UnconfiguredEmailProvider:
    """No SMTP host is set. It refuses rather than pretending.

    The same shape as `app.auth.reset.UnconfiguredDelivery`, and for the same
    reason: a platform with no mail server is a correctly configured platform
    that does not send mail, and saying so is more useful than a silent
    success.
    """

    name: str = "unconfigured"

    @property
    def configured(self) -> bool:
        return False

    async def send(self, *, to: str, subject: str, text: str) -> str | None:
        raise EmailError("no SMTP host is configured", retryable=False)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": False,
            "detail": "set SMTP_HOST to enable email delivery",
        }


@dataclass
class SMTPEmailProvider:
    """One SMTP server. Blocking, run in a thread, bounded by a timeout."""

    host: str
    port: int = 587
    username: str | None = None
    #: Held here and nowhere else. Never logged, never returned by `describe`,
    #: never placed on a delivery row.
    password: str | None = None
    use_tls: bool = True
    sender: str = "notifications@localhost"
    timeout: float = DEFAULT_TIMEOUT
    name: str = "smtp"

    @property
    def configured(self) -> bool:
        return bool(self.host)

    async def send(self, *, to: str, subject: str, text: str) -> str | None:
        if not self.configured:
            raise EmailError("no SMTP host is configured", retryable=False)
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        try:
            await asyncio.to_thread(self._deliver, message)
        except EmailError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
            raise EmailError(f"{type(exc).__name__}: {exc}"[:300], retryable=True) from exc
        # SMTP does not return a message id. None is the honest answer; an
        # invented one would be traced to a message that does not have it.
        return None

    def _deliver(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            # Configuration, not weather. Section 30: retrying this forever
            # locks the account it is failing to authenticate against.
            raise EmailError(
                f"SMTP authentication refused ({exc.smtp_code})", retryable=False
            ) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailError("the recipient address was refused", retryable=False) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise EmailError(
                f"the sender address was refused ({exc.smtp_code})", retryable=False
            ) from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx is a temporary failure by definition; 5xx is permanent.
            retryable = 400 <= int(exc.smtp_code) < 500
            raise EmailError(f"SMTP {exc.smtp_code}", retryable=retryable) from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise EmailError(f"{type(exc).__name__}: {exc}"[:200], retryable=True) from exc

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured,
            "host": self.host,
            "port": self.port,
            # Whether, not what. Section 57 of L36 in advance: status only.
            "authenticated": bool(self.username and self.password),
            "tls": self.use_tls,
        }


class EmailChannel:
    """Section 25. Renders the envelope as text and hands it to the provider.

    **The address is resolved by the caller, not here.** `service.py` looks up
    the recipient's email when it queues the delivery and puts it on the
    envelope's context; this adapter never queries a user table, which is what
    keeps it free of a session.
    """

    channel = Channel.email

    def __init__(self, provider: EmailProvider, *, enabled: bool = True) -> None:
        self.provider = provider
        self.enabled = enabled

    @property
    def available(self) -> bool:
        return self.enabled and self.provider.configured

    async def send(self, envelope: NotificationEnvelope) -> DeliveryResult:
        address = envelope.context.get("recipient_email")
        if not isinstance(address, str) or "@" not in address:
            return DeliveryResult(
                status=DeliveryStatus.skipped,
                detail="the recipient has no email address on file",
                retryable=False,
            )
        if not self.available:
            return DeliveryResult(
                status=DeliveryStatus.skipped,
                detail="email is not configured on this deployment",
                retryable=False,
            )
        try:
            message_id = await self.provider.send(
                to=address, subject=subject_for(envelope), text=body_for(envelope)
            )
        except EmailError as exc:
            return DeliveryResult(
                status=DeliveryStatus.retrying if exc.retryable else DeliveryStatus.failed,
                detail=str(exc),
                retryable=exc.retryable,
            )
        return DeliveryResult(status=DeliveryStatus.delivered, provider_message_id=message_id)

    def describe(self) -> dict[str, Any]:
        return {
            "channel": str(self.channel),
            "state": (
                ChannelState.configured
                if self.available
                else (ChannelState.disabled if not self.enabled else ChannelState.not_configured)
            ),
            "available": self.available,
            **self.provider.describe(),
        }


def subject_for(envelope: NotificationEnvelope) -> str:
    """The title, already stamped with the environment by `templates.stamp`.

    Severity is prefixed only above WARNING, so an inbox is not shouted at by
    every informational line.
    """
    if envelope.severity in (Severity.error, Severity.critical):
        return f"[{envelope.severity}] {envelope.title}"
    return envelope.title


def body_for(envelope: NotificationEnvelope) -> str:
    """Plain text. Every line is a recorded fact; nothing is computed here.

    No link is included unless a base URL is configured and reaches this
    function on the context -- section 31 of L35: never a hard-coded
    `localhost`, and never a URL carrying a token.
    """
    lines = [envelope.title, "", envelope.body, ""]
    if envelope.environment:
        lines.append(f"Environment: {envelope.environment.upper()}")
    lines.append(f"Severity: {envelope.severity}")
    if envelope.entity_type and envelope.entity_id:
        lines.append(f"Reference: {envelope.entity_type} {envelope.entity_id}")
    link = envelope.context.get("link")
    if isinstance(link, str) and link:
        lines += ["", f"Open in the platform: {link}"]
    lines += [
        "",
        "You are receiving this because of your notification preferences. "
        "They can be changed in the platform under Settings.",
    ]
    return "\n".join(lines)


def provider_from(settings: Any) -> EmailProvider:
    """The provider this deployment has, from configuration alone."""
    host = getattr(settings, "smtp_host", "") or ""
    if not host:
        return UnconfiguredEmailProvider()
    return SMTPEmailProvider(
        host=host,
        port=int(getattr(settings, "smtp_port", 587) or 587),
        username=getattr(settings, "smtp_username", "") or None,
        password=getattr(settings, "smtp_password", "") or None,
        use_tls=bool(getattr(settings, "smtp_use_tls", True)),
        sender=getattr(settings, "smtp_from", "") or "notifications@localhost",
    )


class NotificationResetDelivery:
    """`app.auth.reset.ResetDelivery`, finally implemented.

    L04 wrote the port and said its implementation "needs the notification
    engine (level 34)". This is it. The token is placed in the message body and
    nowhere else -- not in a log line, not in a delivery row, not in the audit
    trail -- which is the property `app.auth.reset` spends its docstring on.

    An unconfigured provider raises `DeliveryUnavailable` exactly as
    `UnconfiguredDelivery` did, so a deployment with no SMTP host behaves as it
    did before this class existed.
    """

    def __init__(self, provider: EmailProvider, *, base_url: str = "") -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.name = f"email:{provider.name}"

    async def send(self, email: str, token: str) -> None:
        from app.auth.reset import DeliveryUnavailable

        if not self.provider.configured:
            raise DeliveryUnavailable(
                "no delivery channel is configured; set SMTP_HOST to deliver password resets"
            )
        where = f"{self.base_url}/reset-password?token={token}" if self.base_url else None
        text = "\n".join(
            [
                "A password reset was requested for this address.",
                "",
                f"Reset link: {where}" if where else f"Reset token: {token}",
                "",
                "The link is single-use and expires within the hour. If you did not "
                "request it, no action is needed and the token can be ignored.",
            ]
        )
        try:
            await self.provider.send(to=email, subject="Password reset", text=text)
        except EmailError as exc:
            # Logged without the token, because a logged token is a token in a
            # log file -- `app.auth.reset` says so and means it.
            log.warning(
                "a password reset email could not be sent",
                extra={"event": "reset_email_failed", "provider": self.provider.name},
            )
            raise DeliveryUnavailable(f"password reset delivery failed: {exc}") from exc


__all__ = [
    "DEFAULT_TIMEOUT",
    "EmailChannel",
    "EmailError",
    "EmailProvider",
    "NotificationResetDelivery",
    "SMTPEmailProvider",
    "UnconfiguredEmailProvider",
    "body_for",
    "provider_from",
    "subject_for",
]
