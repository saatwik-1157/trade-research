"""The deterministic assessors. Section 36.

*"Do NOT ask an LLM to calculate values that can be deterministically
calculated."* So every rating in a review is produced here, from recorded rows,
and the narrative layer is handed the result rather than the raw trade.

**Four of the five assessors cannot see the outcome.** Their signatures take a
`DecisionContext` and nothing else. §13's rule -- a losing trade can have a
high-quality entry -- is not a guideline they follow; it is a property of what
they were given.

`exit_quality` is the exception and is handled explicitly. An exit is itself a
decision, so it is rated against the levels that were PLANNED before entry, not
against how far price eventually travelled. It receives the outcome because it
must know where the trade actually left, and it never compares that to a high or
low it could have reached.

**UNKNOWN whenever the input was not recorded.** §12 for compliance and the same
principle everywhere else. A missing strategy definition is not a non-compliant
trade; a missing sizing record is not incorrect sizing. Each unrated section
names the field it needed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.review.context import DecisionContext, OutcomeContext
from app.review.contract import (
    Compliance,
    Rating,
    Section,
    hypothesis,
    interpreted,
    observed,
    unrated,
)

ZERO = Decimal("0")

#: §17. Above this, slippage is called out. Not a threshold anybody measured as
#: meaningful -- it is the point at which `CLAUDE.md`'s own live record says a
#: fill stopped being ordinary: 3 points was the worst across 26 trades, and the
#: 27th was 279.
NOTABLE_SLIPPAGE_POINTS = Decimal("10")

#: §17. Above this many seconds from submit to fill, the latency is worth a
#: line. A market order that took longer than this was queued, not routed.
NOTABLE_FILL_LATENCY_SECONDS = 5.0


# ============================================================ strategy, §12


def strategy_alignment(decision: DecisionContext) -> tuple[Section, Compliance]:
    """Did the trade match the strategy version that produced it? Section 12.

    Only the machine-checkable parts, and only where the definition was
    recorded. §36 assigns "strategy compliance where machine-checkable" to the
    deterministic side; the rest is a follow-up question rather than a verdict.
    """
    if decision.strategy_version_id is None:
        return (
            unrated(
                "no strategy version is linked to this trade, so there is no definition "
                "to check it against. That is a gap in the record, NOT a non-compliant "
                "trade -- section 12 forbids that conflation."
            ),
            Compliance.unknown,
        )

    section = Section(
        evidence=[
            observed(
                f"the trade is attributed to strategy version {decision.strategy_version_id}.",
                "trades.strategy_version_id",
            )
        ]
    )

    definition = decision.strategy_definition
    if not definition:
        section.rating = Rating.unknown
        section.unavailable_reason = (
            "the strategy version is linked but its recorded definition could not be "
            "read, so entry, exit and timeframe rules cannot be checked."
        )
        return section, Compliance.unknown

    checked: list[bool] = []

    declared_timeframe = definition.get("timeframe")
    if declared_timeframe and decision.timeframe:
        matches = str(declared_timeframe) == str(decision.timeframe)
        checked.append(matches)
        section.evidence.append(
            observed(
                f"the strategy declares timeframe {declared_timeframe} and the trade "
                f"was taken on {decision.timeframe}.",
                "strategy_versions.config.timeframe",
            )
        )
        if not matches:
            section.warnings.append(
                f"timeframe mismatch: the strategy declares {declared_timeframe} and "
                f"the trade records {decision.timeframe}."
            )

    declared_symbol = definition.get("symbol")
    if declared_symbol and decision.symbol:
        matches = str(declared_symbol).upper() == str(decision.symbol).upper()
        checked.append(matches)
        section.evidence.append(
            observed(
                f"the strategy declares symbol {declared_symbol} and the trade is on "
                f"{decision.symbol}.",
                "strategy_versions.config.symbol",
            )
        )
        if not matches:
            section.warnings.append(
                f"symbol mismatch: the strategy declares {declared_symbol} and the "
                f"trade records {decision.symbol}."
            )

    # A strategy with exit rules should produce a trade that had protective
    # levels. Checkable; the CONTENT of those rules is not, from a closed trade.
    if definition.get("exit_rules"):
        has_protection = (
            decision.planned_stop_loss is not None or decision.planned_take_profit is not None
        )
        checked.append(has_protection)
        section.evidence.append(
            observed(
                "the strategy declares exit rules and the position "
                + ("carried" if has_protection else "did NOT carry")
                + " a planned stop or target.",
                "strategy_versions.config.exit_rules, positions.stop_loss/take_profit",
            )
        )
        if not has_protection:
            section.warnings.append(
                "the strategy declares exit rules but the position recorded neither a "
                "stop loss nor a take profit."
            )

    if not checked:
        section.rating = Rating.unknown
        section.unavailable_reason = (
            "the strategy definition records none of the fields a closed trade can be "
            "checked against (timeframe, symbol, exit rules)."
        )
        return section, Compliance.unknown

    if all(checked):
        section.rating = Rating.good
        section.evidence.append(
            interpreted(
                f"all {len(checked)} machine-checkable strategy constraints hold.",
                "the checks listed above",
            )
        )
        compliance = Compliance.compliant
    elif any(checked):
        section.rating = Rating.fair
        compliance = Compliance.partially_compliant
    else:
        section.rating = Rating.poor
        compliance = Compliance.non_compliant

    section.evidence.append(
        interpreted(
            "only machine-checkable constraints were evaluated. Whether the entry "
            "CONDITIONS held at the signal bar is not recoverable from a closed trade "
            "and is not asserted either way.",
        )
    )
    return section, compliance


# =============================================================== entry, §13


def entry_quality(decision: DecisionContext) -> Section:
    """Rated from what was known at entry. Section 13 and §19.

    **This function cannot see the result.** Its argument has no outcome on it,
    so the §63 test -- change the post-entry data and assert this output is
    unchanged -- passes because there is nothing to change that reaches here.
    """
    section = Section()
    if decision.entry_price is None:
        return unrated("no entry price is recorded.")

    section.evidence.append(
        observed(f"entry filled at {decision.entry_price}.", "trades.entry_price")
    )

    signals: list[bool] = []

    # Requested against actual, at ENTRY. This is a decision-time fact: the gap
    # existed the moment the fill came back, before anything else happened.
    if decision.requested_entry_price is not None:
        gap = abs(decision.entry_price - decision.requested_entry_price)
        section.evidence.append(
            observed(
                f"the platform requested {decision.requested_entry_price} and the venue "
                f"filled {decision.entry_price}, a difference of {gap}.",
                "trades.requested_entry_price vs trades.entry_price",
            )
        )
        reference = decision.requested_entry_price
        if reference and abs(gap / reference) > Decimal("0.001"):
            signals.append(False)
            section.warnings.append(
                f"the fill differs from the requested price by {gap}, more than 0.1% "
                "of the requested level."
            )
            section.evidence.append(
                hypothesis(
                    "a fill this far from the quote the platform read is the pattern "
                    "CLAUDE.md records for the rollover window, where a one-minute bar "
                    "range can exceed the whole bracket."
                )
            )
        else:
            signals.append(True)

    if decision.planned_stop_loss is not None:
        distance = abs(decision.entry_price - decision.planned_stop_loss)
        section.evidence.append(
            observed(
                f"the planned stop was {decision.planned_stop_loss}, {distance} from the entry.",
                "positions.stop_loss",
            )
        )
        signals.append(True)
    else:
        signals.append(False)
        section.warnings.append(
            "no stop loss was planned. An unstopped position is the one whose loss has no floor."
        )

    if decision.ai_decision is not None:
        section.evidence.append(
            observed(
                f"the AI seat returned {decision.ai_decision}"
                + (
                    f" with probability {decision.ai_probability}"
                    if decision.ai_probability is not None
                    else ""
                )
                + (
                    f", from {decision.prediction_model} {decision.prediction_model_version}"
                    if decision.prediction_model
                    else ""
                )
                + ".",
                "ai_decisions",
            )
        )

    if decision.signal_id is not None:
        section.evidence.append(
            observed(
                f"the entry traces to signal {decision.signal_id}"
                + (f" from {decision.signal_source}" if decision.signal_source else "")
                + ".",
                "signals",
            )
        )
        signals.append(True)

    section.rating = _rate(signals)
    if section.rating is Rating.unknown:
        section.unavailable_reason = (
            "the entry price is recorded but nothing else the entry can be assessed "
            "against is: no requested price, no planned stop, no signal."
        )
    section.evidence.append(
        interpreted(
            "assessed from decision-time information only. The result of the trade is "
            "not an input here -- a losing trade can have a well-formed entry.",
        )
    )
    return section


# ================================================================ exit, §14


def exit_quality(decision: DecisionContext, outcome: OutcomeContext) -> Section:
    """The exit against the PLAN, never against what price later did. Section 14.

    Receives the outcome because it must know where the trade left. It compares
    that only to levels chosen before entry -- it never asks how far price
    eventually travelled, which is the future information §19 forbids using to
    judge a decision.
    """
    if outcome.exit_price is None:
        return unrated("no exit price is recorded.")

    section = Section(
        evidence=[observed(f"the position closed at {outcome.exit_price}.", "trades.exit_price")]
    )
    if outcome.exit_reason:
        section.evidence.append(
            observed(f"the recorded exit reason is {outcome.exit_reason}.", "trades.exit_reason")
        )
    if outcome.closes > 1:
        section.evidence.append(
            observed(
                f"the position closed in {outcome.closes} parts; the exit price is their "
                "volume-weighted average.",
                "position_events",
            )
        )

    signals: list[bool] = []

    planned = {
        "stop loss": decision.planned_stop_loss,
        "take profit": decision.planned_take_profit,
    }
    matched = None
    for name, level in planned.items():
        if level is None:
            continue
        distance = abs(outcome.exit_price - level)
        reference = level or Decimal("1")
        if abs(distance / reference) <= Decimal("0.0005"):
            matched = name
            break

    if matched:
        signals.append(True)
        section.evidence.append(
            interpreted(
                f"the exit landed on the planned {matched}, so the position left where "
                "it was designed to.",
                "positions.stop_loss / positions.take_profit",
            )
        )
    elif any(level is not None for level in planned.values()):
        section.evidence.append(
            interpreted(
                "the exit did not land on either planned level.",
                "positions.stop_loss / positions.take_profit",
            )
        )
        # NOT a fault by itself. A trailing stop, a strategy exit and a manual
        # close are all legitimate and none of them lands on the original level.
        if outcome.exit_reason in ("stop_loss", "take_profit"):
            signals.append(False)
            section.warnings.append(
                f"the exit reason is {outcome.exit_reason} but the exit price is not at "
                "the planned level. The two records disagree about what happened."
            )
        else:
            signals.append(True)
    else:
        section.evidence.append(
            interpreted("no protective level was planned, so there is no plan to compare against.")
        )

    if outcome.exit_reason == "unknown":
        signals.append(False)
        section.warnings.append("nothing recorded why this position was closed.")

    section.rating = _rate(signals)
    if section.rating is Rating.unknown:
        section.unavailable_reason = (
            "the exit is recorded but no planned level and no exit reason are, so there "
            "is nothing to assess it against."
        )
    section.evidence.append(
        interpreted(
            "the exit is compared to the levels planned BEFORE entry. How far price "
            "travelled afterwards is not used -- that would judge the decision with "
            "information the decision did not have.",
        )
    )
    return section


# ================================================= risk and sizing, §15, §16


def risk_quality(decision: DecisionContext) -> Section:
    """Planned risk against what the risk engine recorded. Sections 15 and 16.

    Decision-time only. Whether the trade lost more than planned is an outcome
    fact and belongs in the outcome narrative, not in a rating of whether the
    risk was set correctly.
    """
    if decision.risk_decision is None and decision.sizing is None:
        return unrated(
            "neither a risk decision nor a sizing record is linked to this trade, so "
            "there is nothing to compare. That is a gap in the record, not evidence "
            "that risk was mismanaged."
        )

    section = Section()
    signals: list[bool] = []

    if decision.risk_decision is not None:
        section.evidence.append(
            observed(
                f"the risk engine returned {decision.risk_decision}"
                + (f": {decision.risk_reason}" if decision.risk_reason else "")
                + ".",
                "risk_events.decision",
            )
        )
        signals.append(decision.risk_decision == "approve")
        if decision.risk_configuration_version:
            section.evidence.append(
                observed(
                    "the decision was taken under risk configuration "
                    f"{decision.risk_configuration_version}.",
                    "risk_events.configuration_version",
                )
            )

    if decision.risk_snapshot:
        keys = ", ".join(sorted(str(k) for k in decision.risk_snapshot)[:8])
        section.evidence.append(
            observed(
                f"the risk engine's own state at the decision recorded: {keys}.",
                "risk_events.snapshot",
            )
        )
        section.evidence.append(
            interpreted(
                "read as it was written. It is NOT re-evaluated against today's limits, "
                "which would describe the platform now and claim to describe the trade "
                "then.",
            )
        )

    if decision.sizing:
        requested = decision.sizing.get("final_quantity") or decision.sizing.get("quantity")
        section.evidence.append(
            observed(
                f"position sizing recorded: {_summarise(decision.sizing)}.",
                "orders.sizing",
            )
        )
        if requested is not None and decision.quantity is not None:
            same = _close(Decimal(str(requested)), decision.quantity)
            signals.append(same)
            section.evidence.append(
                observed(
                    f"sizing calculated {requested} and the position holds {decision.quantity}.",
                    "orders.sizing vs positions.quantity",
                )
            )
            if not same:
                section.warnings.append(
                    f"sizing calculated {requested} but the position holds "
                    f"{decision.quantity}. Investigate the execution or configuration "
                    "discrepancy; the historical record is not corrected here."
                )
    elif decision.risk_decision is not None:
        section.evidence.append(
            interpreted(
                "no sizing record is linked, so the calculated quantity cannot be "
                "compared with the executed one.",
            )
        )

    section.rating = _rate(signals)
    if section.rating is Rating.unknown:
        section.unavailable_reason = (
            "risk or sizing records exist but carry nothing comparable to the trade."
        )
    return section


# =========================================================== execution, §17


def execution_quality(outcome: OutcomeContext) -> Section:
    """What the venue did. Section 17.

    An outcome-side assessment by nature, and it is kept apart from strategy
    quality deliberately: §17 asks that an execution problem not be read as a
    strategy problem. A valid signal filled badly is an execution observation.
    """
    signals: list[bool] = []
    section = Section()

    if outcome.fills:
        section.evidence.append(
            observed(f"the entry filled in {outcome.fills} confirmed execution(s).", "executions")
        )

    if outcome.slippage_points:
        worst = max(abs(v) for v in outcome.slippage_points)
        section.evidence.append(
            observed(
                f"recorded slippage across {len(outcome.slippage_points)} fill(s), worst "
                f"{worst} points.",
                "executions.slippage_points",
            )
        )
        ok = worst <= NOTABLE_SLIPPAGE_POINTS
        signals.append(ok)
        if not ok:
            section.warnings.append(
                f"worst fill slippage was {worst} points, above the {NOTABLE_SLIPPAGE_POINTS} "
                "at which this platform treats a fill as unusual."
            )
            section.evidence.append(
                hypothesis(
                    "slippage of this size may have moved the realised result "
                    "materially. CLAUDE.md records a live fill 279 points from its "
                    "quote that inverted the whole bracket."
                )
            )

    if outcome.fill_latency_seconds is not None:
        section.evidence.append(
            observed(
                f"the venue confirmed the fill {outcome.fill_latency_seconds:.2f}s after "
                "submission.",
                "orders.submitted_at vs orders.filled_at",
            )
        )
        ok = outcome.fill_latency_seconds <= NOTABLE_FILL_LATENCY_SECONDS
        signals.append(ok)
        if not ok:
            section.warnings.append(
                f"fill latency was {outcome.fill_latency_seconds:.2f}s, above the "
                f"{NOTABLE_FILL_LATENCY_SECONDS}s a routed market order should need."
            )
    if outcome.submit_latency_seconds is not None:
        section.evidence.append(
            observed(
                f"the platform submitted {outcome.submit_latency_seconds:.2f}s after "
                "creating the order.",
                "orders.created_at vs orders.submitted_at",
            )
        )

    if outcome.partial_fill:
        section.evidence.append(
            observed("the order filled partially against its requested quantity.", "orders")
        )
        signals.append(False)

    if not signals and not section.evidence:
        return unrated(
            "no order, fill or latency record is linked to this trade, so execution "
            "quality cannot be assessed. Imported rows have none."
        )

    section.rating = _rate(signals)
    if section.rating is Rating.unknown:
        section.unavailable_reason = (
            "execution rows exist but none carries slippage or latency, so there is "
            "nothing to rate. Latency is never inferred from a missing timestamp."
        )
    return section


# ======================================================= market context, §18


def market_context(decision: DecisionContext, outcome: OutcomeContext) -> dict[str, Any]:
    """Recorded market information only. Section 18.

    This platform records a REGIME on the AI decision and nothing else about the
    market at entry: no spread series, no volatility figure, no session label.
    Saying so is the whole content -- §5 forbids filling it in, and §18 forbids
    introducing information that was not available.
    """
    return {
        "regime": decision.ai_regime,
        "regime_source": (
            "ai_decisions.regime, classified at inference time" if decision.ai_regime else None
        ),
        "available": decision.ai_regime is not None,
        "observations": (
            [
                {
                    "kind": "OBSERVED",
                    "statement": f"the regime classifier recorded {decision.ai_regime} "
                    "at the time of the decision.",
                    "source": "ai_decisions.regime",
                }
            ]
            if decision.ai_regime
            else []
        ),
        "not_recorded": [
            "spread at entry",
            "volatility at entry",
            "trading session",
            "higher-timeframe trend",
            "volume",
        ],
        "note": (
            "this deployment records a regime label on the AI decision and no other "
            "market context. The fields above are listed as NOT RECORDED rather than "
            "estimated -- an inferred volatility reading would be indistinguishable "
            "from a measured one, and market_bars covers one instrument here."
        ),
        "outcome_side": {
            "mae": None if outcome.mae is None else str(outcome.mae),
            "mfe": None if outcome.mfe is None else str(outcome.mfe),
            "note": (
                "MAE and MFE need an intratrade price series, which this deployment "
                "does not store. Null rather than estimated."
            ),
        },
    }


def ai_context(decision: DecisionContext, outcome: OutcomeContext) -> dict[str, Any]:
    """The prediction beside the result. Section 20.

    **A disagreement is reported, never scored.** §20 is explicit: a 0.72
    probability on a losing trade does not mean the model was wrong. Whether the
    model is calibrated is a question about many trades, and L29 monitoring owns
    it.
    """
    if decision.ai_decision is None:
        return {
            "available": False,
            "why": (
                "no AI decision is linked. For a strategy configured AI_DISABLED that "
                "is the correct and expected shape, and it is NOT evidence the AI was "
                "bypassed."
            ),
        }
    disagreed = None
    if decision.ai_probability is not None and outcome.net_profit is not None:
        leaned_up = float(decision.ai_probability) > 0.5
        won = float(outcome.net_profit) > 0
        disagreed = leaned_up is not won
    return {
        "available": True,
        "mode": decision.ai_mode,
        "decision": decision.ai_decision,
        "probability": None if decision.ai_probability is None else str(decision.ai_probability),
        "confidence": None if decision.ai_confidence is None else str(decision.ai_confidence),
        "regime": decision.ai_regime,
        "anomaly_score": (
            None if decision.ai_anomaly_score is None else str(decision.ai_anomaly_score)
        ),
        "model": decision.prediction_model,
        "model_version": decision.prediction_model_version,
        "outcome_agreed": None if disagreed is None else not disagreed,
        "note": (
            "a probability that leaned the other way from this result says nothing "
            "about the model on its own -- a 0.72 that loses is what 0.72 means 28% of "
            "the time. Whether the model is calibrated is a question about MANY trades, "
            "and L29 model monitoring owns it."
        ),
    }


# ================================================================== helpers


def _rate(signals: list[bool]) -> Rating:
    """GOOD if everything checked held, POOR if none did, FAIR in between.

    UNKNOWN when nothing was checkable -- which is a different answer from
    "nothing went wrong", and the two must not share a value.
    """
    if not signals:
        return Rating.unknown
    passed = sum(1 for s in signals if s)
    if passed == len(signals):
        return Rating.good
    if passed == 0:
        return Rating.poor
    return Rating.fair


def _close(left: Decimal, right: Decimal) -> bool:
    """Equal to within a thousandth, so a rounding difference is not a finding."""
    if right == ZERO:
        return left == ZERO
    return abs((left - right) / right) <= Decimal("0.001")


def _summarise(sizing: dict[str, Any]) -> str:
    wanted = ("mode", "risk_amount", "risk_pct", "stop_distance", "final_quantity")
    parts = [f"{k}={sizing[k]}" for k in wanted if k in sizing]
    return ", ".join(parts) if parts else "a record with none of the expected fields"


__all__ = [
    "NOTABLE_FILL_LATENCY_SECONDS",
    "NOTABLE_SLIPPAGE_POINTS",
    "ai_context",
    "entry_quality",
    "execution_quality",
    "exit_quality",
    "market_context",
    "risk_quality",
    "strategy_alignment",
]
