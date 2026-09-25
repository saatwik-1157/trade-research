# LEVEL 21 — POSITION MANAGEMENT

Completed 2026-09-04. Adaptive build mode: audit → baseline → plan → implement
→ integrate → test → verify → document.

The Position Manager answers *what position exists, and what should happen to
it?* — and nothing else. It generates no strategy, authorises no trade, and
reaches no venue: a close is an order, and it takes the same path any other
order takes.

---

## 1. What already existed

L21 was recorded as **COMPLETE (paper)** in `MIGRATION_STATUS.md` with one
named gap: *"demo/live executor — arrives with L10"*. The audit confirms that
was accurate, and the gap is the level's real work.

| Component | State before | Verdict |
|---|---|---|
| `app/positions/policies.py` | 7 exit policies, a `PRIORITY` order, trailing that only ratchets, pure `PositionView`/`MarketState`/`RiskContext` views | **KEEP + EXTEND** |
| `app/positions/manager.py` | Evaluate → act → record; confirmed / rejected / unknown contract | **KEEP + MODIFY** |
| `app/positions/monitor.py` | A `app.workers.Worker`, so a closed browser stops nothing | **KEEP**, untouched |
| `app/positions/executor.py` | `ExitExecutor` protocol, paper implementation, `UnavailableExitExecutor` | **KEEP**; the unavailable one is now superseded for demo/live |
| `app/brokers/reconcile.py` | `reconcile_positions` — reports, never repairs | **KEEP**, now used |
| `positions` / `position_events` | Tables, with an autoincrement event key chosen precisely so the log is orderable | **KEEP + MODIFY** |
| `GET /v1/positions` | Serves the platform's record, and says it is not the broker's book | **KEEP** |
| `POST /v1/positions/{id}/close` | 501 naming L19/L21 | **REPLACE** the handler |

**Nothing was duplicated.** No second position manager, tracker, exit manager,
P&L engine, reconciliation service or fill processor was created, and none
existed to merge.

---

## 2. The gap, and what closed it

**Demo and live positions could not be closed at all.**
`UnavailableExitExecutor` returned REJECTED with "no broker adapter is built
yet (level 10)" for every attempt. L10 built the adapter and L19 built the
OMS, so `app/positions/broker_executor.py` is the executor that uses them.

It holds a registry rather than an adapter — the same rule the OMS follows,
because an adapter handed out for the wrong account closes the wrong position.
It imports nothing MT5-shaped, and a test parses it to prove that.

Its three outcomes are the ones `CloseOutcome` already had, and the third is
the one that matters: **an unclear answer parks the position and nothing
retries it.** Retrying an uncertain close is how a position gets closed twice,
which on a hedging account opens a new one in the opposite direction.

---

## 3. What else was missing, and was added

### 3.1 Four position states

Three states could not express four real situations, so migration `0011`
adds them:

| State | Why it had to exist |
|---|---|
| `opening` | An order is in flight and nothing has confirmed. A position that exists locally before the venue confirms is one that may not exist at all — and `open` claimed it did. |
| `partially_closed` | Some was taken off. Distinct from `open` because the remaining size is no longer the size that was risk-sized. |
| `closing` | A close was requested and the venue has not confirmed it. "We asked" and "it is closed" are different facts. |
| `reconciling` | Being settled against the venue **right now**. Deliberately distinct from `unknown`: `unknown` means nobody is looking, this means somebody is, and only the second means an answer is coming. |

`NOT_ACTIONABLE` maps each to why a management pass must skip it. A test
drives all four.

### 3.2 Partial exits

`quantity` is what is open now; `initial_quantity` and `closed_quantity` are
the history it was cut from. **Separate columns, because a partial close must
not overwrite the original**: "70 open" and "100 opened, 30 closed" are
different facts, and only the second can be audited.

A partial close larger than what is open is **refused, not clamped** — the
difference between closing 30 of 70 and 30 of 100 is a position size nobody
chose. A database `CHECK` enforces `closed_quantity <= initial_quantity`, so
an over-close cannot become history even if a caller finds a way past the code.

### 3.3 Break-even, and partial take-profit

Both are **off by default**: each needs a level or a trigger the caller
chooses, and a default this platform invented would be a price nobody picked.

`BreakEvenPolicy` moves the stop and never closes — the same division the
trail already follows, so there is exactly one place a stop exit is decided.
It only ever tightens, and running it twice on one tick proposes nothing the
second time. Its `buffer` exists because a stop placed exactly at the entry
loses the spread every time it fires, which is not break-even.

The research note attached to it is worth keeping: the move-to-breakeven exit
was measured across a 16×8 grid at D1 and had a **median out-of-sample
expectancy of −123 points**, worse than the fixed bracket's −58
(`reports/exit_search_d1.json`). It is a risk control, not an edge.

`PartialTakeProfitPolicy` takes a fraction of the **initial** size, so a 0.3
rule takes 30 of a 100-lot position once — not 30, then 21, then 14.7. It
stops firing once that fraction has been closed, which makes §34's idempotency
a property of the arithmetic rather than a flag somebody has to clear.

### 3.4 The protection mismatch

`broker_stop_loss` and `broker_take_profit` are what the **venue** last
reported. `stop_loss` and `take_profit` are what this platform **intends**.

They are separate columns because a stop we asked for and a stop the venue is
holding are different facts, and **a silent disagreement between them is a
position running unprotected while the record says otherwise.** That is the
single most dangerous thing this module can observe, so it is recorded on
every pass it is true (`protection_mismatch`), logged at ERROR when the venue
holds no stop at all, and rendered in the UI as `1.09000 ✗` rather than as a
number that looks fine.

`broker_synced_at` of `None` means **never read**, which is not the same as
absent — reporting it as a mismatch would cry wolf on every position before
its first sync.

### 3.5 Position events on the existing bus

The L07 catalogue declared three `POSITION_*` types and named L21 as their
producer. One had a producer (`POSITION_CLOSED`, from the paper service); the
other two were documented, scoped, authorized and dead.

`app/positions/events.py` maps every recorded change onto one of the three,
and the mapping is consulted at the **one place** every change is already
written to `position_events` — so a change that is recorded is a change that
is published, and the two cannot drift apart. `_record` takes the row as its
**required first argument**, which is what makes that structural rather than a
convention: a call site cannot forget it.

Events are **queued and drained**, not published inline. A caller drains them
after the state is durable, which is the only order in which an event cannot
describe something that was not saved, and it keeps a slow bus out of the
middle of a venue interaction.

A scale-out publishes `POSITION_UPDATED`, never `POSITION_CLOSED` — the bus
must not say a trade is finished while size is still at the venue. And every
payload carries **both** the intended levels and the venue's, because a
subscriber shown only one could not tell a protected position from one that
merely believes it is.

### 3.6 Reconciliation

`app/positions/reconciler.py` reads the venue and applies
`reconcile_positions`, which already reports and never repairs. It corrects
exactly two things, and both are unambiguous:

- **A position the venue does not hold becomes `closed`.** The venue is
  authoritative for existence. What we cannot know is the price it closed at,
  so `realized_pnl` is left alone and the event records `exit_price: null` —
  inventing one would be the fabricated-figure failure this repository exists
  to prevent.
- **What the venue says about levels and sizes is written to the `broker_*`
  columns.** The intended levels are left exactly as they were, so the
  disagreement stays visible instead of being resolved by overwriting a side.

It never opens, closes, cancels or modifies anything at a venue — a test
asserts the module contains no call to `place_order`, `close_position`,
`modify_order` or `cancel_order`. It never adopts an unexpected broker
position: a position opened by hand is a fact to investigate, and silently
claiming it would attribute somebody else's trade to a strategy.

**When the venue cannot be read, it concludes nothing.** A reconciler that
decided a position was gone because the network was down is worse than one
that refuses to decide.

---

## 4. Decisions

### 4.1 The most protective stop wins, not the first

A trail and a break-even can both propose a move on the same tick. Taking
whichever policy ran first would make the outcome depend on list order, which
§20 forbids. `PositionManager._best_stop` takes the highest stop on a long and
the lowest on a short — deterministic, order-independent, and **incapable of
loosening**, because each policy has already refused to propose a loosening of
its own.

### 4.2 `close_now` is separate from `process`

`process` decides for itself; `close_now` acts on a decision the caller made —
an operator close, a risk exit. Split deliberately: a method that both decided
and acted could not offer "close this, because I said so" without also
re-running the policies, and an operator close a policy could veto would be an
operator close in name only.

Everything after the decision is `_act`, shared by both, which is what keeps
the confirmation contract in one place.

### 4.3 A null field never removes a stop

`POST /protect` sets the levels given and leaves the others alone. Removing
protection is a separate, explicit action, because **an API where a missing
field deletes a stop is an API where a typo does.**

A stop on the wrong side of the entry is refused rather than corrected — that
is a target, and it is the same rule position sizing applies at L18.

### 4.4 The executor is chosen by mode alone

Paper closes in the simulator; demo and live go through the account's order
manager to the adapter. There is no branch by which a paper position could
reach a venue or a demo position be closed by the simulator.

---

## 5. Changes, classified

**ADD** — `app/positions/broker_executor.py`, `app/positions/reconciler.py`,
`BreakEvenPolicy`, `PartialTakeProfitPolicy`, `PositionManager.close_now` /
`_act` / `_best_stop` / `_book`, `alembic/versions/0011_position_lifecycle.py`,
`POST /v1/positions/{id}/protect`, `POST /v1/positions/reconcile`, 32 backend
tests, 15 API tests, 1 frontend test.

**KEEP + MODIFY** — `policies.py` (`PositionView` gains account, broker id,
size history and the venue's levels; `ExitDecision` gains `quantity`;
`PolicySet` gains `stop_movers`), `manager.py`, `app/models/execution.py`,
`app/api/v1/schemas.py`, `app/api/v1/positions.py`, `app/paper/service.py`
(sets `initial_quantity` on creation), frontend positions table and service.

**REPLACE** — the `POST /v1/positions/{id}/close` 501 handler only.

**REMOVE** — nothing. `UnavailableExitExecutor` is kept: it is still the right
answer for a mode with no adapter, and deleting it would remove the honest
refusal that made the gap visible.

---

## 6. Safety invariants (§49)

| # | Invariant | How it holds |
|---|---|---|
| 1 | Broker state is authoritative | Reconciliation closes what the venue does not hold; fills come from the venue, never the quote a decision was made against. |
| 2 | No modification bypasses the OMS | The broker executor holds a registry and reaches the adapter through the account's manager. |
| 3 | Risk is not bypassed | Emergency and risk exits are first in `PRIORITY`; the risk engine remains the only producer of an `Approval`. |
| 4 | AI cannot modify a broker position | No AI seat exists in this package, and nothing here accepts a model's output. |
| 5 | TradingView cannot modify MT5 | An alert becomes a signal row; the execution pipeline is the only consumer. |
| 6 | Duplicate actions do not duplicate execution | Break-even and partial TP each propose nothing the second time by arithmetic. |
| 7 | Unknown requires reconciliation | `unknown`, `reconciling`, `closing` and `opening` are all refused by a management pass. |
| 8 | Disconnect stops unsafe orders | An unreadable venue makes reconciliation conclude nothing; a failed close is REJECTED or UNKNOWN, never assumed. |
| 9 | Browser closure stops nothing | `PositionMonitor` is a supervised `Worker`. |
| 10 | Protective stops are not silently removed | `protect` cannot clear a level; a venue not holding one is recorded, logged at ERROR and shown as ✗. |
| 11 | Trailing respects direction | Both stop movers refuse a loosening, and the manager takes the most protective proposal. |
| 12 | Partial close cannot exceed the position | Refused in the executor, in the API, and by a database `CHECK`. |
| 13 | Actual fill quantity is authoritative | `_book` applies the venue's fill price to the size that actually closed. |
| 14 | History preserved | Additive migration; existing rows backfill `initial_quantity = quantity`, which is exactly true of them. |
| 15 | Live disabled by default | Unchanged: `TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten gates false. |
| 16 | No secrets in logs | This package holds no credential; log lines carry ids, prices and reasons. |
| 17 | Stale data does not force a decision | A position with no quote is skipped and recorded, never closed. |
| 18 | Risk is the final authority | Unchanged. |

---

## 7. Tests

Baseline before L21 (the L20 verification run): backend **1087 passed, 16
failed, 3 skipped**. Fifteen of the sixteen are `redis.exceptions` because
Docker was not running on this machine; the sixteenth was a stale L19
assertion in `test_brokers.py` that `POST /v1/orders` returns 501, which this
level corrected — the door is built, and what it refuses now is reaching a
venue nobody registered.

`tests/test_api_v1.py` gains 15. The frontend suite goes from 82 to **83**.

`tests/test_positions.py` goes from 25 to **65**, including the brief's §48
scenario run literally: entry 100, stop 98, trail 3 → price 110 moves the stop
to 107, price 115 moves it to 112, price back to 112 closes on a confirmed
fill for a booked 1200. Every step goes through the manager, and the audit
trail shows two `stop_modified` entries followed by `exit_decided` and
`closed`.

The four that matter most:

- `test_a_stop_the_venue_is_not_holding_is_recorded_every_pass`
- `test_an_unclear_venue_answer_parks_the_close_and_never_retries`
- `test_reconciliation_concludes_nothing_when_the_venue_cannot_be_read`
- `test_the_trail_never_moves_the_stop_backwards_across_a_sweep`

---

## 8. Remaining, and honest about it

- **Unrealised P&L is not stored, deliberately.** It is a function of a price
  that changes every tick, and a stored one is wrong the moment it is written.
  The frontend shows realised P&L and the position's own figures; a live
  unrealised number needs the market-data subscription L08 lists as missing.
- **Hedging vs netting (§25) is not modelled.** The platform tracks positions
  individually with a `broker_position_id`, which is the hedging shape and is
  what this broker's demo account uses. A netting account would aggregate by
  symbol, and adopting that without an account to measure against would be
  guessing at a behaviour rather than implementing one.
- **The monitor is not started at boot**, the same as the execution worker.
  Starting position management is an operator action, and with no adapter
  registered a demo close would refuse anyway. L22 owns START/PAUSE/STOP and
  is what will start it.
- **Events are queued, not yet published to the hub.** `drain_events()` hands
  them to a caller; wiring that caller is the monitor's job once L22 starts
  it. Queuing them at the recording site is the part that had to be right,
  because that is what makes the audit log and the bus agree.
- **`POST /protect` sets the platform's intent; it does not push the level to
  the venue.** Pushing it is `adapter.modify_order`, which needs a broker
  order id per protective order — MT5 attaches SL/TP to the position, so the
  mapping is real work rather than a call. The disagreement it would create is
  exactly what `broker_stop_loss` and `protection_gap` now make visible, which
  is the honest intermediate state.
- **Demo and live are unexercised against a real terminal** — MT5 was not
  running here. The path is identical to the tested one by construction: the
  mode selects the executor, and the executor's only venue call is
  `adapter.close_position`.
