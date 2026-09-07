"""What a user has asked to be told, and the floor they cannot go below.

Sections 15, 16 and 17.

**Three layers, resolved in this order.** The default for a (category,
channel) pair; the user's stored row for it, if there is one; then the floor.
An absent row is not "off" -- it means the user has never expressed an opinion,
and the default is what a sensible platform does in the absence of one.

**The floor is the point of section 16.** A user may quieten what they are
told about; they may not switch off being told that a risk limit broke. ERROR
and CRITICAL always reach the in-app centre, whatever the stored row says, and
`resolve` enforces that even against a row written directly into the database.
The endpoint refuses such a row on the way in *as well*, so a user gets an
explanation rather than a setting that silently does not take effect.

**Section 17, and it is worth being blunt about it.** A preference decides
whether somebody is *told*. It decides nothing else. The RiskEngine's limits,
the kill switch, order execution, position exits and broker reconciliation do
not consult this module and cannot: nothing in `app/notifications` imports
them, and a test parses the package to keep it that way. Switching off risk
email switches off the email.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.notifications.contract import (
    UNSUPPRESSIBLE,
    Category,
    Channel,
    Severity,
    at_least,
)


class PreferenceError(ValueError):
    """A preference that cannot be stored, with the reason. Never clamped silently."""


@dataclass(frozen=True)
class Setting:
    """One resolved (category, channel) decision."""

    enabled: bool
    min_severity: Severity

    def wants(self, severity: Severity) -> bool:
        return self.enabled and at_least(severity, self.min_severity)


#: The highest floor the in-app channel may be given. Section 16: a floor of
#: CRITICAL would suppress ERROR, and an unread risk breach is the failure the
#: notification centre exists to prevent.
IN_APP_MAX_FLOOR = Severity.error


def _defaults() -> dict[tuple[Category, Channel], Setting]:
    """Section 16. Safe, quiet, and never silently dropping a critical event.

    In-app takes everything: it is a list in the platform, it costs nothing to
    receive, and a user who wants less can raise the floor.

    Email defaults to CRITICAL only, so a fresh account is not noisy -- except
    for the three categories where an ERROR is already something somebody has
    to go and look at. Risk, broker and security are that; a rejected order or
    a drifting model is not, and can be turned up by anyone who wants it.

    Discord defaults to off everywhere. Its adapter arrives at L35 and its
    configuration is somebody's deliberate act; defaulting a channel on before
    it is configured would mean a delivery row per event that nothing can send.
    """
    out: dict[tuple[Category, Channel], Setting] = {}
    for category in Category:
        out[(category, Channel.in_app)] = Setting(True, Severity.info)
        out[(category, Channel.email)] = Setting(True, Severity.critical)
        out[(category, Channel.discord)] = Setting(False, Severity.warning)
    for category in (Category.risk, Category.broker, Category.security):
        out[(category, Channel.email)] = Setting(True, Severity.error)
    return out


DEFAULTS: dict[tuple[Category, Channel], Setting] = _defaults()


def default_for(category: Category, channel: Channel) -> Setting:
    return DEFAULTS[(category, channel)]


def validate(
    category: Category,
    channel: Channel,
    *,
    enabled: bool,
    min_severity: Severity,
) -> Setting:
    """The setting as it will be stored, or `PreferenceError` saying why not.

    Refusing rather than clamping is deliberate. A clamped setting is one the
    user believes they made and the platform quietly ignored, which is worse
    than a rejection: they would find out from the notification they were
    trying to silence still arriving.
    """
    if channel is Channel.in_app:
        if not enabled:
            raise PreferenceError(
                "in-app notifications cannot be switched off. They are the platform's own "
                "record, and an unread risk breach is what section 16 exists to prevent. "
                "Raise the severity floor instead."
            )
        if at_least(min_severity, Severity.critical) and IN_APP_MAX_FLOOR is not Severity.critical:
            raise PreferenceError(
                f"the in-app floor may be raised to at most {IN_APP_MAX_FLOOR}. "
                f"{', '.join(sorted(str(s) for s in UNSUPPRESSIBLE))} always reach the "
                "notification centre."
            )
    return Setting(enabled, min_severity)


def resolve(
    category: Category,
    severity: Severity,
    stored: dict[tuple[Category, Channel], Setting],
    *,
    available: Iterable[Channel] | None = None,
) -> list[Channel]:
    """Which channels this notification goes to, in a stable order.

    `available` is the set of channels whose adapter is actually configured.
    A channel that is not configured is left out here rather than being given a
    delivery row that can only fail -- the row would say FAILED, which reads as
    "we tried and the provider broke" and is a different claim from "nobody
    ever set this up". `service.py` records the SKIPPED row that says the
    second thing.

    The floor is applied last and unconditionally, so a stored row that got
    into the database by some other route still cannot hide an ERROR.
    """
    usable = set(available) if available is not None else set(Channel)
    out: list[Channel] = []
    for channel in Channel:  # enum order: IN_APP, EMAIL, DISCORD
        setting = stored.get((category, channel)) or default_for(category, channel)
        wanted = setting.wants(severity)
        if channel is Channel.in_app and severity in UNSUPPRESSIBLE:
            wanted = True
        if not wanted:
            continue
        if channel not in usable:
            continue
        out.append(channel)
    return out


def as_rows(stored: dict[tuple[Category, Channel], Setting]) -> list[dict[str, object]]:
    """Every pair, with whether the value is the user's or the default.

    The full grid is returned rather than only stored rows: a settings page
    that showed only what had been saved would show an empty page to every new
    user, and "no preference" would be indistinguishable from "no such
    setting".
    """
    rows: list[dict[str, object]] = []
    for category in Category:
        for channel in Channel:
            setting = stored.get((category, channel))
            effective = setting or default_for(category, channel)
            rows.append(
                {
                    "category": str(category),
                    "channel": str(channel),
                    "enabled": effective.enabled,
                    "min_severity": str(effective.min_severity),
                    "source": "user" if setting else "default",
                    "locked": channel is Channel.in_app,
                }
            )
    return rows


__all__ = [
    "DEFAULTS",
    "IN_APP_MAX_FLOOR",
    "PreferenceError",
    "Setting",
    "as_rows",
    "default_for",
    "resolve",
    "validate",
]
