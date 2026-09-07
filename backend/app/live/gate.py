"""LiveTradingGate: the mandatory checks that stand between a deployment and
real money.

Brief §5 and §6. Checks in nine groups, each returning PASS, FAIL or WARNING,
and one rule that decides the shape of all of them:

    **A check that cannot get its evidence FAILS. It never skips.**

That is the same rule `RiskEngine` runs on -- *unknown is not permission* --
and it is here for the same reason. A gate that reported "not checked" for the
things it could not reach would produce a green preflight on a machine where
nothing was connected, which is precisely the machine on which a green
preflight is most dangerous. So `LiveContext` fields default to `None`, and
`None` is a FAIL with a detail saying what was missing.

**The gate authorises TURNING LIVE EXECUTION ON. It does not authorise an
order.** Nothing here can be consulted in place of `RiskEngine`: an order still
needs an `Approval`, `Approval` is constructible only by `RiskEngine.approve`,
and the OMS still refuses without one. This module imports neither, and
`test_live_gate.py` parses the package to prove it.

**WARNING is narrow on purpose.** Only checks marked `mandatory=False` may
return it, and there are two of them -- outbound notification delivery and
release stamping -- because neither can make an order unsafe. Everything that
can is mandatory, and a mandatory WARNING is treated as a FAIL by `ready`
rather than being quietly tolerated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.live.allowlist import Allowlist


class Verdict(StrEnum):
    passed = "PASS"
    failed = "FAIL"
    warning = "WARNING"


@dataclass(frozen=True)
class Check:
    """One question, its answer, and why."""

    name: str
    verdict: Verdict
    detail: str
    group: str
    mandatory: bool = True

    @property
    def blocking(self) -> bool:
        """A mandatory check that did not pass blocks activation.

        A mandatory WARNING blocks too. "Warning" on something that can make an
        order unsafe is a FAIL wearing a softer word, and the soft word is how
        it gets waved through.
        """
        return self.mandatory and self.verdict is not Verdict.passed

    def as_dict(self) -> dict[str, object]:
        return {
            "check": self.name,
            "group": self.group,
            "verdict": self.verdict.value,
            "mandatory": self.mandatory,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateReport:
    """Every check, and the single verdict that follows from them."""

    checks: tuple[Check, ...]
    generated_at: datetime

    @property
    def ready(self) -> bool:
        """READY_FOR_LIVE, and fail-closed on an empty report.

        Zero checks is not "nothing went wrong". It is a gate that did not run,
        and a gate that did not run must not read as a gate that passed.
        """
        if not self.checks:
            return False
        return not any(check.blocking for check in self.checks)

    @property
    def blockers(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.blocking)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(
            check
            for check in self.checks
            if check.verdict is Verdict.warning and not check.mandatory
        )

    @property
    def verdict(self) -> str:
        return "READY_FOR_LIVE" if self.ready else "NOT_READY_FOR_LIVE"

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c.verdict is Verdict.passed),
            "failed": sum(1 for c in self.checks if c.verdict is Verdict.failed),
            "warning": sum(1 for c in self.checks if c.verdict is Verdict.warning),
            "blocking": len(self.blockers),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "ready": self.ready,
            "generated_at": self.generated_at.isoformat(),
            "counts": self.counts(),
            "checks": [c.as_dict() for c in self.checks],
            "blockers": [c.as_dict() for c in self.blockers],
        }


# --------------------------------------------------------------------------
# What the gate is given to look at.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectedAccount:
    """The account as the TERMINAL reports it, never as configuration claims.

    §6 turns on this distinction: the check that matters compares what is
    connected against what was approved, and a value copied out of the config
    file would compare the configuration against itself.
    """

    account_id: str
    server: str
    broker: str
    #: "DEMO", "REAL", "CONTEST", or "UNKNOWN(n)" for a value this build does
    #: not recognise. Unrecognised is not treated as safe.
    account_type: str
    currency: str
    balance: Decimal | None = None
    equity: Decimal | None = None
    free_margin: Decimal | None = None
    leverage: int | None = None
    trade_allowed: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "server": self.server,
            "broker": self.broker,
            "account_type": self.account_type,
            "currency": self.currency,
            "balance": str(self.balance) if self.balance is not None else None,
            "equity": str(self.equity) if self.equity is not None else None,
            "free_margin": str(self.free_margin) if self.free_margin is not None else None,
            "leverage": self.leverage,
            "trade_allowed": self.trade_allowed,
        }


@dataclass(frozen=True)
class SymbolState:
    """One tradable symbol as the venue currently reports it."""

    internal: str
    venue_symbol: str | None
    bid: Decimal | None
    ask: Decimal | None
    quoted_at: datetime | None
    market_open: bool | None
    max_spread: Decimal | None = None

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class StrategyLock:
    """A strategy authorised to trade live, pinned to one version."""

    strategy_id: str
    version_id: str
    status: str
    fingerprint: str | None
    symbols: tuple[str, ...] = ()
    has_stop: bool = False
    has_target: bool = False


@dataclass(frozen=True)
class ModelLock:
    """An AI model authorised to advise, pinned to one version."""

    model_id: str
    version: str
    status: str
    fingerprint: str | None


@dataclass(frozen=True)
class CapitalLimits:
    """The approved budget. The gate reads it; nothing here may raise it."""

    approved_capital: Decimal | None = None
    risk_budget: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_drawdown: Decimal | None = None
    max_exposure: Decimal | None = None
    max_position_size: Decimal | None = None
    max_open_positions: int | None = None
    max_daily_orders: int | None = None

    def missing(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.__dict__.items() if value is None)


@dataclass(frozen=True)
class LiveContext:
    """Everything the gate inspects. Absent means FAIL, never skip."""

    settings: object
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Identity and venue
    allowlist: Allowlist = field(default_factory=Allowlist)
    expected_account_id: str | None = None
    expected_server: str | None = None
    connected_account: ConnectedAccount | None = None
    adapter_registered: bool | None = None
    adapter_mode: str | None = None
    adapter_healthy: bool | None = None
    terminal_reachable: bool | None = None

    # Market data
    symbols: tuple[SymbolState, ...] = ()
    market_data_max_age_seconds: float = 60.0

    # Strategy and model
    strategies: tuple[StrategyLock, ...] = ()
    ai_enabled: bool = False
    models: tuple[ModelLock, ...] = ()
    ai_fallback_defined: bool | None = None

    # Risk and capital
    risk_engine_healthy: bool | None = None
    risk_limits_configured: bool | None = None
    capital: CapitalLimits | None = None
    kill_switch_engaged: str | None = None
    kill_switches_available: bool | None = None

    # Execution machinery
    position_sizer_healthy: bool | None = None
    oms_healthy: bool | None = None
    position_manager_healthy: bool | None = None
    unresolved_orders: int | None = None
    unknown_orders: int | None = None
    positions_reconciled: bool | None = None
    orders_reconciled: bool | None = None
    unexpected_positions: int | None = None

    # Platform
    safe_mode_engaged: bool | None = None
    recovery_available: bool | None = None
    monitoring_active: bool | None = None
    notifications_healthy: bool | None = None
    database_healthy: bool | None = None
    redis_healthy: bool | None = None
    workers_healthy: bool | None = None
    audit_logging_active: bool | None = None
    step_up_required: bool | None = None
    release_stamped: bool | None = None


# --------------------------------------------------------------------------
# The gate.
# --------------------------------------------------------------------------

#: The phrase `ENABLE_LIVE_TRADING_CONFIRMATION` must equal. Long and
#: unpleasant to type by accident, which is the point (§22 of L70).
CONFIRMATION_PHRASE = "YES_I_UNDERSTAND"


class LiveTradingGate:
    """Runs every mandatory check and reports one verdict.

    Stateless and side-effect free: it reads a context and returns a report.
    It does not engage safe mode, does not write audit rows and does not change
    a setting -- the caller decides what to do with a refusal, and a gate that
    acted on its own findings would be a second controller.
    """

    def evaluate(self, ctx: LiveContext) -> GateReport:
        checks: list[Check] = []
        checks.extend(self._configuration(ctx))
        checks.extend(self._account(ctx))
        checks.extend(self._venue(ctx))
        checks.extend(self._market_data(ctx))
        checks.extend(self._strategy(ctx))
        checks.extend(self._ai(ctx))
        checks.extend(self._risk(ctx))
        checks.extend(self._execution(ctx))
        checks.extend(self._platform(ctx))
        return GateReport(checks=tuple(checks), generated_at=ctx.now)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _check(
        name: str,
        group: str,
        ok: bool | None,
        yes: str,
        no: str,
        *,
        mandatory: bool = True,
        unknown: str | None = None,
    ) -> Check:
        """PASS on True, FAIL on False, FAIL on None with its own wording.

        `None` gets a distinct detail because "we could not tell" and "we
        looked and it was wrong" call for different fixes, and collapsing them
        sends an operator to debug the wrong thing.
        """
        if ok is None:
            return Check(
                name=name,
                verdict=Verdict.failed,
                detail=unknown or f"{no} (nothing was supplied to check)",
                group=group,
                mandatory=mandatory,
            )
        return Check(
            name=name,
            verdict=Verdict.passed if ok else Verdict.failed,
            detail=yes if ok else no,
            group=group,
            mandatory=mandatory,
        )

    # ------------------------------------------------------ 1. configuration

    def _configuration(self, ctx: LiveContext) -> list[Check]:
        s = ctx.settings
        environment = getattr(getattr(s, "environment", None), "value", None)
        mode = getattr(getattr(s, "trading_mode", None), "value", None)
        live_flag = bool(getattr(s, "live_trading", False))
        confirmation = str(getattr(s, "enable_live_trading_confirmation", "") or "")
        blockers = list(getattr(s, "live_execution_blockers", lambda: ["unavailable"])())

        return [
            self._check(
                "environment_is_production",
                "configuration",
                environment == "production",
                "ENVIRONMENT=production",
                f"ENVIRONMENT={environment!r}; a live account reached from a "
                "development environment is the accident this check exists for",
            ),
            self._check(
                "trading_mode_is_live",
                "configuration",
                mode == "live",
                "TRADING_MODE=live",
                f"TRADING_MODE={mode!r}",
            ),
            self._check(
                "live_trading_flag_set",
                "configuration",
                live_flag,
                "LIVE_TRADING=true",
                "LIVE_TRADING=false",
            ),
            self._check(
                "explicit_confirmation_phrase",
                "configuration",
                confirmation == CONFIRMATION_PHRASE,
                "ENABLE_LIVE_TRADING_CONFIRMATION is set to the required phrase",
                "ENABLE_LIVE_TRADING_CONFIRMATION must be set to "
                f"{CONFIRMATION_PHRASE!r}; it is "
                f"{'unset' if not confirmation else 'set to something else'}",
            ),
            self._check(
                "live_gates_all_built",
                "configuration",
                not blockers,
                "every LIVE_GATES entry is built and verified",
                f"{len(blockers)} live blocker(s): " + "; ".join(blockers[:12]),
            ),
        ]

    # ----------------------------------------------------------- 2. account

    def _account(self, ctx: LiveContext) -> list[Check]:
        acct = ctx.connected_account
        checks: list[Check] = [
            self._check(
                "allowlist_configured",
                "account",
                ctx.allowlist.configured or None,
                f"{len(ctx.allowlist.accounts)} account(s) allowlisted for live trading",
                "LIVE_ALLOWED_ACCOUNT_IDS is empty; an unconfigured allowlist "
                "permits nothing, which is the fail-closed reading",
                unknown="LIVE_ALLOWED_ACCOUNT_IDS is empty; an unconfigured "
                "allowlist permits nothing, which is the fail-closed reading",
            ),
            self._check(
                "account_readable",
                "account",
                acct is not None,
                "the connected account was read from the terminal",
                "no connected account was read; identity cannot be verified",
                unknown="no connected account was read; identity cannot be verified "
                "and an unverified account may not trade",
            ),
        ]
        if acct is None:
            # Every downstream identity check would be asking about a value
            # nobody has. Report them as failures rather than omitting them:
            # a check that vanishes from a report is a check nobody notices is
            # missing.
            for name, detail in (
                ("account_allowlisted", "no account was read, so none is allowlisted"),
                ("account_matches_expected", "no account was read to compare"),
                ("server_matches_expected", "no server was read to compare"),
                ("account_type_is_live", "no account type was read"),
                ("account_trading_permitted", "no trading permission was read"),
                ("account_has_equity", "no equity was read"),
                ("account_has_free_margin", "no free margin was read"),
            ):
                checks.append(Check(name, Verdict.failed, detail, "account", mandatory=True))
            return checks

        expected_id = (ctx.expected_account_id or "").strip()
        expected_server = (ctx.expected_server or "").strip()
        checks.extend(
            [
                self._check(
                    "account_allowlisted",
                    "account",
                    ctx.allowlist.permits(acct.account_id),
                    f"account {acct.account_id} is allowlisted",
                    f"account {acct.account_id} is NOT allowlisted; live trading blocked",
                ),
                self._check(
                    "account_matches_expected",
                    "account",
                    bool(expected_id) and expected_id == str(acct.account_id).strip(),
                    f"connected account {acct.account_id} matches the approved account",
                    (
                        f"connected account {acct.account_id} does not match approved "
                        f"{expected_id!r}"
                        if expected_id
                        else "LIVE_EXPECTED_ACCOUNT_ID is unset; there is nothing to "
                        "compare the connected account against"
                    ),
                ),
                self._check(
                    "server_matches_expected",
                    "account",
                    bool(expected_server) and expected_server == acct.server.strip(),
                    f"connected server {acct.server} matches the approved server",
                    (
                        f"connected server {acct.server!r} does not match approved "
                        f"{expected_server!r}"
                        if expected_server
                        else "LIVE_EXPECTED_SERVER is unset; there is nothing to "
                        "compare the connected server against"
                    ),
                ),
                self._check(
                    "account_type_is_live",
                    "account",
                    acct.account_type.upper() == "REAL",
                    "the connected account is a REAL account",
                    f"the connected account is {acct.account_type}; "
                    "tools/mt5_paper.assert_demo refuses anything but DEMO in code, "
                    "so this platform cannot reach a real account today",
                ),
                self._check(
                    "account_trading_permitted",
                    "account",
                    acct.trade_allowed,
                    "the terminal reports trading permitted",
                    "the terminal reports trading NOT permitted "
                    "(Algo Trading switch, or a broker-side restriction)",
                ),
                self._check(
                    "account_has_equity",
                    "account",
                    None if acct.equity is None else acct.equity > 0,
                    f"equity {acct.equity}",
                    "equity is zero or negative",
                ),
                self._check(
                    "account_has_free_margin",
                    "account",
                    None if acct.free_margin is None else acct.free_margin > 0,
                    f"free margin {acct.free_margin}",
                    "no free margin available",
                ),
            ]
        )
        return checks

    # ------------------------------------------------------------- 3. venue

    def _venue(self, ctx: LiveContext) -> list[Check]:
        return [
            self._check(
                "broker_adapter_registered",
                "venue",
                ctx.adapter_registered,
                "a broker adapter is registered for the account",
                "no broker adapter is registered; a routed signal stops at no_venue",
            ),
            self._check(
                "broker_adapter_mode_is_live",
                "venue",
                None if ctx.adapter_mode is None else ctx.adapter_mode == "live",
                "the registered adapter is a live venue",
                f"the registered adapter is a {ctx.adapter_mode!r} venue; "
                "a live platform may not be pointed at a simulator or a demo",
                unknown="no adapter mode was supplied; a venue nobody can name is "
                "not a venue anybody approved",
            ),
            self._check(
                "broker_connection_healthy",
                "venue",
                ctx.adapter_healthy,
                "the adapter reports a healthy connection",
                "the adapter does not report a healthy connection",
            ),
            self._check(
                "terminal_reachable",
                "venue",
                ctx.terminal_reachable,
                "the MetaTrader terminal answered",
                "the MetaTrader terminal did not answer",
            ),
        ]

    # ------------------------------------------------------- 4. market data

    def _market_data(self, ctx: LiveContext) -> list[Check]:
        if not ctx.symbols:
            return [
                Check(
                    "symbols_present",
                    Verdict.failed,
                    "no symbol was supplied; a live session with no tradable symbol "
                    "is not a configuration anybody meant",
                    "market_data",
                )
            ]

        limit = timedelta(seconds=ctx.market_data_max_age_seconds)
        unmapped = [s.internal for s in ctx.symbols if not s.venue_symbol]
        no_quote = [s.internal for s in ctx.symbols if s.bid is None or s.ask is None]
        stale = [
            s.internal
            for s in ctx.symbols
            if s.quoted_at is None or (ctx.now - s.quoted_at) > limit
        ]
        closed = [s.internal for s in ctx.symbols if s.market_open is not True]
        crossed = [s.internal for s in ctx.symbols if s.spread is not None and s.spread < 0]
        wide = [
            s.internal
            for s in ctx.symbols
            if s.max_spread is not None and s.spread is not None and s.spread > s.max_spread
        ]

        return [
            Check(
                "symbols_present",
                Verdict.passed,
                f"{len(ctx.symbols)} symbol(s) configured",
                "market_data",
            ),
            self._check(
                "symbols_mapped",
                "market_data",
                not unmapped,
                "every symbol maps to a venue symbol",
                f"unmapped: {', '.join(unmapped)}",
            ),
            self._check(
                "quotes_available",
                "market_data",
                not no_quote,
                "every symbol has a bid and an ask",
                f"no quote for: {', '.join(no_quote)}",
            ),
            self._check(
                "quotes_fresh",
                "market_data",
                not stale,
                f"every quote is newer than {ctx.market_data_max_age_seconds:g}s",
                f"stale or undated quote for: {', '.join(stale)}",
            ),
            self._check(
                "market_open",
                "market_data",
                not closed,
                "every symbol's market is open",
                f"market not confirmed open for: {', '.join(closed)}",
            ),
            self._check(
                "quotes_not_crossed",
                "market_data",
                not crossed,
                "no crossed quote",
                f"ask below bid for: {', '.join(crossed)}",
            ),
            self._check(
                "spread_within_limit",
                "market_data",
                not wide,
                "every spread is inside its configured limit",
                f"spread over limit for: {', '.join(wide)}",
            ),
        ]

    # ---------------------------------------------------------- 5. strategy

    def _strategy(self, ctx: LiveContext) -> list[Check]:
        if not ctx.strategies:
            return [
                Check(
                    "strategy_authorised",
                    Verdict.failed,
                    "no strategy is authorised for live trading",
                    "strategy",
                ),
                Check(
                    "strategy_version_locked",
                    Verdict.failed,
                    "no strategy, so no version is locked",
                    "strategy",
                ),
                Check(
                    "strategy_has_protection",
                    Verdict.failed,
                    "no strategy, so no stop or target was checked",
                    "strategy",
                ),
            ]

        approved = {"approved", "active", "live"}
        unapproved = [s.strategy_id for s in ctx.strategies if s.status.lower() not in approved]
        unpinned = [s.strategy_id for s in ctx.strategies if not s.version_id or not s.fingerprint]
        unprotected = [s.strategy_id for s in ctx.strategies if not (s.has_stop and s.has_target)]
        return [
            self._check(
                "strategy_authorised",
                "strategy",
                not unapproved,
                f"{len(ctx.strategies)} strategy/strategies approved for live",
                f"not approved: {', '.join(unapproved)}",
            ),
            self._check(
                "strategy_version_locked",
                "strategy",
                not unpinned,
                "every live strategy is pinned to a version and a fingerprint",
                f"no version or fingerprint for: {', '.join(unpinned)}",
            ),
            self._check(
                "strategy_has_protection",
                "strategy",
                not unprotected,
                "every live strategy defines a stop and a target",
                f"missing stop or target: {', '.join(unprotected)}; "
                "a position that cannot be protected must not be entered",
            ),
        ]

    # ---------------------------------------------------------------- 6. AI

    def _ai(self, ctx: LiveContext) -> list[Check]:
        if not ctx.ai_enabled:
            return [
                Check(
                    "ai_seat_configured",
                    Verdict.passed,
                    "the AI seat is disabled; no opinion is not approval, and the "
                    "deterministic path is unaffected",
                    "ai",
                )
            ]
        approved = {"promoted", "approved", "active"}
        unapproved = [m.model_id for m in ctx.models if m.status.lower() not in approved]
        unpinned = [m.model_id for m in ctx.models if not m.version or not m.fingerprint]
        return [
            self._check(
                "ai_model_present",
                "ai",
                bool(ctx.models),
                f"{len(ctx.models)} model(s) supplied",
                "the AI seat is enabled but no model was supplied",
            ),
            self._check(
                "ai_model_approved",
                "ai",
                bool(ctx.models) and not unapproved,
                "every model is approved",
                f"not approved: {', '.join(unapproved) or 'no model supplied'}",
            ),
            self._check(
                "ai_model_version_locked",
                "ai",
                bool(ctx.models) and not unpinned,
                "every model is pinned to a version and a fingerprint",
                f"no version or fingerprint for: {', '.join(unpinned) or 'no model supplied'}",
            ),
            self._check(
                "ai_fallback_defined",
                "ai",
                ctx.ai_fallback_defined,
                "a deterministic fallback is defined for model unavailability",
                "no fallback is defined; an unavailable model must not stop the "
                "deterministic path, and must never widen it either",
            ),
        ]

    # -------------------------------------------------- 7. risk and capital

    def _risk(self, ctx: LiveContext) -> list[Check]:
        capital = ctx.capital
        missing = capital.missing() if capital else ()
        return [
            self._check(
                "risk_engine_healthy",
                "risk",
                ctx.risk_engine_healthy,
                "the RiskEngine answered and is the final veto",
                "the RiskEngine is not available; with no engine there is no "
                "approval, and with no approval there is no order",
            ),
            self._check(
                "risk_limits_configured",
                "risk",
                ctx.risk_limits_configured,
                "risk limits are configured for the account",
                "no risk limits are configured; an unset limit is an unenforced one",
            ),
            self._check(
                "capital_limits_configured",
                "risk",
                None if capital is None else not missing,
                "every approved capital limit is set",
                f"capital limits not set: {', '.join(missing)}",
                unknown="no capital limits were supplied; the approved budget must "
                "be explicit before it can be enforced",
            ),
            self._check(
                "kill_switches_available",
                "risk",
                ctx.kill_switches_available,
                "kill switches are reachable at global, account and strategy scope",
                "kill switches are not reachable",
            ),
            self._check(
                "no_kill_switch_engaged",
                "risk",
                ctx.kill_switch_engaged is None,
                "no kill switch is engaged",
                f"kill switch engaged: {ctx.kill_switch_engaged}",
            ),
        ]

    # --------------------------------------------------------- 8. execution

    def _execution(self, ctx: LiveContext) -> list[Check]:
        return [
            self._check(
                "position_sizer_healthy",
                "execution",
                ctx.position_sizer_healthy,
                "the position sizer answered",
                "the position sizer is not available",
            ),
            self._check(
                "oms_healthy",
                "execution",
                ctx.oms_healthy,
                "the OMS answered and holds the only path to a venue",
                "the OMS is not available",
            ),
            self._check(
                "position_manager_healthy",
                "execution",
                ctx.position_manager_healthy,
                "the position manager answered",
                "the position manager is not available",
            ),
            self._check(
                "no_unresolved_orders",
                "execution",
                None if ctx.unresolved_orders is None else ctx.unresolved_orders == 0,
                "no unresolved order",
                f"{ctx.unresolved_orders} unresolved order(s); an order the venue may "
                "be holding makes a new one a second order waiting to happen",
                unknown="unresolved orders were not counted; an order the venue may be "
                "holding makes a new one a second order waiting to happen",
            ),
            self._check(
                "no_unknown_order_states",
                "execution",
                None if ctx.unknown_orders is None else ctx.unknown_orders == 0,
                "no order in unknown state",
                f"{ctx.unknown_orders} order(s) in unknown state; these are settled by "
                "reconciliation, never by a retry",
                unknown="orders in unknown state were not counted; these are settled by "
                "reconciliation, never by a retry",
            ),
            self._check(
                "positions_reconciled",
                "execution",
                ctx.positions_reconciled,
                "internal positions agree with the venue",
                "positions do not agree with the venue, or were not compared",
            ),
            self._check(
                "orders_reconciled",
                "execution",
                ctx.orders_reconciled,
                "internal orders agree with the venue",
                "orders do not agree with the venue, or were not compared",
            ),
            self._check(
                "no_unexpected_positions",
                "execution",
                None if ctx.unexpected_positions is None else ctx.unexpected_positions == 0,
                "the venue holds nothing the platform does not know about",
                f"{ctx.unexpected_positions} position(s) at the venue that the platform "
                "does not know about; these are flagged for a person, never closed "
                "or adopted automatically",
                unknown="the venue's positions were not compared against the platform's; "
                "an unexpected position is flagged for a person, never closed or adopted",
            ),
        ]

    # ---------------------------------------------------------- 9. platform

    def _platform(self, ctx: LiveContext) -> list[Check]:
        return [
            self._check(
                "safe_mode_clear",
                "platform",
                None if ctx.safe_mode_engaged is None else not ctx.safe_mode_engaged,
                "safe mode is not engaged",
                "safe mode is engaged; it is released by reconciling, not by activating",
            ),
            self._check(
                "recovery_available",
                "platform",
                ctx.recovery_available,
                "the recovery manager is available",
                "the recovery manager is not available",
            ),
            self._check(
                "monitoring_active",
                "platform",
                ctx.monitoring_active,
                "monitoring is collecting",
                "monitoring is not collecting; a live session nobody is watching is "
                "one whose first failure is discovered from the balance",
            ),
            self._check(
                "database_healthy",
                "platform",
                ctx.database_healthy,
                "the database answered",
                "the database did not answer; order state must be durable before a venue is called",
            ),
            self._check(
                "redis_healthy",
                "platform",
                ctx.redis_healthy,
                "Redis answered",
                "Redis did not answer",
            ),
            self._check(
                "workers_healthy",
                "platform",
                ctx.workers_healthy,
                "the background workers are running",
                "the background workers are not running; execution does not happen "
                "in the browser and does not happen in the HTTP request",
            ),
            self._check(
                "audit_logging_active",
                "platform",
                ctx.audit_logging_active,
                "audit logging is active",
                "audit logging is not active; an unrecorded activation is one nobody "
                "can be asked about",
            ),
            self._check(
                "step_up_required",
                "platform",
                ctx.step_up_required,
                "step-up re-authentication is in force for dangerous actions",
                "STEP_UP_REQUIRED is false; activation would rest on a session cookie",
            ),
            # Non-mandatory. Neither can make an order unsafe, and §29 of the
            # L70 brief is explicit that notification delivery must never
            # become a dependency for order safety.
            self._check(
                "notifications_healthy",
                "platform",
                ctx.notifications_healthy,
                "notification delivery is configured and healthy",
                "notification delivery is not healthy; alerts may not arrive, but "
                "trading safety does not depend on them",
                mandatory=False,
                unknown="notification delivery state was not supplied",
            ),
            self._check(
                "release_stamped",
                "platform",
                ctx.release_stamped,
                "the running image carries a commit and a build time",
                "the running image is not stamped; a release you cannot identify is "
                "one you cannot roll back to",
                mandatory=False,
                unknown="release stamping state was not supplied",
            ),
        ]


def _warning(check: Check) -> Check:
    """Downgrade a failure to a warning. Only legal for non-mandatory checks."""
    if check.mandatory:
        raise ValueError(f"{check.name} is mandatory and may not be downgraded to a warning")
    return Check(check.name, Verdict.warning, check.detail, check.group, mandatory=False)


def soften_optional_failures(report: GateReport) -> GateReport:
    """Report non-mandatory failures as warnings.

    Presentation only, and it cannot change a verdict: `ready` already ignores
    non-mandatory checks, and `_warning` refuses to touch a mandatory one.
    """
    softened = tuple(
        _warning(c) if (not c.mandatory and c.verdict is Verdict.failed) else c
        for c in report.checks
    )
    return GateReport(checks=softened, generated_at=report.generated_at)


def summarise(checks: Sequence[Check]) -> Mapping[str, list[str]]:
    """Check names by group, for a report that has to be read by a person."""
    out: dict[str, list[str]] = {}
    for check in checks:
        out.setdefault(check.group, []).append(check.name)
    return out
