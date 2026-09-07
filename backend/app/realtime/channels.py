"""Channels, and who may subscribe to them.

A channel is `scope:id`, or the bare word `system`. Parsing is strict and
authorization is explicit, because the failure this module exists to prevent
is the cheapest attack there is: change the id in the subscribe frame and
receive somebody else's account.

The rule that shapes it: **authorization asks the database, never the frame.**
A client says "subscribe to account:abc"; the server asks whether that account
belongs to this user. Nothing is inferred from the fact that the client knew
the id -- ids appear in URLs, logs and screenshots, and knowing one is not
owning one.

`system` is the one channel every signed-in user may join, and the catalogue
enforces what may travel on it: `SYSTEM_ALERT` only, whose payload is a
platform notice. No account figure is ever published there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Role, User, role_at_least
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.bots import Bot
from app.realtime.catalogue import Scope

# An id segment. Deliberately narrow: these are UUIDs, symbol codes and
# strategy keys, none of which needs a colon, a wildcard or whitespace.
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

MAX_SUBSCRIPTIONS = 50


class ChannelError(Exception):
    """A channel that cannot be parsed. Never resolved to a default."""


class NotAuthorized(Exception):
    """This user may not subscribe here. Never downgraded to an empty stream."""


@dataclass(frozen=True)
class Channel:
    scope: Scope
    ref: str | None = None

    def __str__(self) -> str:
        return str(self.scope) if self.ref is None else f"{self.scope}:{self.ref}"


SYSTEM = Channel(Scope.system)


def parse(raw: str) -> Channel:
    """`"account:abc"` -> Channel. Raises rather than guessing."""
    if not isinstance(raw, str):
        raise ChannelError("channel must be a string")
    value = raw.strip()
    if not value:
        raise ChannelError("channel is empty")
    if len(value) > 128:
        raise ChannelError("channel is longer than 128 characters")
    if value == str(Scope.system):
        return SYSTEM
    head, sep, ref = value.partition(":")
    if not sep:
        raise ChannelError(f"channel {value!r} needs the form scope:id")
    try:
        scope = Scope(head)
    except ValueError as exc:
        raise ChannelError(
            f"unknown channel scope {head!r}; expected one of {', '.join(str(s) for s in Scope)}"
        ) from exc
    if scope is Scope.system:
        raise ChannelError("the system channel takes no id")
    if not _ID.match(ref):
        raise ChannelError(f"channel id {ref[:32]!r} is not a valid identifier")
    return Channel(scope, ref)


def channel_for_event(event_type: str, ref: str | None) -> Channel:
    """Where an event of this type must be published.

    The catalogue decides, not the caller. A private fill cannot be routed to
    `system` by a publisher that passed the wrong channel, because the
    publisher does not choose the scope.
    """
    from app.realtime.catalogue import scope_of

    scope = scope_of(event_type)
    if scope is Scope.system:
        return SYSTEM
    if not ref:
        raise ChannelError(f"{event_type} is scoped to {scope} and needs a reference id")
    return Channel(scope, ref)


async def _owns_account(db: AsyncSession, user: User, account_id: str) -> bool:
    broker = await db.scalar(
        select(BrokerAccount.id).where(
            BrokerAccount.id == account_id, BrokerAccount.user_id == user.id
        )
    )
    if broker is not None:
        return True
    paper = await db.scalar(
        select(PaperAccount.id).where(
            PaperAccount.id == account_id, PaperAccount.user_id == user.id
        )
    )
    return paper is not None


async def _owns_bot(db: AsyncSession, user: User, bot_id: str) -> bool:
    found = await db.scalar(select(Bot.id).where(Bot.id == bot_id, Bot.user_id == user.id))
    return found is not None


async def authorize(db: AsyncSession, user: User, channel: Channel) -> None:
    """Raise `NotAuthorized` unless this user may subscribe to this channel.

    Ownership is read from the database on every subscribe. It is not cached
    on the connection: a role or an account can be revoked while a socket is
    open, and a long-lived connection must not outlive the permission that
    opened it.
    """
    if channel.scope is Scope.system:
        return  # any signed-in user; carries no private figures by catalogue rule
    if channel.scope is Scope.user:
        if channel.ref != user.id:
            raise NotAuthorized("a user channel is readable only by that user")
        return
    if channel.scope is Scope.symbol:
        # Market data is not private. It is still gated on being signed in,
        # which the connection handshake has already established.
        return
    if channel.scope is Scope.account:
        if await _owns_account(db, user, channel.ref or ""):
            return
        # Deliberately the same message as a missing account. Telling an
        # unauthorized caller that the id exists is a membership oracle.
        raise NotAuthorized("no such account for this user")
    if channel.scope is Scope.bot:
        if await _owns_bot(db, user, channel.ref or ""):
            return
        raise NotAuthorized("no such bot for this user")
    if channel.scope is Scope.model:
        # Training progress, validation verdicts and lifecycle transitions.
        # Gated at TRADER, matching `manage_ai_models` on the REST surface: a
        # channel readable by someone the API would refuse is a way around the
        # API. It carries no private figure -- a model key, a version and a
        # status -- but it does say what a deployment is running.
        if role_at_least(user.role_enum, Role.trader):
            return
        raise NotAuthorized("watching a model requires the trader role")
    if channel.scope is Scope.strategy:
        # Strategies are shared research objects; TRADER and above may watch
        # one. The registry that would scope them per-owner is L12.
        if role_at_least(user.role_enum, Role.trader):
            return
        raise NotAuthorized("watching a strategy requires the trader role")
    raise NotAuthorized(f"no authorization rule for scope {channel.scope}")  # pragma: no cover


async def authorized_channels(db: AsyncSession, user: User) -> list[str]:
    """Every channel this user may currently subscribe to, by name.

    Symbol and strategy channels are open-ended, so they are described rather
    than enumerated; account and bot channels are listed because they are
    exactly the ones a caller cannot guess at.
    """
    broker = (
        await db.scalars(select(BrokerAccount.id).where(BrokerAccount.user_id == user.id))
    ).all()
    paper = (await db.scalars(select(PaperAccount.id).where(PaperAccount.user_id == user.id))).all()
    bots = (await db.scalars(select(Bot.id).where(Bot.user_id == user.id))).all()
    names = [str(SYSTEM), f"user:{user.id}"]
    names += [f"account:{i}" for i in [*broker, *paper]]
    names += [f"bot:{i}" for i in bots]
    return names
