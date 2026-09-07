# First demo venue run

2026-09-07. The first time this platform's execution path reached a venue it
did not also write.

Everything below is a measurement. Order ids, tickets, prices and retcodes are
copied from the database, the API responses and the terminal's own log.

## What was proven

```
  POST /v1/orders  (manual, authenticated, step-up on the venue registration)
       -> SizingService     0.01 lots from a $2.00 risk budget and a 20-pip stop
       -> RiskEngine        APPROVE, decision 01b97fca-6ed9-4c0e-9223-a80bc576970e
       -> OrderManager      order 00943128-ecd6-42bb-accb-2264aa023601
                            durable as `intent` -> `submitting` BEFORE the send
       -> MT5Adapter        -> tools/mt5_paper.place()
       -> MetaTrader 5      ticket 58325115749, retcode 10009 "Request executed"
       -> fill              0.01 @ 1.16104, slippage 0.0 points
       -> modify            SL 1.15904 / TP 1.16304 applied at the venue
       -> position          LIVE at the venue, correctly bracketed
```

Verified independently at the terminal:

```
EURUSD BUY 0.01 ticket=58325115749 open=1.16104 sl=1.15904 tp=1.16304 magic=770315
```

The account: 5055473926 @ MetaQuotes-Demo, **DEMO**, USD. No real money exists
anywhere in this exercise, and `assert_demo` was the thing that let it through.

## What it found

Four defects, none of which could have been found any other way. The first two
were in the platform, the third in the toolkit, and the fourth is the reason the
first three survived.

### 1. `POST /v1/orders` could not write an order at all — FIXED

```
ForeignKeyViolationError: insert or update on table "orders" violates
constraint "fk_orders_signal_id_signals"
DETAIL: Key (signal_id)=(demo-venue-first-order-20260907-b) is not present in "signals".
```

The route set `OrderProposal.signal_id = <the Idempotency-Key header>`. That
value travels into `ManagedOrder.signal_id` and then into `orders.signal_id`,
which is a foreign key to `signals`. A manual order has no signal behind it, so
every manual submission on PostgreSQL failed with a 500.

**Fix:** `signal_id=None` in `app/api/v1/orders.py`. The idempotency key already
lives in `orders.intent_id`, which is UNIQUE and is what `guard_resend` and the
duplicate check actually read, so nothing is lost. The engine's
`duplicate_signal` check was inert on this route regardless — it reads
`PortfolioState.recent_signal_ids`, which the route never populates.

### 2. SQLite hid it — REPORTED, partly covered

174 API tests exercise this route and all of them passed. **SQLite does not
enforce foreign keys unless `PRAGMA foreign_keys=ON` is issued per connection,
and nothing in the suite issues it.** PostgreSQL always does.

So the suite could not have caught this, and cannot catch the same class of bug
in any other column either.

`tests/test_orders_foreign_keys.py` turns the pragma on for one file and covers
`orders.signal_id` specifically, including a test that asserts the pragma is
actually in force so the file cannot silently stop proving anything. **The
general gap is open** — see `KNOWN_TEST_LIMITATIONS.md`.

### 3. Every bracketed order would have been opened and immediately closed — FIXED

`MT5Adapter.place_order` calls the toolkit with ATR multiples of `0.0`,
intending "no bracket — I will set absolute levels afterwards with a modify".
The toolkit had no such convention:

```python
sl = price - sl_atr * atr    # 0.0 * atr == 0  ->  sl = price
tp = price + tp_atr * atr    #                     tp = price
```

so it sent `sl == tp == entry`. Observed twice, on consecutive attempts, with
*different* outcomes — which is why one attempt is not evidence:

| attempt | retcode | outcome |
|---|---|---|
| `...-c` | **10016** INVALID_STOPS | rejected outright, recorded `rejected` with the venue's reason |
| `...-d` | **10009** accepted | filled, then `bracket_is_sane` found the zero-width bracket, the repair returned 10025 NO_CHANGES, and the toolkit **closed the position** |

The second is the toolkit's own safety net working exactly as its comment says
it should: *"Could not fix it and cannot leave it: both exits are against the
position, so holding is a guaranteed loss with no upside branch."* It cost
nothing — opened and closed at 1.16107 for 0.00.

**Fix:** in `tools/mt5_paper.place()`, a multiple of zero now means **no
bracket**: `sl`/`tp` are sent as `0.0`, which is MT5's "not set", and the
sanity/repair block is skipped when no bracket was requested. The harness's own
path is untouched — it always passes 1.5/1.5.

After the fix, the same order filled and **kept** its position, with the
platform's own levels applied by the follow-up modify. That is the run recorded
at the top.

### 4. A broker-mode fill created no `positions` row — FIXED, one dependency left

`grep` for what constructs a `Position`:

```
app/paper/service.py:1002:   Position(
```

That is the only one. **No demo or live fill anywhere in the platform creates a
`positions` row** — not `POST /v1/orders`, and not `ExecutionPipeline`.

Consequences, all observed:

* `PositionManager` cannot see the position, so no stop move, no trailing, no
  break-even, no time exit and no emergency exit applies to it.
* `POST /v1/positions/{id}/close` cannot close it — the route starts from a
  `positions` row and there is none.
* Reconciliation reports `internal_positions: 0` against `broker_positions: 8`,
  every one of them `unexpected_at_broker`.

The reconciler is behaving correctly; it is describing a real gap rather than a
fault of its own. But it means **the platform can open a broker position and
then cannot manage or close it**, which is a prerequisite for anything past a
single manual order.

**Fixed**: `app/positions/ingest.py`. One function, `record_fill`, called from
the order route in the SAME transaction as the order it came from, so an order
recorded filled and the position it implies cannot disagree. It writes only on a
confirmed fill, never for paper, and is idempotent on the venue's ticket. 24
tests, plus a parse test proving the module imports no broker and no OMS -- it
records a fill that already happened and must not be able to cause one.

`app/api/v1/brokers.py::reconcile` now compares against those rows instead of
the hard-coded empty list it had passed since L10.

Proved on a second real order, ticket **58326177606**, 0.01 EURUSD at 1.16118:

```
positions row: broker_position_id=58326177606 side=long quantity=0.01
               entry=1.16118 sl=1.15918 tp=1.16318 status=open source=manual

reconcile:     broker_positions 8, internal_positions 1, unresolved []
               7 unexpected_at_broker -- the harness's own, correctly flagged
               58326177606 NOT flagged: the platform and the venue agree
```

**What is left: the platform still cannot CLOSE it.**
`POST /v1/positions/{id}/close` refused, and the refusal is correct:

    no usable quote for <symbol_id>; refusing to close against a price the
    platform does not have

Two things stand behind that, both structural rather than bugs:

* **The close route prices from `app.state.market_data`**, the normalized feed,
  never from the broker book -- the two are documented as different
  measurements that are "never merged". That feed holds 705 bars across two
  symbols and no live quotes, so there is no price to close against.
* **A position cannot name its venue.** The only column that could is
  `positions.broker_account_id`, a foreign key to `broker_accounts` -- a table
  with **zero rows**, because registering a venue fills two in-process
  registries and creates no account record. So even a route willing to use the
  venue's own book could not work out which adapter to ask.

Closing needs one deliberate decision (may a broker-mode close price off the
venue's own book?) and one piece of lifecycle (does registering a venue create a
`broker_accounts` row?). Neither was invented here.

## What behaved correctly, verified rather than assumed

* **Step-up re-authentication** was required for the venue registration, was
  scoped to the account, and was consumed by one use.
* **`expect_account`** compared the connected login against the named one.
* **The mode fence** refused `mt5_demo` while the platform ran paper.
* **The RiskEngine** approved with a bound, expiring approval and recorded a
  coded decision — and listed **21 limits as `not_enforced`**, because none is
  configured. It did not treat "unset" as "passed"; it said so in the record.
* **The OMS** wrote the order before the send, recorded the venue's own
  rejection reason and retcode on attempt `-c` (`Invalid stops`, 10016), and
  did not retry it.
* **Reconciliation** found the seven pre-existing positions, called them
  `unexpected_at_broker`, and **closed and adopted nothing** — the detail it
  emits says why: *"closing it would close somebody's trade"*.
* **The toolkit's bracket safety net** closed a position it could not protect.

## The shared MAGIC

`tools/` and `MT5Adapter` both tag orders `MAGIC = 770315`, deliberately, so
both see the same positions. The consequence is that **each can close the
other's**: the harness harvests any position with that magic at
`net floating >= $0.50`.

The harness was stopped for the duration of this run and restarted afterwards.
It is now running again and holds 8 positions — its own seven plus the
platform's one, which it will treat as its own and harvest. On a demo account
that is harmless; with two systems that both send orders it is the thing to fix
before either runs unattended alongside the other.

## Housekeeping

* Operator account `demo-venue@tr-platform.io` (admin) was created to drive the
  API. Delete it when it is no longer wanted.
* `python -m app.auth.bootstrap` **fails on its own** with
  `NoReferencedTableError: users.role -> roles`: it does not import `app.models`
  before the mappers configure. Worked around, not fixed.
* The venue was deregistered and the host API process stopped. The
  containerised stack is untouched and still in paper mode with 12 live
  blockers.
* One EURUSD 0.01 position (ticket 58325115749) remains open at the venue with
  a valid 20-pip bracket. It was left rather than closed out of band.

## Status

    The platform has executed real orders end to end against a venue it did
    not write, and now RECORDS the positions they create and reconciles them
    correctly against the venue.

    It cannot yet CLOSE one: the close route prices from a market-data feed
    with no live quotes, and a position has no way to name the venue it is
    held at. Both are named above; neither was worked around.
