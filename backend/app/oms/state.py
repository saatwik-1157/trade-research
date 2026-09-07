"""The order state machine. One machine, every execution mode.

This is the vocabulary and the legal moves, and nothing else -- no venue, no
broker, no database, no clock. It was extracted from `app/paper/oms.py` at
L19, where it worked correctly for the paper venue, so that the broker-facing
lifecycle could use *the same* machine rather than a parallel one. Two state
machines that disagree about whether `accepted -> cancelled` is legal is how a
cancel succeeds in paper and corrupts an order in demo.

`app/paper/oms.py` re-exports `OrderStatus` from here, so every existing
caller and test is unchanged.

---

**Twelve states.** Eight existed and are unchanged. Four were added at L19,
each because a real outcome had nowhere to go:

  * `submitting` -- persisted BEFORE the venue call. A crash between the send
    and the response leaves this row on disk, which is what makes the send
    recoverable at all: without it a crashed submit is indistinguishable from
    a submit that never happened. The paper venue skips it (it is in-process
    and cannot crash mid-call), and `intent -> submitted` stays legal.
  * `cancel_requested` -- a cancel asked for and not yet confirmed. Without
    it, "we asked" and "the venue agreed" are the same state, and assuming a
    cancellation succeeded is how a position nobody is watching stays open.
  * `expired` -- reached only when the venue confirms it. Never inferred from
    a local clock.
  * `failed` -- the request never reached the venue. Distinct from `rejected`,
    which is the venue refusing: a `failed` order is safe to re-send, a
    `rejected` one should not be, and an `unknown` one must be reconciled
    first. Collapsing them loses exactly the distinction that decides whether
    a retry is safe.

**`unknown` is not an error state and nothing retries out of it.** An IPC
timeout after `order_send` looks exactly like a rejection from the caller's
side. The only exits are the ones reconciliation can establish by looking at
the venue.

**Terminal means terminal.** `filled`, `rejected`, `cancelled` and `expired`
have no outgoing edges at all, so `FILLED -> CREATED`, `FILLED -> SUBMITTED`,
`CANCELLED -> SUBMITTED` and `REJECTED -> FILLED` are not reachable -- not
because a check forbids them, but because the edges do not exist.
"""

from __future__ import annotations

from enum import StrEnum

from app.models.execution import ORDER_STATUSES


class OrderStatus(StrEnum):
    """The `orders.status` vocabulary, shared by every execution mode."""

    # An order exists and has been approved. Nothing has been sent.
    intent = "intent"
    # Persisted before the venue call. Only the broker path uses it.
    submitting = "submitting"
    # Sent. The venue has not spoken yet.
    submitted = "submitted"
    # The venue acknowledged it.
    accepted = "accepted"
    partially_filled = "partially_filled"
    filled = "filled"
    # A cancel was requested and is not yet confirmed.
    cancel_requested = "cancel_requested"
    cancelled = "cancelled"
    # The venue refused. NOT the same as `failed`.
    rejected = "rejected"
    # The venue confirmed an expiry. Never inferred locally.
    expired = "expired"
    # The request never reached the venue.
    failed = "failed"
    # We do not know what the venue did. Reconciled, never retried.
    unknown = "unknown"


# Every state this module can reach must exist on the table. A test checks it,
# and this assert fails the import if a migration is missing.
assert {s.value for s in OrderStatus} <= set(ORDER_STATUSES), (
    "an order state exists in code that the orders table will refuse: "
    f"{sorted({s.value for s in OrderStatus} - set(ORDER_STATUSES))}"
)


TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.intent: frozenset(
        {
            # The broker path persists `submitting` first; the paper path,
            # being in-process, goes straight to `submitted`. Both are legal
            # because both are true of the venue they describe.
            OrderStatus.submitting,
            OrderStatus.submitted,
            OrderStatus.rejected,
            OrderStatus.cancelled,
            OrderStatus.failed,
        }
    ),
    OrderStatus.submitting: frozenset(
        {
            OrderStatus.submitted,
            OrderStatus.rejected,
            # The request did not reach the venue. Safe to re-send.
            OrderStatus.failed,
            # It may have. NOT safe to re-send.
            OrderStatus.unknown,
        }
    ),
    OrderStatus.submitted: frozenset(
        {
            OrderStatus.accepted,
            OrderStatus.partially_filled,
            OrderStatus.filled,
            OrderStatus.cancel_requested,
            OrderStatus.rejected,
            OrderStatus.expired,
            OrderStatus.unknown,
        }
    ),
    # `rejected` belongs here: a venue that has acknowledged an order can
    # still refuse to fill it, and without this an unfillable accepted order
    # could not be recorded at all -- it raised instead, losing the reason.
    OrderStatus.accepted: frozenset(
        {
            OrderStatus.partially_filled,
            OrderStatus.filled,
            OrderStatus.cancel_requested,
            OrderStatus.cancelled,
            OrderStatus.rejected,
            OrderStatus.expired,
            OrderStatus.unknown,
        }
    ),
    OrderStatus.partially_filled: frozenset(
        {
            # A second partial fill is a real event and must be recordable.
            OrderStatus.partially_filled,
            OrderStatus.filled,
            OrderStatus.cancel_requested,
            OrderStatus.cancelled,
            OrderStatus.expired,
            OrderStatus.unknown,
        }
    ),
    OrderStatus.cancel_requested: frozenset(
        {
            OrderStatus.cancelled,
            # The venue filled it before the cancel landed. This is a race
            # that happens, and refusing to record it would leave a filled
            # order showing as pending.
            OrderStatus.filled,
            OrderStatus.partially_filled,
            # The venue refused the cancel and the order stands.
            OrderStatus.accepted,
            OrderStatus.unknown,
        }
    ),
    # Terminal. No outgoing edges, so the forbidden moves are unreachable.
    OrderStatus.filled: frozenset(),
    OrderStatus.rejected: frozenset(),
    OrderStatus.cancelled: frozenset(),
    OrderStatus.expired: frozenset(),
    # Terminal for this order, but it never reached the venue, so a NEW order
    # for the same intent is the safe recovery -- not a transition from here.
    OrderStatus.failed: frozenset(),
    # NOT terminal, and NOT retryable. Reconciliation establishes which of
    # these the venue actually did; nothing else may move an order out.
    OrderStatus.unknown: frozenset(
        {
            OrderStatus.filled,
            OrderStatus.partially_filled,
            OrderStatus.cancelled,
            OrderStatus.rejected,
            OrderStatus.expired,
            # Reconciliation found no order at the venue at all.
            OrderStatus.failed,
        }
    ),
}

TERMINAL = frozenset(
    {
        OrderStatus.filled,
        OrderStatus.rejected,
        OrderStatus.cancelled,
        OrderStatus.expired,
        OrderStatus.failed,
    }
)

OPEN = frozenset(
    {
        OrderStatus.intent,
        OrderStatus.submitting,
        OrderStatus.submitted,
        OrderStatus.accepted,
        OrderStatus.partially_filled,
        OrderStatus.cancel_requested,
    }
)

#: States whose truth is not known locally and must be established against the
#: venue before anything is sent for the same intent.
NEEDS_RECONCILIATION = frozenset({OrderStatus.unknown, OrderStatus.submitting})

#: The only states from which a re-send for the same intent is safe. `failed`
#: means the request provably did not reach the venue. Everything else either
#: reached it or might have.
SAFE_TO_RESEND = frozenset({OrderStatus.failed})


class IllegalOrderTransition(Exception):
    """A move the order state machine does not allow."""


def check_transition(current: OrderStatus, wanted: OrderStatus) -> None:
    if wanted not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(str(s) for s in TRANSITIONS[current])) or "nothing"
        raise IllegalOrderTransition(
            f"a {current} order cannot become {wanted}; it may become {allowed}"
        )


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL


def can_resend(status: OrderStatus) -> bool:
    """Whether a fresh order for the same intent is safe.

    False for `unknown` and `submitting` by construction: those are the states
    where the venue may hold an order we cannot see, and re-sending is how one
    intent becomes two positions.
    """
    return status in SAFE_TO_RESEND
