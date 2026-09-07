"""Two contexts, and the wall between them. Sections 18, 19 and 63.

This is the module the level turns on.

    DecisionContext   everything the platform knew AT OR BEFORE the entry.
    OutcomeContext    everything that happened after.

**Decision-quality assessors receive only the first.** Not by convention and not
by a reviewer remembering -- by signature. `checks.entry_quality` takes a
`DecisionContext` and there is no outcome on it to read, so a rating cannot be
influenced by the result even by accident. §63 asks for a test proving the entry
assessment does not gain access to future information when post-entry data
changes; that test passes because the function is not given the data.

**The reverse direction is allowed and is the point.** §19: the review MAY use
post-trade information to explain the OUTCOME. "Price moved 0.8% against the
position after entry before recovering" is a legitimate outcome observation. What
it may not do is conclude "therefore the entry was poor", and the split is what
makes the second sentence unwriteable in the section that rates the entry.

**Nothing is fabricated to fill a gap.** §5. Every field is optional, a missing
one is `None`, and `Completeness` records which were absent -- so a review of a
trade with no strategy record says so and scores low confidence, rather than
reading fluently about a strategy nobody wrote down.

The naming is deliberate: `DecisionContext` says what it is FOR, not what it
contains. A field added to it later is a claim that the platform knew that thing
before the entry, and that claim should be uncomfortable to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.review.contract import Completeness


@dataclass(frozen=True)
class DecisionContext:
    """What was known at or before the entry. Section 19.

    **Every field here must have been recorded at or before `entry_at`.** That
    is the invariant, and it is the reason `exit_price`, `net_profit`,
    `exit_reason` and `mae`/`mfe` are absent rather than merely unused: a field
    that exists can be read, and a field that can be read will eventually be
    read by a section that should not.
    """

    trade_id: str
    environment: str
    symbol: str | None = None
    side: str | None = None
    entry_at: datetime | None = None
    entry_price: Decimal | None = None
    requested_entry_price: Decimal | None = None
    quantity: Decimal | None = None

    #: The protective levels as PLANNED. §16 -- the ones the strategy and the
    #: risk engine agreed on, read from the position record, not from wherever
    #: the trade ended up.
    planned_stop_loss: Decimal | None = None
    planned_take_profit: Decimal | None = None

    #: Strategy identity and its recorded definition. §53: the version that
    #: produced THIS trade, never the strategy's current version.
    strategy_version_id: str | None = None
    strategy_definition: dict[str, Any] | None = None
    timeframe: str | None = None

    #: §54. The chain back to TradingView, where there is one.
    signal_id: str | None = None
    signal_source: str | None = None
    signal_confidence: Decimal | None = None
    signal_at: datetime | None = None
    tradingview_event_id: str | None = None

    #: §20 and §52. The prediction as it stood BEFORE the order, at an exact
    #: version.
    ai_decision: str | None = None
    ai_probability: Decimal | None = None
    ai_confidence: Decimal | None = None
    ai_regime: str | None = None
    ai_anomaly_score: Decimal | None = None
    prediction_model: str | None = None
    prediction_model_version: str | None = None
    ai_mode: str | None = None

    #: §15. The risk engine's own snapshot, written when it decided.
    risk_decision: str | None = None
    risk_reason: str | None = None
    risk_snapshot: dict[str, Any] | None = None
    risk_configuration_version: str | None = None

    #: §16. The sizing calculation as recorded at submission.
    sizing: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "environment": self.environment,
            "symbol": self.symbol,
            "side": self.side,
            "entry_at": self.entry_at.isoformat() if self.entry_at else None,
            "entry_price": _s(self.entry_price),
            "requested_entry_price": _s(self.requested_entry_price),
            "quantity": _s(self.quantity),
            "planned_stop_loss": _s(self.planned_stop_loss),
            "planned_take_profit": _s(self.planned_take_profit),
            "strategy_version_id": self.strategy_version_id,
            "strategy_definition": self.strategy_definition,
            "timeframe": self.timeframe,
            "signal": {
                "id": self.signal_id,
                "source": self.signal_source,
                "confidence": _s(self.signal_confidence),
                "at": self.signal_at.isoformat() if self.signal_at else None,
                "tradingview_event_id": self.tradingview_event_id,
            },
            "ai": {
                "decision": self.ai_decision,
                "probability": _s(self.ai_probability),
                "confidence": _s(self.ai_confidence),
                "regime": self.ai_regime,
                "anomaly_score": _s(self.ai_anomaly_score),
                "model": self.prediction_model,
                "model_version": self.prediction_model_version,
                "mode": self.ai_mode,
            },
            "risk": {
                "decision": self.risk_decision,
                "reason": self.risk_reason,
                "snapshot": self.risk_snapshot,
                "configuration_version": self.risk_configuration_version,
            },
            "sizing": self.sizing,
            "boundary": (
                "everything here was recorded at or before the entry. Decision-quality "
                "assessment receives ONLY this -- the outcome is not on this object to "
                "be read."
            ),
        }


@dataclass(frozen=True)
class OutcomeContext:
    """What happened after the entry. Section 19.

    Legitimate for explaining the RESULT, and never handed to the assessors that
    rate the decision. `exit_quality` is the one borderline case and it is
    handled explicitly: an exit is itself a decision, so it is rated against the
    PLANNED levels carried in `DecisionContext`, not against how far price
    eventually travelled.
    """

    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str | None = None
    closes: int = 0
    gross_profit: Decimal | None = None
    net_profit: Decimal | None = None
    commission: Decimal | None = None
    swap: Decimal | None = None
    fees: Decimal | None = None
    r_multiple: Decimal | None = None
    currency: str | None = None

    #: §17. What the venue actually did.
    fills: int = 0
    slippage_points: list[Decimal] = field(default_factory=list)
    submit_latency_seconds: float | None = None
    fill_latency_seconds: float | None = None
    partial_fill: bool = False
    broker_position_id: str | None = None

    #: §4. MAE and MFE need intratrade price data. `None` here is honest: §5
    #: says an unavailable figure is null, never an estimate.
    mae: Decimal | None = None
    mfe: Decimal | None = None

    #: §44 of L31 -- what the journal flagged about this row.
    data_quality: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_at": self.exit_at.isoformat() if self.exit_at else None,
            "exit_price": _s(self.exit_price),
            "exit_reason": self.exit_reason,
            "closes": self.closes,
            "gross_profit": _s(self.gross_profit),
            "net_profit": _s(self.net_profit),
            "commission": _s(self.commission),
            "swap": _s(self.swap),
            "fees": _s(self.fees),
            "r_multiple": _s(self.r_multiple),
            "currency": self.currency,
            "fills": self.fills,
            "slippage_points": [str(v) for v in self.slippage_points],
            "submit_latency_seconds": self.submit_latency_seconds,
            "fill_latency_seconds": self.fill_latency_seconds,
            "partial_fill": self.partial_fill,
            "broker_position_id": self.broker_position_id,
            "mae": _s(self.mae),
            "mfe": _s(self.mfe),
            "mae_mfe_note": (
                "null because this deployment records no intratrade price series. "
                "An estimate would be indistinguishable from a measurement."
            ),
            "data_quality": self.data_quality,
            "boundary": (
                "everything here happened AFTER the entry. It may explain the outcome "
                "and may not be used to judge the decision."
            ),
        }


@dataclass(frozen=True)
class ReviewInput:
    """The two contexts together, plus the historical baselines. Section 4.

    The baselines come from L32 analytics (§50) and are deliberately NOT
    recomputed here: expectancy, execution medians and regime performance are
    the analytics engine's, and a second computation would eventually disagree
    with the dashboard showing the first.
    """

    decision: DecisionContext
    outcome: OutcomeContext
    #: §50. Historical context for the same strategy/symbol, from analytics.
    baselines: dict[str, Any] = field(default_factory=dict)
    completeness: Completeness = field(default_factory=Completeness)
    built_at: datetime | None = None

    @property
    def trade_id(self) -> str:
        return self.decision.trade_id

    @property
    def environment(self) -> str:
        return self.decision.environment

    def as_dict(self) -> dict[str, Any]:
        """The input snapshot §32 asks for, stored beside the review.

        **No credential is in it**, and there is nothing to redact: the two
        contexts carry prices, quantities, identifiers and recorded decisions,
        and no field on either has ever held a secret. §45.
        """
        return {
            "decision_context": self.decision.as_dict(),
            "outcome_context": self.outcome.as_dict(),
            "baselines": self.baselines,
            "completeness": self.completeness.as_dict(),
            "built_at": self.built_at.isoformat() if self.built_at else None,
            "separation": (
                "the two contexts are separate objects, not two halves of one. A "
                "decision-quality assessor is handed the decision context alone, so it "
                "cannot read the outcome even by mistake -- section 19 and section 63."
            ),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


#: The fields whose presence or absence `Completeness` tracks. Named as a table
#: rather than derived, so adding a field to a context is a deliberate decision
#: about whether its absence should lower a review's confidence.
TRACKED: tuple[tuple[str, str], ...] = (
    ("entry_price", "decision"),
    ("planned_stop_loss", "decision"),
    ("planned_take_profit", "decision"),
    ("quantity", "decision"),
    ("strategy_version_id", "decision"),
    ("strategy_definition", "decision"),
    ("signal_id", "decision"),
    ("ai_decision", "decision"),
    ("risk_decision", "decision"),
    ("risk_snapshot", "decision"),
    ("sizing", "decision"),
    ("requested_entry_price", "decision"),
    ("exit_price", "outcome"),
    ("exit_reason", "outcome"),
    ("net_profit", "outcome"),
    ("r_multiple", "outcome"),
    ("mae", "outcome"),
    ("mfe", "outcome"),
    ("submit_latency_seconds", "outcome"),
    ("fill_latency_seconds", "outcome"),
)


def completeness_of(decision: DecisionContext, outcome: OutcomeContext) -> Completeness:
    """Which tracked fields were recorded, and which were not. Section 22."""
    found = Completeness()
    for name, where in TRACKED:
        holder = decision if where == "decision" else outcome
        value = getattr(holder, name, None)
        (found.available if value is not None else found.missing).append(f"{where}.{name}")
    return found


__all__ = [
    "TRACKED",
    "DecisionContext",
    "OutcomeContext",
    "ReviewInput",
    "completeness_of",
]
