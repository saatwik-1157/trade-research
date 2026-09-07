"""What the system knew when the trade was opened. Sections 8 to 12, and 23.

The question §23 poses is the one this module answers:

    "What did the system know when the trade was opened?"

**Read from the rows that were written then, never recomputed now.** §10 is
explicit — *do not recalculate historical risk context using today's values* —
and that rule generalises to every block here. The risk snapshot was written
into `risk_events.snapshot` at decision time; the AI verdict into `ai_decisions`
at inference time; the sizing into `orders.sizing` at submission. Recomputing
any of them today would produce a number that describes the platform now and
claims to describe the trade then.

**An absent block says it is absent, and why.** §51 forbids fabricated
decisions, and the honest shape for "no AI was consulted" is not an empty AI
block that renders as a model with no opinion — it is a statement that the
strategy was configured AI_DISABLED, or that the link was never written, which
are different facts a reader must be able to tell apart.

**No secret is ever copied.** §8 and §45. The TradingView block carries the
provider, the event id and the auth strength; the webhook payload stays where it
is, because a provider payload can contain a shared secret and this record is
served to a browser.
"""

from __future__ import annotations

from typing import Any


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def absent(what: str, why: str) -> dict[str, Any]:
    """A block that has nothing in it, and says which kind of nothing."""
    return {"available": False, "what": what, "why": why}


def strategy_block(trade: Any, signal: Any = None, webhook: Any = None) -> dict[str, Any]:
    """Section 8 and §22. Which strategy, which version, which signal."""
    version_id = getattr(trade, "strategy_version_id", None)
    if version_id is None and signal is None:
        return absent(
            "strategy attribution",
            "no strategy version and no signal are linked to this trade. Imported "
            "rows have neither: the ledger they came from recorded the fill and not "
            "the rule.",
        )
    block: dict[str, Any] = {
        "available": True,
        "strategy_version_id": version_id,
        "source": "strategy_versions (L12)",
    }
    if signal is not None:
        block["signal"] = {
            "id": getattr(signal, "id", None),
            "signal_key": getattr(signal, "signal_key", None),
            "source": getattr(signal, "source", None),
            "direction": getattr(signal, "direction", None),
            "confidence": _s(getattr(signal, "confidence", None)),
            "signal_time": (
                signal.signal_time.isoformat() if getattr(signal, "signal_time", None) else None
            ),
            "auth_strength": getattr(signal, "auth_strength", None),
            "context": getattr(signal, "metadata", None),
        }
    if webhook is not None:
        block["tradingview"] = {
            "provider": getattr(webhook, "provider", None),
            "event_id": getattr(webhook, "id", None),
            "received_at": (
                webhook.received_at.isoformat() if getattr(webhook, "received_at", None) else None
            ),
            "auth_strength": getattr(webhook, "auth_strength", None),
            "payload": None,
            "payload_note": (
                "deliberately not served. A provider payload can carry a shared "
                "secret, and section 45 forbids exposing one."
            ),
        }
    return block


def ai_block(trade: Any, decision: Any = None) -> dict[str, Any]:
    """Section 9 and §23. The model, at an EXACT version, and what it said."""
    if decision is None:
        return absent(
            "AI attribution",
            "no AI decision is linked. For a strategy configured AI_DISABLED that is "
            "the correct and expected shape -- the seat is empty rather than filled by "
            "a filter that accepts everything -- and it is NOT evidence the AI was "
            "bypassed.",
        )
    return {
        "available": True,
        "mode": getattr(decision, "policy", None),
        "model_key": getattr(decision, "model_key", None),
        "model_version": getattr(decision, "model_version", None),
        "model_version_id": getattr(decision, "model_version_id", None),
        "feature_version": getattr(decision, "feature_version", None),
        "decision": getattr(decision, "decision", None),
        "status": getattr(decision, "status", None),
        "probability": _s(getattr(decision, "probability", None)),
        "predicted_class": getattr(decision, "predicted_class", None),
        "regime": getattr(decision, "regime", None),
        "anomaly_score": _s(getattr(decision, "anomaly_score", None)),
        "confidence": _s(getattr(decision, "confidence", None)),
        "strategy_score": _s(getattr(decision, "strategy_score", None)),
        "combined_score": _s(getattr(decision, "combined_score", None)),
        "reason": getattr(decision, "reason", None),
        "latency_ms": {
            "feature": _s(getattr(decision, "feature_latency_ms", None)),
            "inference": _s(getattr(decision, "inference_latency_ms", None)),
            "total": _s(getattr(decision, "total_latency_ms", None)),
        },
        "note": (
            "an EXACT model version, never 'latest'. Section 9: a review that cannot "
            "say which weights produced a decision cannot review the decision."
        ),
    }


def risk_block(trade: Any, event: Any = None) -> dict[str, Any]:
    """Section 10. The context as it stood, from the row written then."""
    if event is None:
        return absent(
            "risk attribution",
            "no risk decision is linked. The trade may predate the link, or have been "
            "opened outside the pipeline. It is NOT a record of the risk engine being "
            "bypassed, and this does not claim otherwise.",
        )
    return {
        "available": True,
        "decision": getattr(event, "decision", None),
        "reason": getattr(event, "reason", None),
        "configuration_version": getattr(event, "configuration_version", None),
        "decision_id": getattr(event, "decision_id", None),
        "occurred_at": (
            event.occurred_at.isoformat() if getattr(event, "occurred_at", None) else None
        ),
        # The snapshot as written. Not re-derived, not re-evaluated against
        # today's limits -- section 10 forbids exactly that, and a snapshot
        # recomputed now would describe the platform now.
        "snapshot": getattr(event, "snapshot", None),
        "note": (
            "the state the risk engine saw, as it was written at decision time. "
            "Never recalculated with today's values."
        ),
    }


def sizing_block(trade: Any, order: Any = None) -> dict[str, Any]:
    """Section 11. The sizing calculation, from `orders.sizing`.

    Read, never re-run. `app.sizing` is the one calculator and §11 says not to
    duplicate its formulas; re-running it here would additionally use today's
    equity, tick value and volume step, which is §10's error wearing a different
    hat.
    """
    recorded = getattr(order, "sizing", None) if order is not None else None
    if not recorded:
        return absent(
            "position sizing",
            "no sizing record is linked. Orders written before the sizing block "
            "existed, and imported trades, both have none.",
        )
    return {
        "available": True,
        "sizing": recorded,
        "requested_quantity": _s(getattr(order, "quantity", None)),
        "filled_quantity": _s(getattr(order, "filled_quantity", None)),
        "source": "orders.sizing (L18), recorded at submission",
        "note": (
            "read, never re-run. Recomputing it would use today's equity, tick value "
            "and volume step and claim to describe the trade as it was sized."
        ),
    }


def execution_block(
    trade: Any, order: Any = None, executions: list[Any] | None = None
) -> dict[str, Any]:
    """Section 12. What was ASKED for against what the venue DID.

    `CLAUDE.md` records why this block exists at all: a live NZDUSD sell was
    quoted at 0.59752 and filled at 0.59473, 279 points away, and the order log
    hid it for three days by recording the requested price as the entry. A
    figure the tool chose is not a figure the server confirmed.
    """
    fills = executions or []
    block: dict[str, Any] = {
        "available": bool(order is not None or fills),
        "requested_entry_price": _s(getattr(trade, "requested_entry_price", None)),
        "actual_entry_price": _s(getattr(trade, "entry_price", None)),
        "entry_slippage_points": _s(getattr(trade, "entry_slippage_points", None)),
        "actual_exit_price": _s(getattr(trade, "exit_price", None)),
        "fills": [
            {
                "at": (
                    fill.executed_at.isoformat() if getattr(fill, "executed_at", None) else None
                ),
                "price": _s(getattr(fill, "price", None)),
                "quantity": _s(getattr(fill, "quantity", None)),
                "commission": _s(getattr(fill, "commission", None)),
                "swap": _s(getattr(fill, "swap", None)),
                "slippage_points": _s(getattr(fill, "slippage_points", None)),
                "broker_deal_id": getattr(fill, "broker_deal_id", None),
                "fill_source": getattr(fill, "fill_source", None),
            }
            for fill in fills
        ],
        "broker_order_id": getattr(order, "broker_order_id", None) if order else None,
        "broker_position_id": getattr(trade, "broker_position_id", None),
        "order_type": getattr(order, "order_type", None) if order else None,
        "note": (
            "the venue's own figures take precedence over what was requested. "
            "CLAUDE.md records the live trade whose 279-point gap stayed invisible "
            "for three days because the log kept the requested price as the entry."
        ),
    }
    if not block["available"]:
        block["why"] = (
            "no order and no fill rows are linked, so requested-versus-actual cannot "
            "be shown for this trade."
        )
    return block


def costs_block(trade: Any) -> dict[str, Any]:
    """Section 18. Each cost separately, and never folded together."""
    return {
        "gross_profit": _s(getattr(trade, "gross_profit", None)),
        "commission": _s(getattr(trade, "commission", None)),
        "swap": _s(getattr(trade, "swap", None)),
        "fees": _s(getattr(trade, "fees", None)),
        "net_profit": _s(getattr(trade, "net_profit", None)),
        "currency": getattr(trade, "currency", None),
        "r_multiple": _s(getattr(trade, "r_multiple", None)),
        "note": (
            "each cost is recorded separately and none is folded into another: MT5 "
            "reports commission and fees apart, and adding one into the other makes "
            "the total right and each part wrong. Read R rather than net currency "
            "when pooling across trades -- net cannot be pooled across trades sized "
            "by different stop distances."
        ),
    }


__all__ = [
    "absent",
    "ai_block",
    "costs_block",
    "execution_block",
    "risk_block",
    "sizing_block",
    "strategy_block",
]
