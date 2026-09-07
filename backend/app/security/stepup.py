"""Step-up re-authentication for the actions a stolen cookie must not reach.

Steps 5, 6 and 30, and the gap L36 and L38 both named in their own documents:
*"there is no multi-factor authentication and no step-up re-authentication
anywhere in this platform, and the administrative session that releases safe
mode is a password and a cookie."*

**What this is not.** It is not TOTP, and calling it MFA would be the security
theatre the brief forbids. A second factor needs an enrolment flow, recovery
codes, a device-binding story and a way to help somebody who has lost their
phone -- half-building one and putting "MFA" on a checklist produces a control
that is bypassed the first time it is inconvenient. `SECURITY.md`
names TOTP as the next step and says plainly what is missing.

**What it is.** A dangerous action requires the password *again*, at the moment
it is taken, and the resulting grant is narrow, short and spent by one use:

  * **Scoped.** A grant for `admin_user_access` does not release safe mode.
    A single "you are re-authenticated" flag would mean one password prompt
    unlocking every dangerous action on the platform for its lifetime.
  * **Short.** `TTL_SECONDS = 300`. Long enough to type a reason, too short to
    still be open when the laptop is left unlocked.
  * **Single-use.** `consume()` removes it. A grant that survives its action is
    a second unreviewed action; dangerous actions are rare, so the cost of
    asking again is small and the benefit is that no grant is ever spent twice.
  * **Bound to the session.** Keyed on the session id, so a grant obtained in
    one browser cannot be presented from another.
  * **Bound to the subject.** The action's target is part of the key, so a
    grant taken out against one user cannot deactivate a different one.

**This is a second gate, never a replacement for the first.** Every permission
check, every CSRF check and every confirmation phrase still runs. Step-up adds
a refusal; it never approves anything that was going to be refused. That is the
same rule L38 wrote for safe mode, and it is the rule that makes it safe to add
a gate to a system that already has several.

**The grants live in the process, deliberately.** The same argument L38 made
for the safe-mode latch: a grant persisted in a table is one that survives the
process that issued it, and a five-minute credential surviving a restart is a
credential nobody revoked. One API process holds them today; a second process
needs a shared store, which is a row and a lease rather than a dict, and
`SECURITY.md` records that as a known limit rather than leaving it
to be discovered.

**Failure is quiet to the caller and loud in the log.** A wrong password
returns the same refusal as a missing grant, so the endpoint cannot be used to
tell "wrong password" from "no grant" -- and both emit a security event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.security.events import SecurityEvent, SecuritySeverity, log_record, record

#: Five minutes. A grant is a credential; it should not outlive the tab.
TTL_SECONDS = 300.0

#: A ceiling on what one process will hold, so a loop that requests grants
#: cannot grow the dict without bound. Expired entries are swept first, so
#: reaching this means genuinely many live grants, which is itself odd.
MAX_GRANTS = 512

#: Failed step-up attempts before the scope is locked for this session. The
#: step-up endpoint verifies a password, which makes it a password oracle if
#: it is not counted; the login limiter does not cover it because it is a
#: different route.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 900.0


class StepUpScope(StrEnum):
    """The action classes that require the password again.

    Each names something that either moves money, removes somebody's access,
    or lifts a safety control. Read-only surfaces are deliberately absent: a
    control that fires on ordinary work is one people learn to click through.
    """

    admin_user_access = "ADMIN_USER_ACCESS"  # deactivate, reactivate, change role
    admin_session_revoke = "ADMIN_SESSION_REVOKE"  # sign somebody else out
    safe_mode_exit = "SAFE_MODE_EXIT"  # resume trading after a recovery latch
    kill_switch = "KILL_SWITCH"  # release the trading halt
    broker_credentials = "BROKER_CREDENTIALS"  # a seat; see the architecture doc
    #: L45 F-1. Letting a bot take its stop and target from the ALERT rather
    #: than from the platform. It belongs here under "lifts a safety control":
    #: the bracket an alert suggests is quarantined on purpose, and under
    #: `fixed_risk` sizing a tighter stop produces a LARGER position -- so this
    #: hands an external sender an input that moves size upward.
    bot_bracket_source = "BOT_BRACKET_SOURCE"


class StepUpError(Exception):
    """Refused. The message is safe to show and names no secret."""


@dataclass(frozen=True)
class Grant:
    session: str
    scope: StepUpScope
    subject: str
    expires_at: float

    def alive(self, now: float) -> bool:
        return now < self.expires_at


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


@dataclass
class StepUp:
    """The grant registry. One per process; see the module docstring."""

    ttl_seconds: float = TTL_SECONDS
    _grants: dict[tuple[str, StepUpScope, str], Grant] = field(default_factory=dict)
    _attempts: dict[tuple[str, StepUpScope], _Attempts] = field(default_factory=dict)
    _now: object = None  # injectable clock for tests

    def now(self) -> float:
        return self._now() if callable(self._now) else time.monotonic()

    # -- issuing ---------------------------------------------------------

    def grant(
        self,
        *,
        session: str,
        scope: StepUpScope,
        subject: str,
        password_ok: bool,
        user_id: str | None = None,
    ) -> Grant:
        """Verify and issue. `password_ok` is decided by the caller.

        The password itself never reaches this module -- the caller runs
        `verify_password` against the stored Argon2 hash and passes the
        boolean. Nothing here can log, store or compare a credential, which is
        a property of the signature rather than of anybody's discipline.
        """
        now = self.now()
        key = (session, scope)
        attempts = self._attempts.get(key)
        if attempts is not None and now < attempts.locked_until:
            remaining = int(attempts.locked_until - now)
            rec = record(
                SecurityEvent.step_up_failed,
                f"step-up for {scope.value} is locked for another {remaining}s",
                user_id=user_id,
                scope=scope.value,
                outcome="locked",
                severity=SecuritySeverity.error,
            )
            log_record(rec)
            raise StepUpError(
                "too many failed confirmations. Re-authentication for this action is "
                f"locked for another {remaining} seconds."
            )

        if not password_ok:
            attempts = attempts or _Attempts()
            attempts.count += 1
            if attempts.count >= MAX_ATTEMPTS:
                attempts.locked_until = now + LOCKOUT_SECONDS
                attempts.count = 0
            self._attempts[key] = attempts
            rec = record(
                SecurityEvent.step_up_failed,
                f"a confirmation for {scope.value} did not match",
                user_id=user_id,
                scope=scope.value,
                outcome="refused",
            )
            log_record(rec)
            # Deliberately the same wording a missing grant produces.
            raise StepUpError(
                "that did not confirm. Re-enter the password for the signed-in account."
            )

        self._attempts.pop(key, None)
        self._sweep(now)
        if len(self._grants) >= MAX_GRANTS:
            raise StepUpError("too many pending confirmations on this server. Try again shortly.")
        issued = Grant(
            session=session,
            scope=scope,
            subject=subject,
            expires_at=now + self.ttl_seconds,
        )
        self._grants[(session, scope, subject)] = issued
        log_record(
            record(
                SecurityEvent.step_up_granted,
                f"re-authentication accepted for {scope.value}",
                user_id=user_id,
                scope=scope.value,
                outcome="granted",
            )
        )
        return issued

    # -- spending --------------------------------------------------------

    def consume(
        self,
        *,
        session: str,
        scope: StepUpScope,
        subject: str,
        user_id: str | None = None,
    ) -> None:
        """Spend the grant, or refuse. Single-use: it is removed either way.

        Removed on expiry too, so a stale grant cannot sit in the dict being
        checked and rejected forever.
        """
        now = self.now()
        key = (session, scope, subject)
        held = self._grants.pop(key, None)
        if held is None or not held.alive(now):
            log_record(
                record(
                    SecurityEvent.step_up_missing,
                    f"{scope.value} was attempted without a live confirmation",
                    user_id=user_id,
                    scope=scope.value,
                    outcome="refused",
                )
            )
            raise StepUpError(
                "this action needs the account password again. Confirm at "
                "POST /v1/security/step-up and repeat the request within "
                f"{int(self.ttl_seconds)} seconds."
            )

    def holds(self, *, session: str, scope: StepUpScope, subject: str) -> bool:
        """Whether a grant is live. Does NOT spend it. For status only."""
        held = self._grants.get((session, scope, subject))
        return held is not None and held.alive(self.now())

    # -- housekeeping ----------------------------------------------------

    def _sweep(self, now: float) -> None:
        for key, held in list(self._grants.items()):
            if not held.alive(now):
                del self._grants[key]
        for key2, attempts in list(self._attempts.items()):
            if attempts.locked_until and now >= attempts.locked_until and not attempts.count:
                del self._attempts[key2]

    def stats(self) -> dict[str, object]:
        """Counts only. No session ids, no subjects, no scopes per session."""
        now = self.now()
        self._sweep(now)
        return {
            "live_grants": len(self._grants),
            "locked_scopes": sum(1 for a in self._attempts.values() if now < a.locked_until),
            "ttl_seconds": int(self.ttl_seconds),
            "single_use": True,
            "scopes": [s.value for s in StepUpScope],
        }


__all__ = [
    "LOCKOUT_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_GRANTS",
    "TTL_SECONDS",
    "Grant",
    "StepUp",
    "StepUpError",
    "StepUpScope",
]
