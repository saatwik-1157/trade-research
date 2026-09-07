# PRODUCTION_PILOT_READINESS.md

Level 45 eligibility gate. 2026-09-06.

---

## Determination

```
PILOT ELIGIBILITY:   NOT ELIGIBLE
LIVE TRADING:        NOT AUTHORIZED

CRITICAL DEFECTS:    4 found  ->  4 FIXED, each with a regression test
BLOCKING THE GATE:   no venue has ever been connected; no backup has ever
                     been restored. Neither is a code defect and neither is
                     closed by the fixes below.
```

*(Updated 2026-09-06, after C-1 to C-4 were fixed. The audit narrative is kept
verbatim below, because the reasoning that found these is worth more than the
verdict.)*

**Four CRITICAL defects were found. Three of them (C-1, C-2, C-4) are on L45
Phase 2's list of conditions that automatically mean NOT ELIGIBLE**; C-3 is the
test that should have caught C-2. A fifth finding (F-1) is HIGH rather than
critical because it fails closed.

They were found by an adversarial audit — agents instructed to *break* the
platform's safety claims rather than confirm them — and each was reproduced
independently before being recorded here.

**All four are now fixed.** Each fix carries a regression test that fails on
the old code, and in two cases the old behaviour is pinned as an explicit
assertion so the test cannot quietly stop testing anything. What that does
*not* do is make the platform pilot-eligible: the two blockers in "Why a pilot
could not proceed even without C-1 to C-3" are unchanged, and they were never
about these defects.

**The fixes are verified against tests and a simulator. No part of this has
been verified against a real venue, because there still isn't one.**

---

## C-1 · An unknown order IS re-sent across a process restart — CRITICAL · **FIXED**

**L45 category: "unknown-order retry vulnerability" and "duplicate-order
vulnerability".**

The platform's most-documented safety rule is *never retry an order whose venue
state was never established*. That rule holds **only within one process
lifetime.**

Reproduced directly, same code, same venue, one variable changed:

```
A. SAME process (what every existing test covers)
   pass1 = execution_unknown   pass2 = duplicate_signal   -> REFUSED

B. ACROSS a restart (new pipeline + new OMS registry)
   pass1 = execution_unknown   pass2 = filled             -> ALLOWED
```

**Why every layer of protection misses it:**

| Guard | Why it does not fire |
|---|---|
| `ExecutionPipeline.seen` | process-local set, empty after restart |
| `OrderManager.by_intent` | process-local dict, empty after restart |
| `guard_resend()` | consults `by_intent`; empty → returns silently |
| `orders.intent_id` UNIQUE | **the pipeline never writes an `orders` row** — verified: no `persist`, no `OrderRepository`, no `AsyncSession` anywhere in `pipeline.py` |
| `OrderManager.resume()` | **zero callers in `app/`** — built, tested, never wired |
| `load_unresolved()` | **zero callers in `app/`** — same |
| startup reconciliation | `recovery/reconciliation.py` queries the `orders` table; with no row it reports `unresolved=0` and **safe mode never latches** |

The signal row is deliberately left at `status='new'` for `execution_unknown`
(L38's documented "parked, reoffered later"), so the worker re-claims it on the
next tick. After a restart there is nothing left to refuse it.

**A restart is not exotic.** `DEPLOYMENT_GUIDE.md` documents the safe deploy as
*stop → migrate → start*. A crash, an OOM kill, a rolling replacement and a
routine deploy all produce this state.

**Whether the venue ends up holding two orders depends on whether the first one
actually landed — which is unknowable by definition. That is precisely why the
rule exists.**

### The fix

`app/execution/store.py` (new) gives the pipeline the durable half it never
had, and `app/main.py` passes it at the one place the deployed pipeline is
built. Three things follow, in the order they matter:

1. **The pipeline writes an `orders` row before it transmits.** This is §18's
   ordering, which `app/oms/repository.py`'s own docstring has always
   specified and which the API order route has always honoured. The automated
   path did not, and that asymmetry between the manual path and the automated
   one *was* C-1. With the row present, `orders.intent_id` UNIQUE guards the
   automated path, `unresolved_orders` finds the unresolved order, and safe
   mode latches at startup as designed.
2. **`guard_resend` is asked of the database as well as of memory.**
   `ExecutionPipeline._guard_resend_durably` raises the same two exceptions as
   the in-memory guard, so an unresolved intent is still `execution_unknown`
   and a settled one is still `duplicate_signal` — the counters and the
   operator instructions are unchanged.
3. **A durable write that fails refuses the send.** New outcome
   `not_recorded`: nothing is transmitted, the created order is discarded from
   the OMS (`OrderManager.discard`, which refuses outright once anything has
   been sent), and the signal is *not* consumed — so a database that blinks
   costs a delay, not a trading signal.

**Regression:** `tests/test_execution_durability.py`, 27 tests. A "restart"
there is a genuinely fresh `ExecutionPipeline`, `OrderManagerRegistry` and
`OrderManager`, so `seen` and `by_intent` are empty exactly as in a new
process; only the database and the venue carry over. The venue is deliberately
made healthy again before the second pass, so the refusal cannot pass for the
wrong reason.

`test_the_restart_regression_is_capable_of_failing` runs the identical
scenario with no store and asserts the *old* behaviour — pass two fills, the
venue is asked twice, no row is written. A regression test that cannot fail is
C-4 in test form, and this file exists because of C-4.

The fixture enforces foreign keys, which SQLite ignores by default and
PostgreSQL does not, so an integrity error that would only appear in
production appears here instead.

### Two further defects this uncovered

**`persist` never wrote an account.** Both account columns were always NULL,
which left a recovered order belonging to nobody (`_rehydrate` returned
`account_id=""`) and silently emptied two live queries that filter on exactly
those columns — per-account order history in `analytics/service.py`, and the
paper account's own order list. The account is now resolved and written; an
unrecognised id is reported and left NULL rather than written blindly, because
both columns carry a foreign key.

**A "safe resend" could not be recorded.** `SAFE_TO_RESEND` permits a fresh
order for an intent whose send provably failed, and `orders.intent_id` is
UNIQUE, so the second order had nowhere to be written. Left alone this would
have surfaced as an IntegrityError raised between `create` and `submit` — a
failure at the least recoverable moment, for a reason the schema knew all
along. **The schema is the stricter authority and it wins**: the guard refuses
first, with a message that says why, and the API order route was given the same
check for the same reason. The cost is bounded — `execution_rejected` already
retires the signal row, so the worker was never going to reoffer it.

## C-2 · Closing a position bypasses the RiskEngine and the OMS — CRITICAL · **FIXED**

**L45 category: "OMS bypass".**

`app/positions/broker_executor.py:110` calls the adapter directly:

```python
result = await manager.adapter.close_position(
    position.broker_position_id, volume=volume
)
```

It borrows the `OrderManager` purely as a container for `.adapter`. There is no
`Approval`, no RiskEngine call, no `OrderManager.submit()`, no `intent_id`, and
no order record. Reachable over HTTP at `POST /v1/positions/{id}/close`.

**The module's own docstring asserts the opposite:**

> "**A close is an order.** It takes the same path any other order takes — the
> Risk Engine approves it, the OMS submits it, the adapter reaches the venue —
> because a close that skipped those would be a second execution path, and the
> second path is always the one nobody is watching."

It describes the exact defect it contains. That matters beyond this one file:
**this platform's safety argument rests heavily on module docstrings**, and
this is proof that a docstring can be confidently wrong. Every other "cannot
reach a venue" claim in the repository is now worth re-deriving from code.

**Consequences:** no `intent_id` means **no idempotency** — two concurrent close
requests are two `close_position` calls at the venue. No order record means
reconciliation cannot see it.

**Mitigating, and stated so the severity is not overstated:** a close is
risk-*reducing*; the route is authenticated and permissioned; it checks the
environment mode and refuses rather than clamping an oversized volume. This is
"an operator-initiated close escapes the OMS lifecycle and its idempotency",
not "anyone can trade".

### The fix, and the design question it forced

The close now takes the path the docstring claimed: `RiskEngine.approve_close`
mints the `Approval`, `OrderManager.create` makes the order and its
`intent_id`, `OrderManager.close` runs the same lifecycle `submit` runs, and
the order is recorded before and after the venue is called. `.adapter` is no
longer read anywhere in the module, and a test asserts that.

**Routing a close through the RiskEngine naively would have been worse than
the bug.** Every limit this engine enforces bounds the risk of *taking* a
position — exposure, position count, daily loss, drawdown, the kill switches.
Applying them to a close refuses to reduce exposure at the moment exposure is
worst: a breached daily loss would make a position impossible to exit, and a
kill switch would trap every open position behind it. That is not a risk
control, and it is very likely why the original code went around the engine
instead of through it.

So `approve_close` is a **separate entry point** from `approve`. It evaluates
the close, records the verdict, and approves it regardless — re-stamping the
breached checks `enforced=False`, which is what `not_enforced` has always
meant, so they reach the order's `risk_snapshot` and the record of the close
names every limit it went over. One check still refuses: the mode fence, since
a close is still an instruction transmitted to a venue.

That decision is written down in three places that a reader will actually hit —
the method, the executor's module docstring, and
`RiskService.engine_for_close` — because "the risk engine approved it" now
means something different for a close than for an open, and a reader who
assumes otherwise is the next incident.

**`Approval` now has two constructors instead of one**, so the invariant worth
pinning is no longer "one method" but **one class**:
`test_only_the_risk_engine_can_mint_an_approval` walks every `.py` file in
`app/` and fails if an `Approval` is built outside `app/risk/engine.py`.

**Regression:** six tests in `tests/test_positions.py`, including two
concurrent identical closes reaching the venue **once**, a breached limit that
cannot trap an open position (with the override recorded), and a close in an
unsupported mode still refused.

## C-3 · The existing guard test does not guard this — CRITICAL (contributing) · **FIXED**

`tests/test_positions.py::test_the_broker_executor_reaches_no_terminal_of_its_own`
AST-parses the module and asserts only that it imports nothing named
`MetaTrader5`/`mt5`. **Nothing asserts an Approval, a RiskEngine call, or an OMS
submission.** The test passes on the broken code and reads, to anyone scanning
the suite, as though the path were guarded.

### The fix

Two tests were added beside it, one negative and one positive, because
asserting the absence of a shortcut is not the same as asserting the presence
of the real path — a module that did neither would pass the first:

* `test_the_broker_executor_never_touches_an_adapter_directly` — no attribute
  named `adapter` is read anywhere in the module. That is the thing that was
  actually wrong, and it is now the thing the test checks.
* `test_the_broker_executor_goes_through_risk_and_the_oms` — `approve_close`,
  `create`, `close` and `guard_resend` are all called.


## C-4 · The Approval binding check is vacuous — CRITICAL · **FIXED**

**L45 category: "RiskEngine bypass" (latent).**

`app/oms/service.py:822`:

```python
if not approval.binds(approval.bound_fields()):
```

`bound_fields()` (`app/risk/engine.py:259`) derives **entirely from the approval's
own proposal**. So the check digests the approval and compares it to the
approval's own hash. **It is always True. The check can never fire.**

Its error message says otherwise, and that is the dangerous part:

> "the approval does not bind to this order; a change to the account, symbol,
> side, volume, price, stop, target or strategy requires a fresh risk evaluation"

It never looks at *this order*. `create()` takes `account_id`, `order_type`,
`time_in_force` and `bot_id` **from the caller** (`service.py:185-197`) and
writes the caller's `account_id` onto the order (`service.py:245`).

**Stated precisely, because the severity must not be overstated:**

* **Not exploitable today.** Both call sites pass one account through: the API
  uses `body.account_id` for the manager, the proposal and `create()`; the
  pipeline uses `signal.account_id` for both. There is no live path that
  substitutes an account.
* **`order_type` IS genuinely unbound.** `bound_fields()` hardcodes
  `"order_type": "market"` while the API accepts `market|limit|stop`
  (`orders.py:159`). Risk evaluates a market order; the OMS may create a limit
  or stop order with different execution semantics, and the binding cannot
  notice.
* **Latent RiskEngine bypass.** Any future caller — a bot, a new route, a
  worker — that passes an `account_id` other than the proposal's is silently
  authorized. The defence built to catch exactly that cannot fire.

**A safety check that cannot fail is not a safety check.** It is worse than an
absent one, because it reads as protection in every audit.

**Fix:** pass the order's real fields to `binds()` rather than the approval's,
and include the caller's `order_type` in the bound set.

### The fix as applied

`Approval.binds_order(account_id=, order_type=, mode=)` overrides exactly the
three fields the *caller* controls and the approval cannot assert, and
`bound_fields()` now carries a docstring warning that it is not "the order" and
must not be validated against on its own.

The OMS validation was split in two so the messages stay precise:
`_validate_approval` answers "is this a genuine, live approval at all?"
(identity and expiry, first, before anything reads `approval.proposal`), and
`_validate_binding` answers "does it bind to *this* order?" — deliberately
after the shape checks, so `order_type="iceberg"` still reports an unsupported
order type rather than a binding failure that is true but unhelpful. Both
`app/oms/service.py` and `app/paper/oms.py` use it, in `create` and `modify`.

**Regression:** four tests, including
`test_the_binding_check_is_capable_of_failing` and
`test_bound_fields_alone_is_never_a_valid_binding_check`, which walks the AST
for a `.binds(...)` call whose single argument is a `.bound_fields()` call —
the exact tautology, in a form that prose cannot accidentally satisfy. (The
first draft of that test matched its own docstring, which is why it reads the
syntax tree rather than the text.)

**Applying the fix surfaced 55 test call sites** that built a proposal for
`acct-a` and then called `create(account_id="a")`. Production was checked first
and is consistent — the API uses `body.account_id` for both, the pipeline uses
`signal.account_id` for both — so the tests had always exercised the defect
rather than the behaviour. Aligning them was not weakening them.

## F-1 · The TradingView chain cannot complete in the deployed wiring — HIGH · **FIXED**

Not a safety defect — it fails closed — but it materially changes what "verified
end to end" has ever meant.

`app/webhooks/gateway.py` **never writes `account_id`** into a signal's `meta`
(verified: no occurrence in the file). `app/main.py:178-181`'s
`_to_incoming_signal` returns `None` without one, and the worker then retires the
row as `signal_not_executable`.

**Measured on the deployed database: all 118 signals sit at `status = new`** —
none has ever been executed, because the execution worker is registered and
deliberately not started (an operator action since L38).

So the headline flow *TradingView → … → order* has never been able to complete
in the deployed configuration, independent of the absent broker. L40 and L41
verified the webhook half and the pipeline half **separately**; nothing ever
joined them through the worker, and the join is where the gap is.

This narrows C-1's reach today — no signal reaches the pipeline via the worker —
while leaving C-1 fully live on the API order path.

### The fix

**It was five missing links, not one.** Measured by feeding the gateway's own
`meta` to the real pipeline built with `app/main.py`'s arguments: `account_id`
alone moves the failure to `signal_invalid` ("no symbol on the signal"), then
to `sizing_refused` twice for two further reasons. Fixing the field F-1 names
would not have fixed F-1.

Four were facts the platform already held and never recorded. The gateway now
writes `internal_symbol` (the platform's code, not the alert's ticker),
`strategy_id` (the key `strategy_state()` is keyed by), and — via the new
`app/execution/routing.py` — `account_id`, `bot_id` and `risk_amount`
resolved from the `bots` row.

**The account comes from the bot and never from the alert.** A payload that
could name an account could name somebody else's. `bots` already carried
`strategy_version_id`, the account, `mode` and `max_risk_per_trade`; **nothing
new was modelled.** `app/main.py`'s own comment described the intended design —
limits "replaced per pass by the worker's caller once a bot configuration
exists" — and that caller was never written. Ambiguity (two enabled bots on one
strategy version) is refused and both are named, never resolved.

**The fifth link was a policy, not a fact, and it needed a decision.**
`fixed_risk` sizing needs a stop distance and does not invent one. An
ATR-derived bracket is the better answer and **cannot be computed** —
`market_bars` covers one instrument — which left the alert's own suggestion as the only
bracket that exists. That suggestion is quarantined under `advisory_ignored`
deliberately, because under `fixed_risk` a tighter stop produces a **larger**
position: it is an input that moves size upward.

So it is an explicit per-bot opt-in, **false by default** —
`bots.use_alert_bracket`, migration 0026, a typed column rather than a key in
an untyped blob because "where did this order's stop come from" is exactly what
somebody queries after an incident. Nothing is turned on. `max_risk_per_trade`
still caps the money at risk, the RiskEngine still evaluates the order, and
`bracket_source` is recorded on every routed signal — including when there is
no bracket.

Without the opt-in the signal is refused at sizing with a precise reason, which
**retires** it rather than parking it, so nothing loops.

**The headline flow is now proven end to end for the first time.**
`test_an_alert_becomes_an_order_through_the_deployed_wiring` posts an alert
over HTTP and follows it to a filled order with a durable `orders` row, through
the application's own `ExecutionPipeline`. Before this work that test could not
have been written.

**The execution worker is still registered and not started** — an operator
action since L38. Nothing here begins trading.

**Regression:** `tests/test_signal_routing.py` (11) and four gateway tests in
`tests/test_webhooks.py`, including an alert that tries to name its own account
and risk budget.

---

## What the gate found working

Recorded so the failures above are not mistaken for a general verdict. Each was
attacked and held:

* **Live trading cannot be enabled by any single change.** Attacked and
  **NOT refuted**. Seven independent locks; with `TRADING_MODE=live` *and*
  `LIVE_TRADING=true`, `live_execution_allowed` is still False with 10 blockers.
* **No secret can be exposed** through source, logs, API, health or the frontend
  bundle. Attacked and **NOT refuted**.
* **Duplicate webhooks, within a process, do not duplicate.** Measured under
  load this session: 50 identical alerts concurrently → exactly 1 signal id, 0
  × 5xx; 118 signals / 118 distinct keys; **0 orders created**.
* **Unauthenticated writes reach nothing.** 100 concurrent → 0 × 2xx.

---

## Load and capacity, measured this session

First load testing ever performed on this platform. Against the deployed stack
through nginx:

| Scenario | Result |
|---|---|
| `GET /health/live` sequential | p50 **6.9 ms**, p99 27 ms |
| `GET /health/ready` sequential | p50 **103 ms** (touches DB + Redis) |
| `GET /health/live` × 200 concurrent | ~98 req/s, p99 **1.96 s** |
| Webhook × 25 concurrent distinct | 25/25 accepted, p50 1.2 s |
| Webhook × 50 identical | **1 signal id**, 0 × 5xx |
| Webhook × 200 flood | 0 × 5xx, 193 × 429 |
| `POST /v1/orders` × 100 unauthenticated | **0 × 2xx** |

**Data integrity after ~600 concurrent requests:** 118 signals / 118 distinct
keys, 125 events / 125 distinct idempotency keys, **0 orders created**, 252
historical trades untouched.

### A deployment defect found and fixed by this testing

The nginx rate limit I added at L41 returned **503** for rate-limited webhooks,
and dropped **21 of 25** legitimate distinct alerts at a modest burst.

L40 established that **TradingView retries on 5xx** — so the proxy's own rate
limit triggered exactly the retry storm the gateway exists to absorb, and
silently dropped real trading signals from a multi-symbol strategy firing at one
bar close.

Fixed: `limit_req_status 429` (the truth — "you are going too fast" — which a
well-behaved sender backs off from) and a burst sized for a realistic
simultaneous multi-symbol fire. **Re-measured: 0 × 5xx across every scenario**,
every refusal now a correct 4xx.

---

## Why a pilot could not proceed even without C-1 to C-3

Stated because it is independent of the defects and does not go away when they
are fixed:

1. **No broker has ever been connected.** A demo adapter exists
   (`app/brokers/mt5.py`, demo-fenced) and has never spoken to a terminal.
   **A production pilot with no venue is not a pilot.**
2. **No backup has ever been restored** (`FINAL_RISK_REGISTER.md` H-1). A pilot
   generates records that cannot be recovered.

---

## Required before this gate is re-run

1. ~~**Fix C-1.**~~ **Done.** The pipeline persists its orders, so
   `orders.intent_id` UNIQUE protects the automated path and startup
   reconciliation can latch safe mode. The durable guard is consulted before
   every create.
2. ~~**Fix C-2.**~~ **Done.** The close is routed through `approve_close`,
   `create` and `close`; it acquires an `intent_id` and an order record. The
   docstring was corrected in the same pass — it now states what the module
   does, including the four levels for which it did not.
3. ~~**Fix C-3.**~~ **Done.** The guard test asserts what it claims, in both
   directions.
4. ~~**Add a cross-restart regression test.**~~ **Done.**
   `tests/test_execution_durability.py`, with the pre-fix behaviour pinned as
   an assertion.
5. **H-1 and H-2 are unchanged, and they are now the top of the list.**

### What is still true, and is not affected by any of the above

1. **No broker has ever been connected.** A demo adapter exists
   (`app/brokers/mt5.py`, demo-fenced) and has never spoken to a terminal.
   **A production pilot with no venue is not a pilot**, and every fix above is
   verified against a simulator.
2. **No backup has ever been restored** (`FINAL_RISK_REGISTER.md` H-1).
3. **F-1 is fixed.** The chain routes an alert to a bot, an account, a risk
   budget and a bracket, and is verified end to end to a filled order through
   the deployed pipeline. What a deployment must still supply is a registered
   broker adapter and an `mt5` contract spec — neither of which exists here,
   because no venue does. The bracket opt-in is off for every bot and turning
   it on is an approval (`SIGNAL_ROUTING.md`).

**These were changes to the execution core, and the earlier note here — that
rushing them is how a fix becomes the next defect — was right.** What made
them safe to make was doing them one at a time, each against a test that fails
on the old code, and letting the test suite find the three consequential
disagreements it found: the validation ordering, the UNIQUE-vs-resend conflict,
and the OMS's refusal of a vetoed verdict. Each of those was a real design
question the first draft had got wrong.

---

## Note on Levels 46, 47 and 48

L46 and L47 analyse a pilot's operational history — latency, fill rates,
slippage, `INTERNAL vs MT5 vs BROKER` position state, pilot P&L and drawdown.
**No pilot has run and no venue has ever been connected, so that data does not
exist.** Producing `PILOT_STABILITY_REPORT.md` or
`POSITION_RECONCILIATION_REPORT.md` from it would be fabrication, which those
briefs forbid.

L48's scale and concurrency phases do **not** depend on a venue, and the
executable parts of them were run: load, throughput, idempotency under load,
backpressure, unauthenticated flood, and data integrity after stress. Those
results are above, and they found both the nginx defect and — through the
adversarial audit run alongside them — C-1 and C-2.
