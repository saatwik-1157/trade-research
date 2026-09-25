# LEVEL 19 — ORDER MANAGEMENT SYSTEM

Completed 2026-09-03. Adaptive build mode: audit → baseline → plan → implement
→ integrate → test → verify → document.

The OMS answers *what happened to the order?* — and nothing else. It decides
no risk (that is `app/risk`), computes no quantity (that is `app/sizing`, and
this package does not import it), and holds no position state (that is
`app/positions`).

---

## 1. What already existed

More than the level brief assumes, and the audit's most useful finding is how
much was already right.

| Component | State before L19 | Verdict |
|---|---|---|
| `orders` / `order_events` / `executions` tables | L05, with `intent_id UNIQUE` and 8 statuses including `unknown` | **KEEP**, extended |
| `app/paper/oms.py` | Full lifecycle: `Approval`-gated submit, intent idempotency, transitions, `unknown` non-retryable | **KEEP + REFACTOR** |
| `app/brokers/` | `BrokerAdapter` (12 methods), MT5 adapter, `FakeBroker` + fault injector, three-state `OrderResult` | **KEEP**, untouched |
| `app/brokers/reconcile.py` | Reports mismatches, never repairs | **KEEP**, untouched |
| `app/risk/` | The only producer of an `Approval` | **KEEP**, untouched |
| `app/positions/` | Position manager, separate by design | **KEEP**, untouched |
| `app/realtime/catalogue.py` | Eleven `ORDER_*` types declared, scoped, and naming L19 | **KEEP**, now produced |
| `POST /v1/orders` | 501 naming L19, with its gate and idempotency contract already fixed | **REPLACE** the handler |

**The guarantee that already existed and was preserved verbatim:**
`submit` takes an `Approval` as its first positional argument, and `Approval`
is constructible only by `RiskEngine.approve`. "Nothing reaches a broker
without passing Risk" is therefore a type, not a check somebody must remember.

**Nothing was duplicated.** No second OMS, order model, broker adapter,
execution service, position manager, reconciliation system or event bus was
created, and none existed to merge.

---

## 2. The gaps this level filled

### 2.1 Four states that real outcomes had nowhere to go into

The eight states could not express four things that actually happen:

| State | Why it had to exist |
|---|---|
| `submitting` | Persisted **before** the venue call. Without it a crashed submit is indistinguishable from a submit that never happened — which is the whole difference between "reconcile" and "safe to send". |
| `cancel_requested` | "We asked" and "the venue agreed" were the same state. Assuming a cancellation succeeded is how a position nobody is watching stays open. |
| `expired` | Reached only when the venue confirms it, never inferred from a local clock. |
| `failed` | The request never reached the venue. Deliberately **not** a synonym for `rejected`: a `failed` order is safe to re-send, a `rejected` one is not, and an `unknown` one must be reconciled first. Collapsing them loses exactly the distinction that decides whether a retry is safe. |

Twelve states, migration `0010_oms_lifecycle`. `SAFE_TO_RESEND` is the
one-element set `{failed}`, and `can_resend` is asserted against every state.

### 2.2 The state machine lived inside the paper package

It was correct, and it was reachable only by the paper venue. Extracted to
`app/oms/state.py` and imported back by `app/paper/oms.py`, which re-exports
every name it previously defined — so no existing caller or test changed.
`test_the_paper_oms_and_the_broker_oms_share_one_state_machine` asserts the
objects are literally identical, because two machines that disagree about
whether `accepted → cancelled` is legal is how a cancel succeeds in paper and
corrupts an order in demo.

### 2.3 No partial-fill accounting

`PaperOrder.fill` was a single fill. `app/oms/fills.py` adds `FillBook`:
`requested`, `filled_quantity`, `remaining_quantity` (derived, never stored)
and a quantity-weighted `average_price` recomputed from the whole list, so a
restart that reloads fills from the database gets the same number. The brief's
worked example — 40 @ 100.20 then 60 @ 100.25 → **100.23** — is asserted
literally.

An overfill is **refused, not truncated**: truncating would record a smaller
fill than the venue reported and leave the difference on the book with nothing
pointing at it. A repeated `broker_deal_id` is ignored, because execution
reports arrive more than once and a deal applied twice is a position twice the
size it should be.

### 2.4 An audit trail that could not be ordered

`order_events` had `event_type`, `occurred_at`, `retcode`, `comment`. L19 adds
`previous_status`, `new_status`, `source` and `broker_order_id` — all four
were reconstructible from the sequence, which is not the same as recorded.

`sequence` turned out to be load-bearing and was found by a test. A submit
that completes inside one clock reading produces four transitions with
identical `occurred_at`, and the primary key is a uuid, so without a per-order
sequence the four are **unorderable** — an audit trail that cannot be read
back in order is not an audit trail.

### 2.5 Ten declared events with no producer

The L07 catalogue named L19 as the producer of eleven `ORDER_*` types; one had
a producer. `app/oms/events.py` maps every state to a type, and the map is
total by construction — a state added later without an event fails the build.

Events are **queued and drained**, not published inline, so a slow or broken
bus cannot sit in the middle of a broker interaction; the caller publishes
after the state is durable, which is the only order in which an event can
never describe something that was not saved. A publish failure is logged and
swallowed: the record is already written, and a delivery problem must not
become a lifecycle problem.

### 2.6 No broker-bound lifecycle at all

`app/oms/service.OrderManager` drives a `BrokerAdapter`. **There is no
`DemoOMS` and no `LiveOMS`** — demo and live differ only in which adapter an
operator registered, exactly as §16 asks.

---

## 3. Decisions

### 3.1 One lifecycle core, two venue bindings

`state.py` and `fills.py` are shared. `OrderManager` binds them to a broker
adapter; `app/paper/oms.py` binds the same two to the paper venue's in-process
execution provider.

Merging the two bindings into one class would mean making the paper path
async, which ripples through `PaperEngine` and its 93 tests — a rebuild of
working code, which the brief forbids. What §16 actually forbids is a *second
set of rules*, and there is one set: one state machine, one fill accounting,
one idempotency key, one reconciliation rule.

### 3.2 `POST /v1/orders` is built, and is not a shortcut

A manual order runs the **same gates a bot signal runs, in the same order,
through the same objects** — including reaching risk the way the paper service
reaches it (effective limits from `RiskService`, `Approval` minted by the pure
engine, `record_verdict` for the durable record). This route cannot hold a
looser copy of the limits.

Three properties make a client-facing submit route safe:

- **A client cannot bypass a gate by asking.** Its quantity is an *input* to
  position sizing, validated against the venue's step, minimum and maximum
  like any other. A size below the venue minimum is refused, never raised.
- **A client cannot reach a venue nobody registered.** `OrderManagerRegistry`
  is empty until an operator registers an adapter, so the default deployment
  refuses with that reason.
- **Idempotency is the platform's.** `Idempotency-Key` becomes
  `orders.intent_id`, which is `UNIQUE`. A double-clicked button, a retried
  fetch and a replayed TradingView alert are one mechanism.

### 3.3 Modification requires a *fresh* approval

The approval that authorised the original order was bound by `request_hash` to
*those* levels — the entire point of that binding is that changed levels need
a fresh evaluation. Quantity is not modifiable: on this venue a size change is
a different order, and pretending otherwise would let the modify path become
an un-sized, un-reconciled second submission.

The local record is written **only after the venue confirms**. Writing first
would make the platform believe a stop is in force that the venue never took.

### 3.4 Reconciliation concludes nothing when it cannot read the venue

`reconcile` raises and leaves the order `unknown` if the adapter cannot be
queried. A reconciler that decides an order is lost because the network was
down is worse than one that refuses to decide.

When the venue *can* be read and holds neither an order nor a position for the
intent, the order becomes `failed` — the one state a fresh order may follow,
reached by asking rather than by assuming.

### 3.5 The live gates stay false

`position_sizing_refusal` was built at L18 and held false; `order_idempotency`
and `unknown_status_reconciliation` are built here and held false the same
way. A gate is a claim that live execution may rely on the mechanism, and
flipping them is an operator decision taken in one reviewed pass — not a side
effect of the level that wrote the code. Three tests assert every gate is
false, so a flip must be deliberate and visible.

---

## 4. Changes, classified

**ADD** — `app/oms/` (`state.py`, `fills.py`, `order.py`, `service.py`,
`events.py`, `repository.py`, `registry.py`), `alembic/versions/0010_oms_lifecycle.py`,
`tests/test_oms.py` (63 tests), `POST /v1/orders`, `POST /v1/orders/{id}/cancel`,
`POST /v1/orders/{id}/reconcile`, `GET /v1/orders/oms/status`.

**REFACTOR** — the state machine out of `app/paper/oms.py` into
`app/oms/state.py`, re-exported so nothing downstream changed.

**KEEP + MODIFY** — `app/models/execution.py` (four states, fill accounting,
provenance, lifecycle stamps, the audit-trail columns); `app/api/v1/orders.py`
(the 501 becomes the real path); `frontend` orders table and order ticket.

**REPLACE** — only the `POST /v1/orders` 501 handler, which said the OMS did
not exist. It does.

**REMOVE** — nothing. No file, table, column, route or test was deleted; no
historical order was touched, and the down-migration moves the four new states
to `unknown` rather than guessing at them.

---

## 5. Safety invariants (§42)

| # | Invariant | How it holds |
|---|---|---|
| 1 | No order reaches a broker without risk approval | `create` takes an `Approval`; only `RiskEngine.approve` makes one. Expiry and `request_hash` re-checked. |
| 2 | No order without valid sizing output | The volume arrives on the approval; `app/oms` does not import `app.sizing`, asserted by a test. |
| 3 | AI cannot execute | The AI seat runs before risk and can only decline. |
| 4 | TradingView cannot execute | The gateway publishes `SIGNAL_CREATED`; nothing consumes it. |
| 5 | Duplicate signals → one order | `by_intent` in memory, `intent_id UNIQUE` in the database, `guard_resend` at the entrance. |
| 6 | Unknown state reconciled before retry | `SAFE_TO_RESEND == {failed}`; `unknown`'s exits contain no sendable state. |
| 7 | Filled orders are not pending | `filled` is terminal — no outgoing edges at all. |
| 8 | Cancelled cannot be re-submitted | Same: terminal states have empty transition sets. |
| 9 | Requested ≠ filled | `filled_quantity` moves only when an execution is recorded; an acknowledged order has filled nothing, and `average_fill_price` is `None` rather than 0. |
| 10 | Broker execution is the truth | Fills come from execution reports; recovery rebuilds them from `executions` and reports any disagreement with the stored figure. |
| 11 | Live disabled by default | `TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten gates false. |
| 12 | No credentials in logs | The OMS holds an adapter, never a secret; asserted on the parsed AST. |
| 13 | History preserved | Additive migration; existing rows keep `filled_quantity = 0` and NULL provenance, which says "not recorded". |
| 14 | Working functionality intact | The paper OMS re-exports every name it defined; its 93 tests pass unchanged. |

---

## 6. Tests

Baseline before L19 (after L18): backend **962 passed, 15 failed, 3 skipped** —
the 15 are `redis.exceptions` because Docker was not running on this machine,
and they failed identically on the pre-L18 baseline.

`tests/test_oms.py` is **63 tests**, covering the brief's thirty cases plus
the state machine's own properties. Ten API tests cover the submission path
end to end against a simulated venue; the frontend suite gained one.

The three that matter most:

- `test_an_unknown_submission_is_never_retried`
- `test_a_second_order_for_an_unresolved_intent_is_refused` (the guard at the
  entrance, not only at the exit)
- `test_reconciliation_that_cannot_read_the_venue_concludes_nothing`

---

## 7. Remaining, and honest about it

- **Limit and stop orders are accepted and validated but reach the venue as
  the adapter's single `place_order`.** `OrderRequest` has no price field —
  MT5 pending orders need one — so a limit order currently behaves as a market
  order at the venue. The type is validated and stored; the adapter extension
  is broker work, and pretending otherwise would be the "fake execution
  result" the brief forbids.
- **Restart recovery loads unresolved orders and reports them; nothing calls
  it at startup yet.** The wiring belongs with the supervised runner (L22) and
  startup reconciliation (L38). `load_unresolved` and `resume` are built and
  tested.
- **The per-account lock is not distributed.** `orders.intent_id UNIQUE` is
  the backstop for a second process; `describe()` says so.
- **`modify` covers stop and target only**, for the reason in §3.3.
- **Demo and live are unexercised against a real terminal** — MT5 was not
  running here. The path is identical to the tested one by construction: the
  mode is not a parameter of the lifecycle, only of which adapter is
  registered.
