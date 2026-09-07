# BROKER_REGISTRATION.md

How an operator points the platform at a venue. Added at L51, 2026-09-06.

---

## The gap this closed

`OrderManagerRegistry` and `BrokerRegistry` are empty at startup by design —
"registering an adapter is an operator action rather than a side effect of the
API booting". **But no operator action existed.** There was no route, no admin
command and no configuration that could fill them.

That was found by starting the execution worker: every routed signal reached
the OMS stage and stopped at

> no order manager is registered for account …. An adapter is registered by an
> operator, not by the API booting.

The same shape as the defects this session fixed — a seat built, and nobody
able to sit in it.

## Why the write went here, and why it was owed

`app/api/v1/brokers.py` was read-only for eleven levels, and its own docstring
said why:

> The prompt for this level is explicit that execution must run
> Risk Engine → Position Sizing → OMS → BrokerAdapter, and three of those four
> do not exist. […] A read-only broker route added now cannot grow a write
> later by accident, because **the write has to be added deliberately in the
> level that also adds the veto in front of it.**

All four now exist and are verified. The write was added as that paragraph
required, and the docstring was rewritten to say what the module does — leaving
it claiming "there is no write path here" would have been the C-2 failure
exactly: a confident docstring describing code that no longer matches it.

## What the route does

```
POST   /v1/brokers/adapters            register a venue for one account
DELETE /v1/brokers/adapters/{account}  remove it
```

Both require `manage_brokers`, **step-up re-authentication**
(`BROKER_CREDENTIALS`), and a reason of at least eight characters. Both write a
security event and an audit row.

**Registering fills BOTH registries.** The execution path reads
`OrderManagerRegistry`; the observation routes read `BrokerRegistry`. Filling
one would give a platform that reports a healthy venue and refuses every signal
with `no_venue`, or the reverse.

**Registering sends nothing.** `POST /v1/orders` remains the one submission
door and the RiskEngine still stands in front of it. A test asserts the order
count does not move.

## A safety test was deliberately changed, and how

`test_there_is_no_write_route_on_the_broker_surface` asserted
`methods <= {GET, HEAD}` for every `/v1/brokers` route. Adding the write broke
it, which is exactly what it was built to do.

**It was re-aimed, not relaxed.** "No writes" was a proxy for the property that
actually matters — *this surface cannot send anything to a venue* — and the
proxy stopped being the property once a control-plane write was owed. Two tests
replace it:

* `test_the_broker_surface_carries_no_write_but_registration` — an **exact
  allow-list** of `(path, method)` pairs, not a prefix or a pattern. A new write
  here has to be added by name, which preserves the deliberateness the original
  rule protected.
* `test_the_broker_surface_cannot_reach_a_venue_to_trade` — AST-asserts that no
  route calls `place_order`, `close_position`, `cancel_order` or
  `modify_order`. This is the real rule, now stated directly rather than
  approximated.

Changing a safety assertion to accommodate new code is how a suite quietly
stops protecting anything. It is recorded here so the change is reviewable
rather than invisible.

## Only a simulator

`_ADAPTERS = {"simulator": "paper"}`. Not a temporary limitation:

* A real venue needs credentials — a login, a password, a server. A route that
  accepted them would be a route that stores them, and this one stores nothing.
  Credentials are a different seat.
* The adapter name is a **key**, never an import path. A route that took an
  import target would import whatever it was handed.
* The mode fence refuses when the adapter's mode is not the platform's, and
  `live` has no entry at all — so no value reaches a live venue even if
  `TRADING_MODE` were changed.

A test asserts `set(_ADAPTERS) == {"simulator"}` and that `mt5` is absent.

## What it refuses

* **A venue is never silently replaced.** An adapter swapped underneath a
  manager holding orders is a manager whose orders belong to a venue it can no
  longer ask about. Registering over an existing one is a 409.
* **Removal is refused while an order is unresolved.** Removing the adapter
  over an unknown order discards the only thing that can reconcile it: the
  platform keeps the record and loses the ability to ask. A test creates an
  `unknown` order and asserts the 409.

## Observed live

The full chain, against the running deployment:

```
register operator -> step up -> POST /v1/brokers/adapters   201, state=connected
alert -> gateway -> routed to bot/account/risk/bracket
      -> worker -> Risk -> Sizing -> OMS -> adapter
      -> ORDER CREATED and durably recorded
```

The order row, the first ever produced by the automated path:

| field | value |
|---|---|
| `intent_id` | `tv:id:l51-chain-final2` |
| `source` | `pipeline` |
| `paper_account_id` | the routed account |
| `quantity` | 0.20 — sized from the bot's budget and the alert's stop |
| `risk_decision_id` | present |
| `status` | `failed` |

Orders went 270 → 271. **Trades stayed at 252 and positions at 1: nothing was
executed.**

## The boundary that remains, and it is honest

The order failed with:

> NotConnected: no quote for EURUSD in the simulator

**A simulated venue has no prices, because the platform has no market-data
feed.** `market_bars` covers one instrument and `market_data.freshness` reports `UNKNOWN`.

Seeding the simulator with a made-up quote was considered and **rejected**: the
fills computed from it would flow into `trades` as invented history, beside 252
real ledger trades. That is fabricated trading data, and no amount of it being
"only paper" makes it safe to mix with the real record.

`failed` is the correct terminal state — it means the request provably never
reached a venue — so the chain stops safely and says why.

**The remaining work is a market-data feed, not more execution plumbing.** It
is also what blocks the ATR bracket policy (`SIGNAL_ROUTING.md`).

## Known limitation: registration is in-memory

A registered venue does **not** survive an API restart; both registries are
process state. An operator must register again after every restart, and the
`execution_worker_started` log line lists the registered `order_managers` so
the question "does this process have a venue?" is answered on one line.

Persisting it is a design decision nobody has taken: a venue that came back
automatically after a crash would be a venue reconnected without anybody
deciding to, which is the opposite of why the registries start empty.
