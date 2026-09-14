#!/usr/bin/env python
"""The Risk Engine, reachable from the harness. **P2: fence Path A.**

The audit's first critical finding was that this repository has two
independent paths to a broker order, and the safety machinery is on the one
that has never traded:

    Path B   webhook -> RiskEngine -> Approval -> OMS -> MT5Adapter -> venue
    Path A   take_profit.py -> mt5_paper.place() -> venue

Path A imported nothing from ``app/``. No RiskEngine, no kill switch, no
journal -- and it is the only path that has ever opened a position on this
account. On the night of 2026-09-10 it opened 21 of them, hours after three
new vetoes were added to an engine it could not see.

This module is the seam. It exists because ``app/risk/engine.py`` turned out
to be **pure**: stdlib only, synchronous, no SQLAlchemy, no settings, no
session. It takes dataclasses and returns a verdict. So the harness does not
need the platform running, a database, or an event loop -- it needs
``backend/`` on ``sys.path`` and the numbers it already reads from MT5.

**What this is not.** It is not a second risk engine. Every limit, every
threshold and every veto code is the platform's; this file only carries the
venue's answers in and the verdict back out. If the two ever disagree, the
engine is right and this file has a bug.

Three rules are taken from the engine's own docstring and are load-bearing
here:

  **Unknown is not permission.** If the engine cannot be imported, an opening
  order is REFUSED. The harness runs on 3.14 where it imports cleanly; CI also
  runs these gates on 3.10, where ``StrEnum`` and ``datetime.UTC`` do not
  exist and the import genuinely fails. A fence that disappears on the
  interpreter that cannot load it is not a fence.

  **A close is never refused.** The mirror of the same rule, and the reason
  `record_close` can only return approval. Every limit here bounds the risk of
  TAKING a position; applying them to a close would refuse to reduce exposure
  at the moment exposure is highest, and would trap the `--flat-by` flush
  behind the very loss limit that makes flushing urgent. `approve_close` in
  the engine says this at greater length. A close that cannot be evaluated is
  still sent, and the record says the evaluation was unavailable.

  **Every decision is recorded, approvals included.** A veto that leaves no
  trace is indistinguishable from a check that never ran. Each verdict returns
  a `record` that `mt5_paper` writes into `data/paper_trades.jsonl` beside the
  order it decided about.

**The overlap with `cycle()` is deliberate and is not a second implementation.**
`mt5_paper.cycle` still checks the daily loss and the losing streak before it
proposes anything. Those are session controls: they decide whether to keep
running, and they ran correctly through the night of 2026-09-10. The engine
decides about an ORDER. Both read the same two measured numbers --
`realised_today()` and `consecutive_losses(closed_outcomes())` -- so they
cannot disagree about a threshold, only about scope.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal

import paths as _paths

# The engine lives in `backend/app`, which is NOT inside the frozen bundle --
# it is imported from the checkout on disk. So the root has to come from
# `paths`, which searches for it, and not from `__file__`, which when frozen
# points into PyInstaller's extraction directory.
#
# Getting this wrong is not a crash. The import below fails,
# `ENGINE_IMPORT_ERROR` is set, and every open is refused with RISK_VETO while
# the session otherwise appears to run normally. Observed 2026-09-14: 27
# consecutive vetoes, no orders sent, and the session still exited 0.
ROOT = _paths.project_root()
BACKEND = os.path.join(ROOT, "backend")

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

#: Why the engine is unavailable, or None when it loaded. Never swallowed:
#: a refusal has to be able to say what refused it.
ENGINE_IMPORT_ERROR: str | None = None

try:
    from app.risk.engine import (
        KillSwitches,
        OrderProposal,
        PortfolioState,
        RiskDecision,
        RiskEngine,
        RiskLimits,
    )
except Exception as exc:  # noqa: BLE001 - an unimportable engine is a refusal, not a crash
    ENGINE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    KillSwitches = OrderProposal = PortfolioState = None  # type: ignore[assignment]
    RiskDecision = RiskEngine = RiskLimits = None  # type: ignore[assignment]


class _ApprovedUpstream:
    """The platform path's marker, and the only way past `place()` without a gate.

    `app/brokers/mt5.py` calls `mt5_paper.place()` too, and it is NOT ungated:
    its caller is `app/oms/service.py`, whose `create()` takes an `Approval` as
    an argument, and an `Approval` can be built nowhere but `RiskEngine`. By
    the time Path B reaches order construction the engine has already ruled.

    Evaluating it a second time here would be worse than useless. It would
    assess a different portfolio snapshot against limits configured for the
    harness, and a disagreement would refuse an order the platform had already
    approved, booked and recorded.

    So this names the exemption instead of hiding it. `place()` refuses a live
    order when `gate is None`; it proceeds for this sentinel; and there is no
    third way through.
    """

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<APPROVED_UPSTREAM: Path B, approved by RiskEngine before the OMS>"


APPROVED_UPSTREAM = _ApprovedUpstream()


@dataclass(frozen=True)
class Decision:
    """What the harness needs to act on, flattened out of a `RiskVerdict`."""

    approved: bool
    #: A breached daily loss, weekly loss or drawdown stops the SESSION, not
    #: just this order: the next order would breach it too. A losing streak
    #: does not -- it clears itself on the next win, and halting would stop the
    #: session managing the positions the streak just produced.
    halt: bool
    reason: str
    decision_id: str = ""
    #: Limits that were evaluated and deliberately not applied, by name. An
    #: unconfigured limit lands here; so does one the engine waived on a close.
    #: "not enforced" must never read as "passed".
    not_enforced: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    #: Written into the order log beside the order it decided about.
    record: dict = field(default_factory=dict)


def _unavailable_record(kind: str) -> dict:
    return {
        "engine": "unavailable",
        "kind": kind,
        "import_error": ENGINE_IMPORT_ERROR,
    }


def _dec(value):
    """Decimal from a venue float, or None. Never 0.0 for a missing figure.

    `str()` first, deliberately: `Decimal(0.1)` is 0.1000000000000000055511...
    and a limit comparison against that is arithmetic nobody can reproduce.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a figure that will not convert is a gap
        return None


@dataclass
class Gate:
    """One session's risk configuration, plus the venue state it last saw.

    Built once per session from the harness's own flags, and told what the
    venue said once per cycle. `open()` is then a pure call into the engine.
    """

    account_id: str
    #: `mode` is the engine's `trading_mode` check and it accepts only "paper"
    #: and "demo". `assert_demo` reports "DEMO", so this is lowercased at
    #: construction rather than trusted -- a case mismatch would fail closed
    #: and refuse the whole session with a message about a trading mode.
    mode: str = "demo"
    max_daily_loss: float | None = None
    max_consecutive_losses: int | None = None
    max_open_positions: int | None = None
    max_risk_per_trade: float | None = None
    require_stop_loss: bool = True

    equity: float | None = None
    balance: float | None = None
    margin_used: float | None = None
    margin_free: float | None = None
    realised_today: float | None = None
    consecutive_losses: int | None = None
    open_positions: int | None = None
    open_symbols: tuple = ()

    def __post_init__(self) -> None:
        self.mode = (self.mode or "").strip().lower()
        self._engine = None
        if RiskEngine is not None:
            self._engine = RiskEngine(
                RiskLimits(
                    max_daily_loss=_dec(self.max_daily_loss),
                    max_consecutive_losses=self.max_consecutive_losses or None,
                    max_open_positions=self.max_open_positions or None,
                    max_risk_per_trade=_dec(self.max_risk_per_trade),
                    require_stop_loss=self.require_stop_loss,
                    one_position_per_symbol=True,
                ),
                # EMPTY ON PURPOSE, and this is the one place in the file
                # where the engine is deliberately told less than is known.
                #
                # `tools/kill_switch.py` exists and the harness honours it --
                # but as a WIND-DOWN: `take_profit.run` breaks to the flush and
                # leaves `halted` false, so the `--flat-by` close still runs.
                # The engine's kill switch is a HALT, and `run()` skips the
                # flush after a halt, on the reasoning that a daily-loss limit
                # firing at 22:00 is not a reason to close hours early.
                #
                # Feeding the stop file in here would therefore convert an
                # operator stop that flushes into one that abandons every open
                # position -- the exact failure the flush exists to prevent.
                # The switch stays where it already works.
                KillSwitches(),
            )

    @property
    def available(self) -> bool:
        return self._engine is not None

    def observe(self, *, account=None, positions=None, realised_today=None,
                consecutive_losses=None):
        """Take the venue's answers. Every one of them may be missing.

        A figure that could not be read stays None, and a limit that needs it
        then vetoes. It is never defaulted to zero -- zero is a measurement,
        and "no loss today" is exactly the answer that must not be invented
        from a dropped terminal. That defect ran 23 blind passes in August, and
        this is the same class of mistake one layer up.
        """
        if account is not None:
            self.equity = getattr(account, "equity", None)
            self.balance = getattr(account, "balance", None)
            self.margin_used = getattr(account, "margin", None)
            self.margin_free = getattr(account, "margin_free", None)
        if positions is not None:
            self.open_positions = len(positions)
            self.open_symbols = tuple(p.symbol for p in positions)
        self.realised_today = realised_today
        self.consecutive_losses = consecutive_losses
        return self

    def _portfolio(self):
        return PortfolioState(
            equity=_dec(self.equity),
            balance=_dec(self.balance),
            margin_used=_dec(self.margin_used),
            margin_free=_dec(self.margin_free),
            open_positions=self.open_positions,
            open_symbols=frozenset(self.open_symbols),
            realised_today=_dec(self.realised_today),
            consecutive_losses=self.consecutive_losses,
        )

    def open(self, *, symbol, side, volume, entry_price=None, stop_loss=None,
             take_profit=None, risk_amount=None, spread_points=None) -> Decision:
        """Rule on ONE opening order. Refuses when it cannot rule."""
        if self._engine is None:
            return Decision(
                approved=False,
                halt=False,
                reason=(
                    "the risk engine could not be loaded, so this order was not "
                    f"evaluated: {ENGINE_IMPORT_ERROR}"
                ),
                record=_unavailable_record("open"),
            )

        proposal = OrderProposal(
            symbol=symbol,
            side=side,
            mode=self.mode,
            volume=_dec(volume),
            entry_price=_dec(entry_price),
            # 0.0 is MT5's "no bracket", and passing it as a price would tell
            # the engine a stop exists at zero. `place()` uses the same
            # convention; here it has to become the absence it means.
            stop_loss=_dec(stop_loss) if stop_loss else None,
            take_profit=_dec(take_profit) if take_profit else None,
            risk_amount=_dec(risk_amount),
            spread_points=_dec(spread_points),
            account_id=self.account_id,
            strategy_id="take_profit",
        )
        approval, verdict = self._engine.approve(proposal, self._portfolio())
        return _decision_from(verdict, approval, kind="open")

    def close(self, *, symbol, side, volume, entry_price=None) -> Decision:
        """Record a closing order. See `record_close` -- this can only approve."""
        return record_close(
            symbol=symbol, side=side, volume=volume, entry_price=entry_price,
            account_id=self.account_id, mode=self.mode,
        )


def _decision_from(verdict, approval, kind: str) -> Decision:
    halting = verdict.decision is RiskDecision.halt
    failed = tuple(str(c.limit) for c in verdict.failed)
    record = {
        "engine": "RiskEngine",
        "kind": kind,
        "decision": str(verdict.decision),
        "reason": verdict.reason[:400],
        "decision_id": approval.decision_id if approval is not None else "",
        "checks": len(verdict.checks),
        "failed": list(failed),
        "not_enforced": list(verdict.not_enforced),
    }
    return Decision(
        approved=approval is not None,
        halt=halting,
        reason=verdict.reason,
        decision_id=approval.decision_id if approval is not None else "",
        not_enforced=tuple(verdict.not_enforced),
        failed=failed,
        record=record,
    )


def record_close(*, symbol, side, volume, entry_price=None,
                 account_id: str = "", mode: str = "demo") -> Decision:
    """Evaluate and RECORD a close. It cannot refuse one, and that is the point.

    `RiskEngine.approve_close` evaluates every limit, records what failed, and
    approves anyway -- because a limit that bounds the risk of opening must
    never be the reason a position cannot be shut. The one check that still
    bites there is the trading-mode fence, and `assert_demo` has already
    refused anything but a demo account before this module is reached.

    So the value here is the record, not the control, and the function is
    written so that nothing it does can stop a close: an unimportable engine,
    a malformed figure or an unexpected exception all return an approval whose
    record says the evaluation did not happen. The `--flat-by` flush runs
    through this, and the flush is the one mechanism bounding a losing tail
    overnight. It does not get a new way to fail.
    """
    if RiskEngine is None:
        return Decision(True, False, "risk engine unavailable; a close is never blocked",
                        record=_unavailable_record("close"))
    try:
        engine = RiskEngine(
            # A close needs neither a stop nor an opinion about how many
            # positions the symbol already has -- it is removing one. Left at
            # the defaults, `require_stop_loss` would fail on every close and
            # be overridden by `approve_close`, filling the record with a
            # waived limit that was never relevant.
            RiskLimits(require_stop_loss=False, one_position_per_symbol=False),
            KillSwitches(),
        )
        proposal = OrderProposal(
            symbol=symbol,
            side=side,
            mode=(mode or "demo").strip().lower(),
            volume=_dec(volume),
            entry_price=_dec(entry_price),
            account_id=account_id,
            strategy_id="take_profit",
        )
        approval, verdict = engine.approve_close(proposal, PortfolioState())
        decision = _decision_from(verdict, approval, kind="close")
        if decision.approved:
            return decision
        # Only the mode fence and a missing volume reach here. Both are real,
        # and neither may stop a close: `assert_demo` is the mode fence that
        # matters and it has already passed, and a volume this module could
        # not convert is this module's problem, not a reason to hold a
        # position open. Recorded loudly and overridden.
        record = dict(decision.record)
        record["override"] = "a close is never blocked by this module"
        return Decision(True, False, f"close proceeds over: {decision.reason}"[:400],
                        not_enforced=decision.not_enforced, failed=decision.failed,
                        record=record)
    except Exception as exc:  # noqa: BLE001 - see the docstring: never block a close
        record = _unavailable_record("close")
        record["raised"] = f"{type(exc).__name__}: {exc}"[:200]
        return Decision(True, False, "close evaluation raised; the close proceeds",
                        record=record)


def gate_for(args, account=None) -> Gate:
    """Build the session's gate from the harness's own flags.

    Only the limits the harness can actually FEED are configured. The engine
    reports everything else as `not_enforced`, which is the honest answer and
    reaches the order log: weekly loss and correlated exposure have no data
    source on this path yet, and an unfed limit that approved would read in an
    audit as one that was checked.
    """
    account = account or {}
    return Gate(
        account_id=str(account.get("login", "")),
        mode=str(account.get("mode", "demo")),
        max_daily_loss=getattr(args, "max_daily_loss", None),
        max_consecutive_losses=getattr(args, "max_consecutive_losses", 0) or None,
        max_open_positions=getattr(args, "max_positions", None),
        max_risk_per_trade=getattr(args, "max_risk_per_trade", None),
    )
