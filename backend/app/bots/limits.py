"""Per-bot limits, and the one rule that governs them.

**A bot limit can only RESTRICT.** Section 17 states it and this module is
where it is true: `effective()` takes the more restrictive of the bot's figure
and the account's, in every field, in one place. A bot asking for 5% when its
account permits 2% gets 2% — not because a check rejected it, but because the
combination arithmetic cannot produce a looser number than either input.

That is deliberately not "validate the bot config against the account and
reject if it exceeds". Validation happens once, at write time, and a limit
that was legal when it was saved can become illegal when the account tightens.
Combining at read time means the tighter figure wins *whenever* it is tighter,
including for a configuration nobody has touched since.

**The RiskEngine is still the authority.** Everything here is a second, tighter
gate in front of it. A bot limit that somehow passed would still meet the
global limits afterwards; a bot limit that blocks means the RiskEngine is
never asked. Neither order can produce a trade the account did not permit.

**Counters are read, never cached.** `BotCounters` is built from what the
database actually holds for the day, because a cached counter survives a
restart wrong: a bot that had used 9 of 10 daily trades and then restarted
would come back with 0 used, and the tenth trade would become the nineteenth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class BotLimits:
    """What one bot may do. Every field is optional, and None means NOT SET.

    Not set is not the same as unlimited: it means this layer states nothing,
    and the account's limit — whatever it is — is what applies. A field that
    defaulted to a number would be this layer inventing a policy.
    """

    max_positions: int | None = None
    max_daily_trades: int | None = None
    max_daily_loss: Decimal | None = None
    max_risk_per_trade: Decimal | None = None
    cooldown_seconds: int | None = None

    def effective(self, account: BotLimits) -> BotLimits:
        """Combine with the account's limits, taking the more restrictive.

        The whole of section 17, in one function. There is no branch by which
        a bot's figure can widen an account's, because `_tighter` returns the
        smaller of two numbers and `None` on either side means that side
        states nothing.
        """
        return BotLimits(
            max_positions=_tighter(self.max_positions, account.max_positions),
            max_daily_trades=_tighter(self.max_daily_trades, account.max_daily_trades),
            max_daily_loss=_tighter(self.max_daily_loss, account.max_daily_loss),
            max_risk_per_trade=_tighter(self.max_risk_per_trade, account.max_risk_per_trade),
            # A longer cooldown is the more restrictive one, so this is the
            # single field where the LARGER number wins. Getting it backwards
            # would let a bot shorten a cooldown its account imposed.
            cooldown_seconds=_looser(self.cooldown_seconds, account.cooldown_seconds),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "max_positions": self.max_positions,
            "max_daily_trades": self.max_daily_trades,
            "max_daily_loss": str(self.max_daily_loss) if self.max_daily_loss else None,
            "max_risk_per_trade": (
                str(self.max_risk_per_trade) if self.max_risk_per_trade else None
            ),
            "cooldown_seconds": self.cooldown_seconds,
        }


def _tighter(a, b):  # noqa: ANN001, ANN202
    """The smaller of two limits. None on a side means that side is silent."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _looser(a, b):  # noqa: ANN001, ANN202
    """The larger of two figures, for the one field where larger is stricter."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


@dataclass(frozen=True)
class BotCounters:
    """What this bot has actually done today, measured rather than remembered.

    Built from the database on every check. A cached counter survives a
    restart wrong: a bot that had used 9 of 10 daily trades would come back
    with 0 used, and the tenth trade would become the nineteenth.
    """

    open_positions: int = 0
    trades_today: int = 0
    realised_today: Decimal = Decimal("0")
    last_trade_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "open_positions": self.open_positions,
            "trades_today": self.trades_today,
            "realised_today": str(self.realised_today),
            "last_trade_at": self.last_trade_at.isoformat() if self.last_trade_at else None,
        }


@dataclass(frozen=True)
class LimitVerdict:
    """Whether this bot may open a new trade, and why not if it may not."""

    allowed: bool
    code: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "code": self.code, "detail": self.detail}


ALLOWED = LimitVerdict(allowed=True)


def check(limits: BotLimits, counters: BotCounters, *, now: datetime) -> LimitVerdict:
    """May this bot open a NEW trade right now?

    Ordered so the most consequential refusal is named first: a breached loss
    limit is a different message from a full position book, and an operator
    reading one line should see the one that matters.

    This never touches an existing position. Sections 20, 21 and 24 all say
    the same thing in different words — a bot that may not open a new trade
    still has its open trades managed by the position manager.
    """
    if limits.max_daily_loss is not None and counters.realised_today <= -limits.max_daily_loss:
        return LimitVerdict(
            False,
            "bot_daily_loss",
            f"this bot has realised {counters.realised_today} today against a "
            f"{limits.max_daily_loss} limit; no new trades. Open positions are "
            "unaffected and stay under the position manager",
        )

    if limits.max_positions is not None and counters.open_positions >= limits.max_positions:
        return LimitVerdict(
            False,
            "bot_max_positions",
            f"this bot holds {counters.open_positions} of a permitted "
            f"{limits.max_positions} positions",
        )

    if limits.max_daily_trades is not None and counters.trades_today >= limits.max_daily_trades:
        return LimitVerdict(
            False,
            "bot_daily_trades",
            f"this bot has taken {counters.trades_today} of a permitted "
            f"{limits.max_daily_trades} trades today",
        )

    if limits.cooldown_seconds and counters.last_trade_at is not None:
        # Both sides are made aware before subtracting. A naive stamp read from
        # the database compared against an aware `now` raises, and this
        # repository has already paid for that once in the risk engine.
        last = counters.last_trade_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=now.tzinfo)
        elapsed = now - last
        wait = timedelta(seconds=limits.cooldown_seconds)
        if elapsed < wait:
            remaining = (wait - elapsed).total_seconds()
            return LimitVerdict(
                False,
                "bot_cooldown",
                f"this bot traded {elapsed.total_seconds():.0f}s ago and its cooldown is "
                f"{limits.cooldown_seconds}s; {remaining:.0f}s remaining",
            )

    return ALLOWED
