"""The chronological event timeline. Section 26.

**Derived, never stored.** This is the decision that shapes the module, and it
is §3's rule applied literally: *do not create duplicate trade-history systems.*

Every event a trade's timeline needs is already recorded, with a timestamp, by
the system that produced it:

    webhook_events    the TradingView payload arriving
    signals           the normalised signal
    risk_events       approved or vetoed, with the snapshot that decided it
    ai_decisions      the model's verdict, at an exact version
    orders            the intent
    order_events      submitted, acknowledged, filled, rejected
    executions        each fill, with its deal id and slippage
    position_events   opened, stop modified, exit decided, closed
    trades            the journal row itself

A `trade_events` table would be a second copy of all of that. It could be
written wrongly, it could fall behind, and when it disagreed with the rows it
was copied from there would be no way to tell which was right — and the one
nobody looked at would be the one somebody quoted. So the timeline is assembled
at read time, and it cannot disagree with its sources because it has none of its
own.

**Idempotency comes free.** §27 asks that a duplicate fill or broker event must
not create duplicate records. A derived timeline has nothing to duplicate: the
same rows produce the same timeline however many times it is read, and a
duplicate event upstream is visible AS a duplicate rather than being silently
merged into a second history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: How the timeline labels each source. Named rather than inferred from the
#: table, so a reader can see which system to go and ask -- the same rule the
#: portfolio engine applies to its figures.
SOURCES = {
    "webhook": "the raw provider payload (L09)",
    "signal": "the normalised signal (L09, L12)",
    "risk": "the risk engine's decision (L17)",
    "ai": "the AI decision (L27)",
    "order": "the OMS order record (L19)",
    "order_event": "the OMS order lifecycle (L19)",
    "execution": "a confirmed fill (L19)",
    "position": "the position lifecycle (L21)",
    "journal": "the trade journal (L31)",
}


@dataclass(frozen=True)
class Event:
    """One thing that happened, and which system says so."""

    at: datetime
    source: str
    kind: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "source": self.source,
            "source_note": SOURCES.get(self.source, ""),
            "kind": self.kind,
            "detail": self.detail,
        }


def _at(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def derive(
    *,
    trade: Any,
    signal: Any = None,
    webhook: Any = None,
    risk: Any = None,
    ai: Any = None,
    order: Any = None,
    order_events: list[Any] | None = None,
    executions: list[Any] | None = None,
    position_events: list[Any] | None = None,
) -> list[Event]:
    """Every recorded moment of one trade, oldest first.

    Every argument is optional and a missing one contributes nothing. A trade
    imported from a JSONL ledger has no signal, no risk decision and no order —
    its timeline is two events, and that is the honest length rather than a
    reconstruction of what a pipeline trade would have looked like.

    **Ties are broken by the order the arguments appear**, which is the causal
    order of the pipeline. Two events stamped in the same second are common —
    L19 acknowledges and fills within one — and sorting them arbitrarily would
    render a fill before the submission that caused it.
    """
    found: list[tuple[int, Event]] = []
    rank = 0

    def add(at: datetime | None, source: str, kind: str, detail: dict[str, Any]) -> None:
        nonlocal rank
        if at is None:
            return
        rank += 1
        found.append((rank, Event(at=at, source=source, kind=kind, detail=detail)))

    if webhook is not None:
        add(
            _at(getattr(webhook, "received_at", None)),
            "webhook",
            "webhook_received",
            {
                "provider": getattr(webhook, "provider", None),
                "auth_strength": getattr(webhook, "auth_strength", None),
                "status": getattr(webhook, "status", None),
                # The payload is NOT included. It can carry a shared secret, and
                # §45 forbids exposing one; the provider and the auth strength
                # are what a reader needs to know it arrived and was trusted.
                "payload": "not included: a provider payload can carry a secret",
            },
        )
    if signal is not None:
        add(
            _at(getattr(signal, "signal_time", None)) or _at(getattr(signal, "received_at", None)),
            "signal",
            "signal_created",
            {
                "signal_key": getattr(signal, "signal_key", None),
                "source": getattr(signal, "source", None),
                "direction": getattr(signal, "direction", None),
                "confidence": _s(getattr(signal, "confidence", None)),
                "status": getattr(signal, "status", None),
            },
        )
    if ai is not None:
        add(
            _at(getattr(ai, "created_at", None)),
            "ai",
            f"ai_{str(getattr(ai, 'decision', 'decided')).lower()}",
            {
                "model": getattr(ai, "model_key", None),
                "version": getattr(ai, "model_version", None),
                "policy": getattr(ai, "policy", None),
                "decision": getattr(ai, "decision", None),
                "probability": _s(getattr(ai, "probability", None)),
                "regime": getattr(ai, "regime", None),
                "reason": getattr(ai, "reason", None),
            },
        )
    if risk is not None:
        add(
            _at(getattr(risk, "occurred_at", None)),
            "risk",
            f"risk_{str(getattr(risk, 'decision', 'decided')).lower()}",
            {
                "decision": getattr(risk, "decision", None),
                "reason": getattr(risk, "reason", None),
                "configuration_version": getattr(risk, "configuration_version", None),
            },
        )
    if order is not None:
        add(
            _at(getattr(order, "created_at", None)),
            "order",
            "order_created",
            {
                "intent_id": getattr(order, "intent_id", None),
                "order_type": getattr(order, "order_type", None),
                "quantity": _s(getattr(order, "quantity", None)),
                "requested_price": _s(getattr(order, "requested_price", None)),
                "stop_loss": _s(getattr(order, "stop_loss", None)),
                "take_profit": _s(getattr(order, "take_profit", None)),
            },
        )
    for event in order_events or []:
        add(
            _at(getattr(event, "occurred_at", None)),
            "order_event",
            str(getattr(event, "event_type", "order_event")),
            {
                "retcode": getattr(event, "retcode", None),
                "comment": getattr(event, "comment", None),
                "previous_status": getattr(event, "previous_status", None),
                "new_status": getattr(event, "new_status", None),
                "broker_order_id": getattr(event, "broker_order_id", None),
            },
        )
    for fill in executions or []:
        add(
            _at(getattr(fill, "executed_at", None)),
            "execution",
            "fill",
            {
                "price": _s(getattr(fill, "price", None)),
                "quantity": _s(getattr(fill, "quantity", None)),
                "commission": _s(getattr(fill, "commission", None)),
                "swap": _s(getattr(fill, "swap", None)),
                "slippage_points": _s(getattr(fill, "slippage_points", None)),
                "broker_deal_id": getattr(fill, "broker_deal_id", None),
                "fill_source": getattr(fill, "fill_source", None),
            },
        )
    for event in position_events or []:
        add(
            _at(getattr(event, "occurred_at", None)),
            "position",
            str(getattr(event, "event_type", "position_event")),
            dict(getattr(event, "payload", None) or {}),
        )

    add(
        _at(getattr(trade, "closed_at", None)),
        "journal",
        "trade_recorded",
        {
            "trade_id": getattr(trade, "id", None),
            "status": getattr(trade, "status", None),
            "exit_reason": getattr(trade, "exit_reason", None),
            "net_profit": _s(getattr(trade, "net_profit", None)),
        },
    )

    found.sort(key=lambda pair: (pair[1].at, pair[0]))
    return [event for _rank, event in found]


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def gaps_in(events: list[Event]) -> list[str]:
    """What the timeline cannot show, said out loud rather than left blank.

    A timeline with no risk decision could mean the trade bypassed the risk
    engine or that the link was never written, and those are very different
    facts. Naming the gap is not the same as resolving it, and this deliberately
    does not try — it says which is missing, so a reader knows to go and look.
    """
    seen = {event.source for event in events}
    gaps: list[str] = []
    for source, note in (
        ("signal", "no signal is linked, so what triggered this trade is not recorded here"),
        (
            "risk",
            "no risk decision is linked; the trade may predate the link or have been "
            "opened outside the pipeline",
        ),
        (
            "ai",
            "no AI decision is linked. That is the expected shape for an AI_DISABLED "
            "strategy and is NOT evidence the AI was bypassed",
        ),
        ("order", "no order is linked, so requested-versus-actual cannot be shown"),
        ("execution", "no fill rows are linked, so per-fill slippage is not available"),
        ("position", "no position events are linked, so partial closes cannot be itemised"),
    ):
        if source not in seen:
            gaps.append(note)
    return gaps


__all__ = ["SOURCES", "Event", "derive", "gaps_in"]
