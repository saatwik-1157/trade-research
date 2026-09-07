"""Recovery, resilience and reconciliation (L38).

    FAILURE -> SAFE STATE -> RECOVERY -> RECONCILIATION -> VALIDATION
            -> SAFE RESUME

and never `DISCONNECTED -> EXECUTING`.

**Almost none of the checking is new.** Four reconcilers already existed, each
written by the level that owns the state it compares -- `app/brokers/reconcile.py`
(L10), `OrderManager.reconcile` (L19), `app/positions/reconciler.py` (L21) and
`app/bots/supervisor.py` (L22). Every one of them reports rather than repairs,
and **not one of them ran at boot**. That was the gap: the pieces existed and
nothing called them first.

So this package orchestrates. It contains no comparison logic of its own,
because a second implementation of "do these positions match" is a second
answer.

**What is genuinely new is the latch.** Safe mode is a refusal that closes
automatically when the startup sequence finds a condition, names that condition
as its reason, and opens only for an authorized person after the sequence has
been re-run and the condition has actually cleared.

**What this package may not do**, and a test parses every module to prove it:
import an order manager class, a broker adapter class, a risk engine or a
sizing service; submit, cancel or modify an order; open, close or modify a
position; release a kill switch; enable live trading; or delete a historical
record. It reads, it reports, and it refuses.

Submodules:

    contract        the recovery ladder, safe-mode reasons, step results
    safe_mode       the latch, its reasons, and the guard callers use
    reconciliation  the existing reconcilers, called in a defined order
    manager         the startup sequence, the latch decision, the audit trail
"""

from __future__ import annotations
