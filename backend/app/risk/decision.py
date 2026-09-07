"""The risk decision: its codes, its identity, and what it is bound to.

Three ideas live here.

**A rejection names itself.** `RejectionCode` is a closed vocabulary, so a
caller never gets back "risk rejected" and has to guess. Every code maps from a
`LimitKind`, and a test asserts the map is total -- a limit added later without
a code fails the build rather than falling back to a generic string.

**A decision has an identity.** `decision_id` is a uuid, so an approval can be
referenced from an order, an audit row and a log line and be the same object in
all three.

**An approval is bound to the order it approved.** `request_hash` is a digest
of the fields that make the order what it is -- account, symbol, side, quantity,
type, price, stop, target, strategy, execution mode. If any of them changes
after approval, the hash changes and the OMS refuses the approval as belonging
to a different order. That closes the gap where risk approves order A and
something executes a modified order B.

Approvals also **expire**. A decision made against a portfolio snapshot ten
minutes ago is not evidence about now, and an approval with no expiry is an
approval that can be replayed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

# How long an approval stands. Short: the portfolio state it was computed
# against moves, and a stale approval is a decision about a world that has gone.
APPROVAL_TTL_SECONDS = 60


class RiskOutcome(StrEnum):
    """The five results. `error` is a result, not an exception to swallow."""

    approved = "APPROVED"
    approved_with_adjustment = "APPROVED_WITH_ADJUSTMENT"
    rejected = "REJECTED"
    halted = "HALTED"
    error = "ERROR"


# Outcomes that permit an order. Deliberately a frozenset rather than
# `!= rejected`, so a new outcome is refused until someone decides about it.
PERMITTING = frozenset({RiskOutcome.approved, RiskOutcome.approved_with_adjustment})


class RejectionCode(StrEnum):
    """Why an order was refused, in a word a caller can branch on."""

    kill_switch_active = "KILL_SWITCH_ACTIVE"
    emergency_stop = "EMERGENCY_STOP"
    trading_mode_blocked = "TRADING_MODE_BLOCKED"
    live_trading_disabled = "LIVE_TRADING_DISABLED"
    account_disabled = "ACCOUNT_DISABLED"
    account_state_invalid = "ACCOUNT_STATE_INVALID"
    bot_not_running = "BOT_NOT_RUNNING"
    strategy_disabled = "STRATEGY_DISABLED"
    invalid_symbol = "INVALID_SYMBOL"
    symbol_not_tradable = "SYMBOL_NOT_TRADABLE"
    market_closed = "MARKET_CLOSED"
    market_data_stale = "MARKET_DATA_STALE"
    market_data_invalid = "MARKET_DATA_INVALID"
    spread_too_wide = "SPREAD_TOO_WIDE"
    invalid_quantity = "INVALID_QUANTITY"
    invalid_stop_loss = "INVALID_STOP_LOSS"
    stop_loss_required = "STOP_LOSS_REQUIRED"
    risk_reward_too_low = "RISK_REWARD_TOO_LOW"
    max_position_size_exceeded = "MAX_POSITION_SIZE_EXCEEDED"
    max_open_positions_exceeded = "MAX_OPEN_POSITIONS_EXCEEDED"
    position_already_open = "POSITION_ALREADY_OPEN"
    max_exposure_exceeded = "MAX_EXPOSURE_EXCEEDED"
    max_concentration_exceeded = "MAX_CONCENTRATION_EXCEEDED"
    max_risk_per_trade_exceeded = "MAX_RISK_PER_TRADE_EXCEEDED"
    max_daily_loss_exceeded = "MAX_DAILY_LOSS_EXCEEDED"
    daily_loss_locked = "DAILY_LOSS_LOCKED"
    max_drawdown_exceeded = "MAX_DRAWDOWN_EXCEEDED"
    drawdown_locked = "DRAWDOWN_LOCKED"
    max_leverage_exceeded = "MAX_LEVERAGE_EXCEEDED"
    insufficient_margin = "INSUFFICIENT_MARGIN"
    trade_frequency_exceeded = "TRADE_FREQUENCY_EXCEEDED"
    cooldown_active = "COOLDOWN_ACTIVE"
    duplicate_signal = "DUPLICATE_SIGNAL"
    signal_too_old = "SIGNAL_TOO_OLD"
    invalid_risk_configuration = "INVALID_RISK_CONFIGURATION"
    risk_engine_error = "RISK_ENGINE_ERROR"


class WarningCode(StrEnum):
    """Advisory. A warning NEVER becomes an approval on its own, and a check
    that the policy defines as a hard block is never demoted to one."""

    high_spread = "HIGH_SPREAD_WARNING"
    high_concentration = "HIGH_CONCENTRATION_WARNING"
    near_daily_loss = "NEAR_DAILY_LOSS_LIMIT"
    near_drawdown = "NEAR_DRAWDOWN_LIMIT"
    limit_not_enforced = "LIMIT_NOT_ENFORCED"
    quantity_reduced = "QUANTITY_REDUCED"


# The fields that make an order the order that was approved. A change to any of
# them produces a different hash and therefore requires a fresh evaluation.
BOUND_FIELDS = (
    "account_id",
    "symbol",
    "side",
    "volume",
    "order_type",
    "entry_price",
    "stop_loss",
    "take_profit",
    "strategy_id",
    "mode",
)


def request_hash(fields: Mapping[str, object]) -> str:
    """A stable digest of the order's identity-bearing fields.

    Sorted and rendered as strings so two dicts that mean the same order hash
    the same, and `Decimal("1.0")` and `Decimal("1.00")` do not silently differ
    -- they are normalised first, because they are the same quantity.
    """
    parts = []
    for key in BOUND_FIELDS:
        value = fields.get(key)
        if isinstance(value, Decimal):
            rendered = format(value.normalize(), "f")
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class CheckRecord:
    """One check's result. `enforced=False` means the limit was not configured,
    which is reported so "not enforced" never reads as "passed"."""

    name: str
    passed: bool
    detail: str
    enforced: bool = True
    code: RejectionCode | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "check": self.name,
            "passed": self.passed,
            "enforced": self.enforced,
            "detail": self.detail,
            "code": str(self.code) if self.code else None,
        }


@dataclass(frozen=True)
class RiskDecision:
    """The structured result of one evaluation. Immutable once made.

    Recorded whether it approved or refused: a veto that leaves no trace is
    indistinguishable from a check that never ran.
    """

    outcome: RiskOutcome
    at: datetime
    request_hash: str
    checks: tuple[CheckRecord, ...]
    account_id: str | None = None
    strategy_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    execution_mode: str = "paper"
    requested_quantity: Decimal | None = None
    approved_quantity: Decimal | None = None
    reason: str = ""
    codes: tuple[RejectionCode, ...] = ()
    warnings: tuple[str, ...] = ()
    configuration_version: int = 0
    decision_id: str = field(default_factory=lambda: str(uuid4()))
    expires_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.outcome in PERMITTING

    @property
    def failed(self) -> tuple[CheckRecord, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def not_enforced(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if not c.enforced)

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now > self.expires_at

    def binds(self, fields: Mapping[str, object]) -> bool:
        """True when this decision approved exactly this order."""
        return request_hash(fields) == self.request_hash

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "outcome": str(self.outcome),
            "approved": self.approved,
            "at": self.at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "request_hash": self.request_hash,
            "account_id": self.account_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "execution_mode": self.execution_mode,
            "requested_quantity": (
                str(self.requested_quantity) if self.requested_quantity is not None else None
            ),
            "approved_quantity": (
                str(self.approved_quantity) if self.approved_quantity is not None else None
            ),
            "reason": self.reason,
            "codes": [str(c) for c in self.codes],
            "warnings": list(self.warnings),
            "configuration_version": self.configuration_version,
            "checks": [c.as_dict() for c in self.checks],
            "not_enforced": list(self.not_enforced),
        }


def expiry(at: datetime, seconds: int = APPROVAL_TTL_SECONDS) -> datetime:
    return at + timedelta(seconds=seconds)
