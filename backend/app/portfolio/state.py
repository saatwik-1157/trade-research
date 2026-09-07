"""What the account is, where the figure came from, and how old it is.

Section 5, and the two rules that shape the module:

**Never fabricate a broker value.** Section 59. Every field on `AccountState`
may be `None`, and a `None` is reported as `None` — not as zero, not as the last
value seen, not as a derived guess. §6 and §7 say to prefer the broker's own
balance and equity where it provides them, and this type has no arithmetic that
could quietly replace one.

**Never present stale values as current truth.** Section 44 and §31. Freshness
is part of the state, not a note beside it: `Freshness` is computed from the age
of the reading, and `PortfolioHealth` degrades to `STALE` or
`RECONCILIATION_REQUIRED` rather than reporting a number as though it were
current. A dashboard that shows a two-hour-old balance as "equity" is worse than
one that shows nothing.

**This is not the risk engine.** Section 2. `AccountState` says what is held;
`RiskEngine` says whether a new action is allowed. This module produces the
`PortfolioState` L17 already reads and takes no decision of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Freshness(StrEnum):
    """How old the underlying reading is. Section 44.

    **Not the same enum as `app.portfolio.decision.Freshness`**, which carries
    six uppercase states to this one's three. The overlap is partial and the
    difference is real: `unknown` here means "no timestamp was recorded", which
    is `INVALID` there, and there is no counterpart here for that module's
    `MISSING`, `CONFLICTED` or `AGING`. A caller holding one must not assume the
    other's vocabulary.
    """

    fresh = "FRESH"
    stale = "STALE"
    # Nothing has ever been read for this account. Distinct from stale: one
    # says the value is old, the other says there has never been one.
    unknown = "UNKNOWN"


class PortfolioHealth(StrEnum):
    """The operational state of the portfolio view. Section 45.

    Based on measurable conditions, never on a score. The three that are not
    "fine" all mean something an operator does differently:

      * `STALE` — the numbers are real and old.
      * `RECONCILIATION_REQUIRED` — internal state and the broker disagree.
      * `ERROR` — the view could not be assembled at all.
    """

    healthy = "HEALTHY"
    warning = "WARNING"
    stale = "STALE"
    reconciliation_required = "RECONCILIATION_REQUIRED"
    error = "ERROR"


#: Precedence, most severe first. `RECONCILIATION_REQUIRED` outranks `STALE`
#: because a disagreement about what is held is worse than an old figure for
#: something we agree on -- §30 and §31 both turn on that distinction.
_ORDER: tuple[PortfolioHealth, ...] = (
    PortfolioHealth.error,
    PortfolioHealth.reconciliation_required,
    PortfolioHealth.stale,
    PortfolioHealth.warning,
    PortfolioHealth.healthy,
)


def worst_health(states: list[PortfolioHealth]) -> PortfolioHealth:
    if not states:
        return PortfolioHealth.error
    return min(states, key=_ORDER.index)


class Source(StrEnum):
    """Where a figure came from. Section 6 and §29.

    Recorded per reading rather than per snapshot, because one snapshot mixes
    them: a live account's balance is the broker's and its strategy attribution
    is ours. §29 says not to create conflicting ownership, and naming the source
    is how a reader can tell which system to go and ask.
    """

    broker = "BROKER"
    paper_engine = "PAPER_ENGINE"
    oms = "OMS"
    trade_journal = "TRADE_JOURNAL"
    # Computed here from figures whose own sources are named.
    derived = "DERIVED"
    unavailable = "UNAVAILABLE"


@dataclass(frozen=True)
class AccountState:
    """One account, normalized. Section 5.

    **Every monetary field is optional.** Not every broker provides every one,
    and §5 asks that unavailable values be represented explicitly. A `None`
    here reaches the API as `null` and the dashboard as a dash — never as 0.00,
    which a reader would take for a measurement.
    """

    account_id: str
    environment: str  # paper | demo | live
    currency: str | None = None
    broker: str | None = None

    balance: Decimal | None = None
    equity: Decimal | None = None
    margin_used: Decimal | None = None
    margin_free: Decimal | None = None
    margin_level: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None

    #: When the underlying reading was taken -- NOT when this object was built.
    #: The difference is the whole of §44.
    as_of: datetime | None = None
    source: Source = Source.unavailable
    freshness: Freshness = Freshness.unknown
    #: Present only when something could not be read. §59: say so rather than
    #: substituting a number.
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "environment": self.environment,
            "currency": self.currency,
            "broker": self.broker,
            "balance": _money(self.balance),
            "equity": _money(self.equity),
            "margin_used": _money(self.margin_used),
            "margin_free": _money(self.margin_free),
            "margin_level": _money(self.margin_level),
            "unrealized_pnl": _money(self.unrealized_pnl),
            "realized_pnl": _money(self.realized_pnl),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "source": str(self.source),
            "freshness": str(self.freshness),
            "unavailable_reason": self.unavailable_reason,
            "rule": (
                "an absent figure is null, never zero. Not every broker provides every "
                "field, and a 0.00 in a balance column is a number a reader will act on."
            ),
        }


def _money(value: Decimal | None) -> str | None:
    """Money as a string. Never a float: a float balance is a rounded balance."""
    return None if value is None else str(value)


def freshness_of(as_of: datetime | None, *, now: datetime, tolerance: timedelta) -> Freshness:
    """How old a reading is, against a configured tolerance. Section 44.

    `None` is UNKNOWN rather than STALE: "never read" and "read a while ago" are
    different facts, and only one of them means the connection is working.
    """
    if as_of is None:
        return Freshness.unknown
    return Freshness.fresh if (now - as_of) <= tolerance else Freshness.stale


def unavailable(account_id: str, environment: str, reason: str) -> AccountState:
    """An account whose state could not be read. Sections 31 and 59.

    Every figure `None`, the reason named, and the freshness UNKNOWN. This is
    what a disconnected broker produces, and it is deliberately not a state
    carrying the last values seen: §31 says an uncertain broker state must be
    exposed as such, and stale values shown as current are the failure it
    describes.
    """
    return AccountState(
        account_id=account_id,
        environment=environment,
        source=Source.unavailable,
        freshness=Freshness.unknown,
        unavailable_reason=reason,
    )


@dataclass(frozen=True)
class Reconciliation:
    """Whether internal state and the broker agree. Sections 30 and 31."""

    checked: bool
    agrees: bool
    internal_positions: int = 0
    broker_positions: int = 0
    mismatches: tuple[str, ...] = ()
    checked_at: datetime | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "agrees": self.agrees,
            "internal_positions": self.internal_positions,
            "broker_positions": self.broker_positions,
            "mismatches": list(self.mismatches),
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "note": self.note
            or (
                "a discrepancy is reported, never silently ignored. §30: internal 1.0 lot "
                "against a broker's 0.0 is a fact somebody must see."
            ),
        }


def not_checked(reason: str) -> Reconciliation:
    """No reconciliation has run. NOT the same as "they agree"."""
    return Reconciliation(
        checked=False,
        agrees=False,
        note=(
            f"{reason} No reconciliation has run, which is not the same as agreement: "
            "an unchecked portfolio and a checked one that matched must not look alike."
        ),
    )


def health_of(
    *,
    account: AccountState,
    reconciliation: Reconciliation,
    margin_utilisation: Decimal | None = None,
    margin_warning: Decimal | None = None,
) -> tuple[PortfolioHealth, list[str]]:
    """The operational state, and the reasons for it. Section 45.

    By rule, never a score, and the reasons are returned beside the state so a
    dashboard can say WHY rather than showing a coloured dot somebody has to
    interpret.
    """
    reasons: list[str] = []
    states: list[PortfolioHealth] = []

    if account.source is Source.unavailable:
        states.append(PortfolioHealth.error)
        reasons.append(account.unavailable_reason or "the account state could not be read at all")
    if reconciliation.checked and not reconciliation.agrees:
        states.append(PortfolioHealth.reconciliation_required)
        reasons.append(
            f"internal state and the broker disagree on {len(reconciliation.mismatches)} "
            "position(s). Nothing here resolves that; a reconciliation pass does."
        )
    if account.freshness is Freshness.stale:
        states.append(PortfolioHealth.stale)
        reasons.append(
            f"the account was last read at "
            f"{account.as_of.isoformat() if account.as_of else 'never'}, "
            "which is outside the freshness tolerance. These figures are real and old."
        )
    if account.freshness is Freshness.unknown and account.source is not Source.unavailable:
        states.append(PortfolioHealth.warning)
        reasons.append("the account state carries no timestamp, so its age cannot be checked")
    if (
        margin_utilisation is not None
        and margin_warning is not None
        and margin_utilisation >= margin_warning
    ):
        states.append(PortfolioHealth.warning)
        reasons.append(
            f"margin utilisation is {margin_utilisation:.2%}, at or above the configured "
            f"{margin_warning:.2%}. The RISK ENGINE decides whether that permits a trade; "
            "this only reports it."
        )

    if not states:
        return PortfolioHealth.healthy, ["every reading is present and within tolerance"]
    return worst_health(states), reasons
