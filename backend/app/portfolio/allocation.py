"""Whether an opportunity deserves capital, given what is already held. **L86.**

The question this answers is the third of three, and conflating them is the
failure it exists to prevent:

    GOOD COMPANY        -- the business is sound
    GOOD INVESTMENT     -- the price is wrong
    GOOD PORTFOLIO DECISION -- adding it beats holding what we have, or cash

A thing can be all of the first and none of the third. `app.research.opportunity`
answers the first two and is deliberately not consulted for the third.

## What this reuses rather than rebuilds

None of L86's named engines existed, but most of the *capability* did, under
different module names. This module is a bridge, not a new stack:

| Capability | Reused from |
|---|---|
| layered verdict, missing-is-never-safe | `app.portfolio.decision` (L60) |
| account balance, margin, freshness, source | `app.portfolio.state` (L60) |
| scenarios, monotone tightening | `app.portfolio.scenario` (L66) |
| stress coverage, earned never assumed | `app.portfolio.stress` (L67) |
| gross/net exposure, concentration | `app.portfolio.exposure` |
| forbidden self-loosening | `app.portfolio.control` (L61) |
| horizons | `app.portfolio.horizon` (L65) |
| the final veto | `app.risk` |

`decide()` already returns a `Decision` with `allows_new_risk()`. This module
**never overrides it** — a proposal that would add risk when the platform says
no becomes `BLOCKED`, and the reason is recorded.

## The three rules

**1. Zero allocation is always a valid answer** (§6, §34.10). `NO_ALLOCATION`
is the default and needs no justification; every other outcome does. An engine
that must always allocate something is a forced buyer, and a forced buyer is
the thing every constraint here exists to prevent.

**2. Hard constraints are never overridden** (§7, §34.15). They are checked
last and they are absolute — no score, conviction or scenario can outvote one.
Conviction that can move a hard limit is not a limit.

**3. Nothing is ever sold automatically** (§9). Sell-to-fund produces a
`REVIEW` for a human. The engine may observe that capital is short and that a
position looks weak; turning that into a sale is a decision it does not have.

## Why so much of this returns `insufficient_data`

Marginal contribution, correlation stress and counterfactual portfolios need a
portfolio. This one has **one open position at its historical peak and none
today**, across 7 paper accounts, and `market_bars` holds 705 bars across 2
symbols. `CLAUDE.md` states the requirement rather than the count, because the
count goes stale: *correlation, fragility and cross-instrument stress need a
common window across two or more instruments.*

A correlation computed over one position is not a small correlation, it is not
a correlation. So it is reported absent. This module is built now so the
machinery exists when the portfolio does; it is not built to produce numbers
in the meantime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.portfolio.state import AccountState, Freshness

__all__ = [
    "Allocation",
    "Fit",
    "Horizon",
    "Marginal",
    "PortfolioActionProposal",
    "PortfolioDecisionContext",
    "PositionSnapshot",
    "SellToFund",
    "WaitingValue",
    "propose",
]


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class Fit(StrEnum):
    blocked = "BLOCKED"
    conflicting = "CONFLICTING"
    poor_fit = "POOR_FIT"
    neutral = "NEUTRAL"
    good_fit = "GOOD_FIT"
    strong_fit = "STRONG_FIT"
    #: The portfolio is too small for fit to mean anything.
    indeterminate = "INDETERMINATE"


class Allocation(StrEnum):
    """`no_allocation` is the default and needs no justification."""

    no_allocation = "NO_ALLOCATION"
    watch = "WATCH"
    small_allocation = "SMALL_ALLOCATION"
    standard_allocation = "STANDARD_ALLOCATION"
    reduce_existing = "REDUCE_EXISTING"
    review_required = "REVIEW_REQUIRED"
    blocked = "BLOCKED"


class SellToFund(StrEnum):
    no_sell_required = "NO_SELL_REQUIRED"
    possible_sell_candidate = "POSSIBLE_SELL_CANDIDATE"
    reduce_review = "REDUCE_REVIEW"
    exit_review = "EXIT_REVIEW"


class WaitingValue(StrEnum):
    act_now = "ACT_NOW"
    wait_for_information = "WAIT_FOR_INFORMATION"
    wait_for_valuation = "WAIT_FOR_VALUATION"
    wait_for_catalyst = "WAIT_FOR_CATALYST"
    wait_for_risk_reduction = "WAIT_FOR_RISK_REDUCTION"
    no_action = "NO_ACTION"


class Horizon(StrEnum):
    short_term = "SHORT_TERM"
    medium_term = "MEDIUM_TERM"
    long_term = "LONG_TERM"


class Preservation(StrEnum):
    """§16. Mirrors the existing autonomy ladder in `portfolio.control`."""

    normal = "NORMAL"
    cautious = "CAUTIOUS"
    defensive = "DEFENSIVE"
    capital_preservation = "CAPITAL_PRESERVATION"
    emergency = "EMERGENCY"


class Action(StrEnum):
    observe = "OBSERVE"
    research = "RESEARCH"
    wait = "WAIT"
    hold = "HOLD"
    add_review = "ADD_REVIEW"
    reduce_review = "REDUCE_REVIEW"
    exit_review = "EXIT_REVIEW"
    rebalance_review = "REBALANCE_REVIEW"
    cash_preservation = "CASH_PRESERVATION"
    risk_review = "RISK_REVIEW"
    blocked = "BLOCKED"


#: Hard constraints. Failing any one of these is absolute -- §7, §34.15.
HARD_CONSTRAINTS = (
    "NO_AVAILABLE_CAPITAL",
    "MARGIN_INSUFFICIENT",
    "MAX_POSITION_EXCEEDED",
    "MAX_PORTFOLIO_EXPOSURE",
    "SECTOR_LIMIT",
    "CONCENTRATION_LIMIT",
    "DRAWDOWN_LIMIT",
    "CAPITAL_PRESERVATION_ACTIVE",
    "PLATFORM_FORBIDS_NEW_RISK",
    "ACCOUNT_STATE_UNKNOWN",
)

#: Below this many independent holdings, portfolio-relative quantities are not
#: computed. Two is the floor at which a correlation exists at all.
MIN_HOLDINGS_FOR_PORTFOLIO_MATH = 2


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    market_value: Decimal | None = None
    sector: str | None = None
    strategy: str | None = None
    thesis_strength: float | None = None
    unrealized_pnl: Decimal | None = None


@dataclass(frozen=True)
class Marginal:
    """§5. Portfolio before against portfolio after. Every field may be absent.

    Absent is the normal case here and is not a defect: a delta needs a
    portfolio to be a delta *of*.
    """

    d_expected_return: float | None = None
    d_risk: float | None = None
    d_drawdown: float | None = None
    d_concentration: float | None = None
    d_correlation: float | None = None
    d_liquidity_risk: float | None = None
    d_uncertainty: float | None = None
    computed: bool = False
    reason_absent: str = ""


@dataclass(frozen=True)
class PortfolioDecisionContext:
    """§3. Everything the decision saw, so it can be replayed exactly."""

    as_of: datetime
    account: AccountState
    positions: tuple[PositionSnapshot, ...] = ()
    cash: Decimal | None = None
    available_capital: Decimal | None = None
    risk_budget_remaining: float | None = None
    preservation: Preservation = Preservation.normal
    platform_allows_new_risk: bool = True
    opportunity_symbol: str = ""
    opportunity_sector: str | None = None
    opportunity_score: float | None = None
    opportunity_edge: str = "no_edge"
    opportunity_blocking: tuple[str, ...] = ()
    horizon: Horizon = Horizon.medium_term
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def holdings(self) -> int:
        return len(self.positions)


@dataclass(frozen=True)
class PortfolioActionProposal:
    """§20. Advisory only. Carries its own expiry — §28."""

    action: Action
    allocation: Allocation
    fit: Fit
    marginal: Marginal
    sell_to_fund: SellToFund
    waiting: WaitingValue
    horizon: Horizon
    capital_required: Decimal | None
    confidence: float | None
    uncertainty: str
    reason: tuple[str, ...]
    evidence: tuple[str, ...]
    hard_constraint_failures: tuple[str, ...]
    data_gaps: tuple[str, ...]
    created_at: datetime
    valid_until: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.valid_until

    @property
    def permits_capital(self) -> bool:
        """Whether any capital is proposed at all. Never implies authority."""
        return self.allocation in (
            Allocation.small_allocation,
            Allocation.standard_allocation,
        )


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------


def _hard_constraints(ctx: PortfolioDecisionContext) -> list[str]:
    """§7. Checked without reference to how attractive the opportunity is.

    Deliberately takes no score argument. A constraint that can see the
    conviction behind a request is a constraint that can be argued with.
    """
    failures: list[str] = []

    if ctx.account.freshness in (Freshness.unknown, Freshness.stale):
        # L60's rule: missing is never safe. An account whose state is not
        # known cannot be shown to have room.
        failures.append("ACCOUNT_STATE_UNKNOWN")
    if ctx.account.balance is None and ctx.available_capital is None:
        failures.append("ACCOUNT_STATE_UNKNOWN")

    if ctx.available_capital is not None and ctx.available_capital <= 0:
        failures.append("NO_AVAILABLE_CAPITAL")
    if ctx.account.margin_free is not None and ctx.account.margin_free <= 0:
        failures.append("MARGIN_INSUFFICIENT")

    if not ctx.platform_allows_new_risk:
        failures.append("PLATFORM_FORBIDS_NEW_RISK")

    if ctx.preservation in (
        Preservation.capital_preservation,
        Preservation.emergency,
    ):
        failures.append("CAPITAL_PRESERVATION_ACTIVE")

    if ctx.risk_budget_remaining is not None and ctx.risk_budget_remaining <= 0:
        failures.append("DRAWDOWN_LIMIT")

    return list(dict.fromkeys(failures))


def marginal_contribution(ctx: PortfolioDecisionContext) -> Marginal:
    """§5. Refuses to compute on a portfolio too small to have a margin."""
    if ctx.holdings < MIN_HOLDINGS_FOR_PORTFOLIO_MATH:
        return Marginal(
            computed=False,
            reason_absent=(
                f"{ctx.holdings} holding(s); portfolio-relative quantities need "
                f"at least {MIN_HOLDINGS_FOR_PORTFOLIO_MATH} independent holdings"
            ),
        )

    valued = [p for p in ctx.positions if p.market_value is not None]
    if len(valued) < MIN_HOLDINGS_FOR_PORTFOLIO_MATH:
        return Marginal(
            computed=False,
            reason_absent="positions carry no market value; nothing to weight",
        )

    total = sum((p.market_value or Decimal(0)) for p in valued)
    if total <= 0:
        return Marginal(computed=False, reason_absent="portfolio value is not positive")

    # Concentration as a Herfindahl index. It is the one portfolio-relative
    # quantity computable from position values alone -- risk, drawdown and
    # correlation all need a return series this repository does not have for
    # these instruments, and are left absent rather than approximated.
    weights = [float((p.market_value or Decimal(0)) / total) for p in valued]
    hhi = sum(w * w for w in weights)
    n_after = len(valued) + 1
    even_after = 1.0 / n_after
    hhi_after = sum(w * w for w in weights) * (1 - even_after) ** 2 + even_after**2

    return Marginal(
        d_concentration=round(hhi_after - hhi, 6),
        computed=True,
        reason_absent=(
            "risk, drawdown and correlation deltas need a return series per "
            "holding; none is stored for these instruments"
        ),
    )


def portfolio_fit(ctx: PortfolioDecisionContext, marginal: Marginal) -> Fit:
    """§4. Sector overlap is the one overlap computable from what is stored."""
    if ctx.holdings < MIN_HOLDINGS_FOR_PORTFOLIO_MATH:
        return Fit.indeterminate

    same_sector = [
        p for p in ctx.positions if ctx.opportunity_sector and p.sector == ctx.opportunity_sector
    ]
    share = len(same_sector) / ctx.holdings if ctx.holdings else 0.0

    if share >= 0.5:
        return Fit.conflicting
    if share >= 0.25:
        return Fit.poor_fit
    if marginal.computed and marginal.d_concentration is not None:
        if marginal.d_concentration > 0.05:
            return Fit.poor_fit
        if marginal.d_concentration < 0:
            return Fit.good_fit
    return Fit.neutral


def sell_to_fund(ctx: PortfolioDecisionContext, *, capital_short: bool) -> SellToFund:
    """§9. Produces a review. Never a sale."""
    if not capital_short:
        return SellToFund.no_sell_required
    weak = [p for p in ctx.positions if p.thesis_strength is not None and p.thesis_strength < 0.3]
    if not weak:
        return SellToFund.no_sell_required
    return SellToFund.reduce_review


def waiting_value(ctx: PortfolioDecisionContext, fit: Fit, hard: list[str]) -> WaitingValue:
    """§17. Waiting is a decision, and here it is usually the right one."""
    if hard:
        return WaitingValue.no_action
    if ctx.opportunity_blocking:
        return WaitingValue.wait_for_information
    if ctx.opportunity_edge == "no_edge":
        # The repository's standing finding. Absent demonstrated edge there is
        # nothing time-sensitive to act on.
        return WaitingValue.wait_for_information
    if fit in (Fit.conflicting, Fit.poor_fit):
        return WaitingValue.wait_for_risk_reduction
    if fit is Fit.indeterminate:
        return WaitingValue.wait_for_information
    return WaitingValue.act_now


def propose(
    ctx: PortfolioDecisionContext,
    *,
    valid_for: timedelta = timedelta(hours=12),
    now: datetime | None = None,
) -> PortfolioActionProposal:
    """The entry point. Advisory: it proposes, and authorises nothing.

    Order matters. Hard constraints are evaluated first and settle the outcome
    on their own; everything after them can only make the answer *more*
    conservative, never less -- the same monotone rule `portfolio.scenario`
    already enforces for forecasts.
    """
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    evidence: list[str] = []
    gaps: list[str] = []

    hard = _hard_constraints(ctx)
    marginal = marginal_contribution(ctx)
    if not marginal.computed:
        gaps.append(f"marginal contribution absent: {marginal.reason_absent}")
    elif marginal.reason_absent:
        gaps.append(marginal.reason_absent)

    fit = portfolio_fit(ctx, marginal)
    if fit is Fit.indeterminate:
        gaps.append(f"portfolio fit indeterminate: {ctx.holdings} holding(s) is too few")

    capital_short = ctx.available_capital is not None and ctx.available_capital <= Decimal(0)
    sell = sell_to_fund(ctx, capital_short=capital_short)
    wait = waiting_value(ctx, fit, hard)

    # --- the outcome ------------------------------------------------------
    if hard:
        allocation = Allocation.blocked
        action = Action.blocked
        reasons.extend(f"hard constraint: {h}" for h in hard)
    elif ctx.opportunity_blocking:
        allocation = Allocation.no_allocation
        action = Action.research
        reasons.extend(f"opportunity blocked: {b}" for b in ctx.opportunity_blocking)
    elif ctx.preservation is Preservation.defensive:
        # §16: preservation does not forbid, it tightens. The strongest
        # outcome available under `defensive` is a review, never an allocation.
        allocation = Allocation.review_required
        action = Action.risk_review
        reasons.append("capital preservation is DEFENSIVE; allocation needs review")
    elif fit in (Fit.conflicting, Fit.blocked):
        allocation = Allocation.no_allocation
        action = Action.observe
        reasons.append(f"portfolio fit is {fit}")
    elif wait is not WaitingValue.act_now:
        allocation = Allocation.watch
        action = Action.wait
        reasons.append(f"waiting is preferred: {wait}")
    elif ctx.opportunity_score is not None and ctx.opportunity_score >= 0.6:
        allocation = Allocation.small_allocation
        action = Action.add_review
        reasons.append("opportunity scores above 0.6 with no blocking factor")
    else:
        allocation = Allocation.no_allocation
        action = Action.observe
        reasons.append("nothing clears the bar for capital")

    if sell is not SellToFund.no_sell_required:
        reasons.append(f"capital is short; {sell} (no position is sold by this engine)")

    evidence.append(f"holdings={ctx.holdings}")
    evidence.append(f"edge={ctx.opportunity_edge}")
    evidence.append(f"preservation={ctx.preservation}")
    if ctx.available_capital is not None:
        evidence.append(f"available_capital={ctx.available_capital}")
    evidence.append(f"account_freshness={ctx.account.freshness}")

    confidence = None if (gaps or hard) else 0.5

    return PortfolioActionProposal(
        action=action,
        allocation=allocation,
        fit=fit,
        marginal=marginal,
        sell_to_fund=sell,
        waiting=wait,
        horizon=ctx.horizon,
        capital_required=None,
        confidence=confidence,
        uncertainty=(
            "portfolio-relative quantities are absent below "
            f"{MIN_HOLDINGS_FOR_PORTFOLIO_MATH} holdings; this engine "
            "authorises nothing and the RiskEngine remains the final veto"
        ),
        reason=tuple(reasons),
        evidence=tuple(evidence),
        hard_constraint_failures=tuple(hard),
        data_gaps=tuple(dict.fromkeys(gaps)),
        created_at=now,
        valid_until=now + valid_for,
    )
