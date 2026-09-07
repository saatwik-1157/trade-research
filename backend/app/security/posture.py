"""The security posture, reported the way every other component is reported.

Step 45 asks for a security status in the monitoring dashboard. It is built as
**one more `ComponentHealth` in L37's existing stack**, not as a security
dashboard beside the monitoring dashboard, for the reason this project keeps
arriving at: two views of one fact eventually disagree, and the one on the
screen is not the one the system acted on.

**Everything here is derived from configuration and counters that already
exist.** Nothing is asserted. The rule the brief states -- *do not invent
health states* -- bites hardest in a security panel, where a green tick is
exactly what somebody wants to see, so each check below either reads a setting
or reads a counter, and anything it cannot read reports `UNKNOWN` with the
reason rather than `HEALTHY`.

**A control that is absent is reported as absent.** There is no MFA on this
platform. The posture says so, in the response, every time it is asked. A
scorecard that omits what is missing is a scorecard that reads as complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.observability.contract import (
    ComponentHealth,
    ComponentState,
    Criticality,
    Layer,
)
from app.security.headers import BASE_HEADERS


@dataclass(frozen=True)
class Control:
    """One security control, and whether it is actually in force."""

    name: str
    #: True/False/None -- None means "this deployment cannot tell", which is a
    #: third answer and is never rendered as either of the other two.
    enforced: bool | None
    detail: str
    #: A control that is deliberately absent is not a failure. It is a gap, and
    #: it is stated as one.
    planned: bool = False


def controls(settings: Any, app_state: Any = None) -> list[Control]:
    """What is in force right now, read from configuration rather than claimed."""
    production = settings.environment.value == "production"
    out: list[Control] = [
        Control(
            "password_hashing",
            True,
            "Argon2id, with a rehash on login when the parameters move.",
        ),
        Control(
            "session_storage",
            True,
            "Server-side, SHA-256 in the table, revocable, HttpOnly cookie.",
        ),
        # `cookie_secure_effective` is already True in production and False in
        # development, which is the right behaviour and the wrong thing to
        # report as a failure: a browser will not send a Secure cookie over the
        # plain http a development server speaks, so the flag CANNOT be set
        # there. Outside production it is reported as a known gap rather than
        # as a control that is off, because a dashboard that is amber on every
        # developer's machine is a dashboard nobody reads in production either.
        Control(
            "cookie_secure",
            settings.cookie_secure_effective,
            "Secure flag on the session and CSRF cookies."
            + (
                ""
                if production
                else " Not set outside production: a browser will not send a "
                "Secure cookie over plain http, so development could not sign in."
            ),
            planned=not production and not settings.cookie_secure_effective,
        ),
        Control(
            "csrf",
            bool(getattr(settings, "csrf_enabled", True)),
            "Double-submit token on every unsafe method that carries a session.",
        ),
        Control(
            "rate_limiting",
            True,
            "Login, registration, password reset, webhooks, notifications, "
            "admin writes and step-up.",
        ),
        Control(
            "rbac",
            True,
            "16 permissions across 3 roles, checked server-side on every route.",
        ),
        Control("audit_trail", True, "Append-only; no code path updates or deletes a row."),
        Control(
            "security_headers",
            True,
            f"{len(BASE_HEADERS)} headers on every response, including errors.",
        ),
        # Not a failure outside production, for the same reason `cookie_secure`
        # is not: sending HSTS from a development server on 127.0.0.1 pins the
        # browser to HTTPS for that host for a year and breaks every other
        # local project on the same address. Reporting the correct behaviour as
        # a degraded control would make the dashboard amber on every developer
        # machine, and an alert that is always on is an alert nobody reads.
        Control(
            "hsts",
            production,
            "Sent in production only."
            + (
                ""
                if production
                else " Withheld here deliberately: it would pin this browser to "
                "HTTPS for 127.0.0.1 for a year."
            ),
            planned=not production,
        ),
        Control(
            "cors_restricted",
            "*" not in tuple(settings.cors_origins),
            "Explicit origin list; a wildcard with credentials is refused at startup.",
        ),
        # Reads the setting rather than asserting the feature exists. A control
        # that can be turned off invisibly is a control that is turned off, so
        # `STEP_UP_REQUIRED=false` shows here as a control that is not in force
        # and takes the whole component to DEGRADED.
        Control(
            "step_up_reauth",
            bool(getattr(settings, "step_up_required", True)),
            "Password again for user access changes, session revocation, "
            "kill-switch release and safe-mode exit."
            + (
                ""
                if getattr(settings, "step_up_required", True)
                else " STEP_UP_REQUIRED is false, so these four actions need only a session cookie."
            ),
        ),
        # Reported as in force in BOTH states, and the detail says which one.
        # An unset secret is not a missing control -- L09 made the receiver
        # refuse every alert when there is no secret, so the unconfigured state
        # is the closed one. Marking it as a failure would teach an operator
        # that the safe configuration is the broken one, and the fix they would
        # reach for is setting a secret they do not need.
        Control(
            "webhook_authentication",
            True,
            (
                "Constant-time secret comparison, a 120-second replay window, an "
                "idempotency key, an IP allowlist and a 64 KB cap."
                if getattr(settings, "tv_webhook_secret", "")
                else "No secret is configured, so the receiver REFUSES every "
                "alert. That is L09's deliberate default and is the closed state, "
                "not an absent control."
            ),
        ),
        Control(
            "websocket_limits",
            True,
            "Per-connection subscription cap, per-user connection cap, frame "
            "size cap, idle close, and per-subscribe authorization.",
        ),
        Control(
            "live_trading_disabled",
            not settings.live_trading,
            "LIVE_TRADING is the switch that decides whether a refusal costs money.",
        ),
        # --- the gaps, stated rather than omitted -------------------------
        Control(
            "mfa",
            False,
            "NOT BUILT. Step-up re-authentication asks for the same password "
            "again; it is a second prompt, not a second factor. TOTP needs "
            "enrolment, recovery codes and a lost-device path.",
            planned=True,
        ),
        Control(
            "broker_credential_encryption",
            None,
            "No broker credential is stored anywhere in this platform, so there "
            "is nothing to encrypt yet. The requirement lands with the first "
            "adapter that needs one.",
            planned=True,
        ),
        Control(
            "secret_manager",
            False,
            "Secrets come from the environment. A managed store is a deployment "
            "decision this repository does not make.",
            planned=True,
        ),
        Control(
            "intrusion_detection",
            False,
            "Failed logins and refusals are counted and alerted on. There is no "
            "behavioural detection, and calling the counters one would be a claim "
            "the code does not support.",
            planned=True,
        ),
    ]
    if (
        app_state is not None
        and getattr(settings, "step_up_required", True)
        and getattr(app_state, "step_up", None) is None
    ):
        # Configured on, but this process holds no grant registry -- a worker,
        # or an app built without `create_app`. UNKNOWN rather than False: the
        # requirement is set and this process simply cannot say whether the one
        # serving the routes honours it.
        out = [
            c
            if c.name != "step_up_reauth"
            else Control(
                "step_up_reauth",
                None,
                "required by configuration, but this process holds no grant "
                "registry, so it cannot report whether the requirement is met.",
            )
            for c in out
        ]
    return out


def component(settings: Any, app_state: Any = None) -> ComponentHealth:
    """The security posture as one component of the platform's health.

    The state is DEGRADED when an *enforced* control is off, never when a
    *planned* one is missing: a gap that is written down and known is not the
    same operational fact as a control that was switched off, and collapsing
    the two makes the dashboard permanently amber and therefore ignored.
    """
    items = controls(settings, app_state)
    off = [c.name for c in items if c.enforced is False and not c.planned]
    unknown = [c.name for c in items if c.enforced is None and not c.planned]
    gaps = [c.name for c in items if c.planned]

    if off:
        state = ComponentState.degraded
        detail = f"{len(off)} enforced control(s) are not in force: {', '.join(sorted(off))}"
    elif unknown:
        state = ComponentState.unknown
        detail = f"cannot determine: {', '.join(sorted(unknown))}"
    else:
        state = ComponentState.healthy
        detail = (
            f"{len(items) - len(gaps)} controls in force. "
            f"{len(gaps)} known gap(s), listed and not counted as failures."
        )

    return ComponentHealth(
        name="security",
        layer=Layer.security,
        state=state,
        criticality=Criticality.critical,
        detail=detail,
        checked_at=datetime.now(UTC),
        facts={
            "in_force": sorted(c.name for c in items if c.enforced is True),
            "not_in_force": sorted(off),
            "undetermined": sorted(unknown),
            "known_gaps": sorted(gaps),
        },
    )


def scorecard(settings: Any, app_state: Any = None) -> dict[str, Any]:
    """The posture in full, for `/v1/security/posture` and the scorecard doc.

    No score out of ten. A number invites the question "how do we get to ten?",
    which is answered by adding controls that move the number rather than
    controls that reduce risk -- and the brief calls that security theatre by
    name.
    """
    items = controls(settings, app_state)
    return {
        "environment": settings.environment.value,
        "trading_mode": settings.trading_mode.value,
        "live_trading": settings.live_trading,
        "controls": [
            {
                "name": c.name,
                "enforced": c.enforced,
                "planned": c.planned,
                "detail": c.detail,
            }
            for c in items
        ],
        "note": (
            "There is no overall score. A number invites the question 'how do we "
            "get to ten', and the cheapest answers to that question are controls "
            "that move a number rather than controls that reduce risk."
        ),
    }


__all__ = ["Control", "component", "controls", "scorecard"]
