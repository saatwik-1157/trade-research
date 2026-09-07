# SIGNAL_ROUTING.md

How a TradingView alert becomes an order, and where it stops if it cannot.
Written 2026-09-06 with the L45 F-1 fix.

---

## What F-1 was

`app/webhooks/gateway.py` wrote none of the keys
`app/main.py::_to_incoming_signal` reads. It returned `None` for every alert
this platform had ever received, and the worker retired each one as
`signal_not_executable`. **Measured on the deployed database: all 118 signals
sat at `status='new'`.**

L40 verified the webhook half and L41 verified the pipeline half. Nothing ever
joined them through the worker, and the join was the gap.

**It was five missing links, not one.** Measured before the fix by feeding the
gateway's own `meta` to the real pipeline built with `app/main.py`'s arguments:

| Added | Result |
|---|---|
| the gateway's meta, as written | `_to_incoming_signal` returns `None` |
| `+ account_id` only | `signal_invalid` — "no symbol on the signal" |
| `+ internal_symbol` | `sizing_refused` — "stop distance is required" |
| `+ strategy_id` | `sizing_refused` — "stop distance is required" |
| `+ a bracket` | `sizing_refused` — "fixed_risk needs a positive risk_amount" |
| `+ the bot's risk budget` | **filled** |

Fixing only `account_id` — the field F-1 names — would have moved the failure
one step, not removed it.

---

## The chain as it now stands

```
TradingView alert
  -> gateway authenticates, deduplicates, resolves the symbol and the strategy
  -> route_for(): WHICH BOT runs this strategy version in this mode?
       exactly one enabled, non-disabled bot -> its account and risk budget
       zero, or more than one                -> recorded, NOT executable, reason stated
  -> signals row, meta carrying: internal_symbol, strategy_id,
     account_id, bot_id, risk_amount        (or: not_executable + why)
  -> execution worker claims it
  -> _to_incoming_signal() builds the pipeline's input
  -> ExecutionPipeline: nine gates, Risk -> Sizing -> OMS -> adapter
```

## Why the account comes from the bot and never from the alert

**A payload that could name an account could name somebody else's**, and one
that could set its own risk budget could set any number. Neither is in the
alert schema's vocabulary at all — an unknown key lands in `meta.extra`, which
nothing executes from — and a test asserts that rather than leaving it to
inspection.

`bots` already carried everything needed: `strategy_version_id`, the account,
`mode`, and `max_risk_per_trade`. **Nothing new was modelled**; L22 built it
and nothing read it. `app/main.py`'s own comment described the intended design
— limits "replaced per pass by the worker's caller once a bot configuration
exists" — and that caller was never written.

## Ambiguity is refused, never resolved

Two enabled bots on one strategy version is a configuration nobody has
finished. Picking one would send an order to an account nobody selected, so
`route_for` refuses and names both — the same rule
`OrderManagerRegistry.get` already states for adapters.

---

## The fifth link: the bracket, and the decision it needed

**A TradingView alert can now produce an order — but only for a bot whose
operator has explicitly opted in.** The default is unchanged and refuses.

The problem was a policy the platform did not have.

`fixed_risk` sizing needs a stop distance and does not invent one. The stop and
target an alert suggests are deliberately quarantined in
`meta["advisory_ignored"]`, and `meta["stop_loss"]` is meant to be *the
platform's* bracket — a distinction the design draws on purpose and which
`test_the_advisory_from_an_alert_is_carried_and_not_obeyed` pins.

Copying the alert's suggestion into the platform's field would have collapsed
that quarantine and let an external sender set a risk input. Under `fixed_risk`
a tighter stop means a **larger** position, so it is not a harmless default.

### What was decided, and why it was the only option that runs

Three were possible:

1. **An ATR multiple**, per bot or per strategy. The better answer, and **it
   cannot be computed**: `market_bars` covers one instrument, so there is no volatility
   series to derive one from. `CLAUDE.md`'s own research measured 1.5×ATR
   brackets extensively and found no configuration clearing significance, so
   the number would be a risk-control choice rather than an edge claim — but
   the point is moot until a feed exists.
2. **Obey the alert's suggestion, opted into per bot.** The only bracket that
   exists today.
3. **Require it on the bot's configuration**, so a bot without one does not
   trade. This is what 2 collapses to when the bot has not opted in.

**Option 2 was implemented, off by default.** `bots.use_alert_bracket`
(migration 0026) is a boolean column, false for every existing row, and an
operator turns it on for one bot at a time.

**Why it must be a decision rather than a default.** Under `fixed_risk` a
TIGHTER stop produces a LARGER position, so this hands an external sender an
input that moves size *upward* — the direction that matters. It also promotes a
value out of a quarantine the design built deliberately. What bounds it:
`max_risk_per_trade` still caps the money at risk, the RiskEngine still
evaluates the resulting order and can veto it, and both values stay separately
auditable — `advisory_ignored` keeps what was suggested, `stop_loss` records
what was used, and `bracket_source` says which.

`bracket_source` is written on **every** routed signal, including when there is
no bracket, so "where did this order's stop come from" has an answer on the
record rather than being reconstructed from which keys happen to be present.

Without the opt-in the signal is refused at sizing with a precise reason —
`sizing_refused`, "stop distance is required and must be positive" — which
**retires** it rather than parking it, so nothing loops.

### How an operator turns it on

`POST /v1/bots/{id}/bracket-source` with `use_alert_bracket` and a reason of at
least eight characters. It requires **step-up re-authentication**
(`BOT_BRACKET_SOURCE`), because a session cookie is not enough to lift a safety
quarantine, and it writes an audit row carrying the reason and both the old and
new value.

**Both directions are gated**, deliberately — otherwise somebody could flip it
back and forth below the audit trail.

A control nobody can reach is worse than one that does not exist, so there is a
test that a legitimate operator gets through as well as one that a bare cookie
does not.

### Still requiring approval

Whether to turn it on for any bot at all. **Nothing is on**, and no bot exists
in the deployed database.

---

## What did NOT change

* **The execution worker is still registered and not started.** That is an
  operator action and has been since L38. Nothing here starts trading; it makes
  the chain *able* to complete when an operator starts it.
* `TRADING_MODE=paper`, `LIVE_TRADING=false`.
* The alert still cannot set size, account, or risk budget.
* The RiskEngine still evaluates every resulting order and can veto it.

## The chain, proven end to end

`tests/test_webhooks.py::test_an_alert_becomes_an_order_through_the_deployed_wiring`
posts an alert over HTTP and follows it to a **filled order with a durable
`orders` row** — through the gateway, the `signals` table, the worker's own
`_to_incoming_signal`, and the application's OWN `ExecutionPipeline` (the one
`create_app` builds, with its store, spec loader and strategy state).

Two things are supplied that a real deployment must also supply and this one
does not have: a broker adapter registered against the account, and an `mt5`
contract spec for the symbol.

**Before this work that test could not have been written.**

## Regression

* `tests/test_signal_routing.py` — 11 tests: the routing decision, ambiguity
  refused, mode isolation, the alert's inability to name an account, the full
  chain to a filled order, and the bracket refused precisely.
* `tests/test_webhooks.py` — 4 tests through the real HTTP gateway, including
  an alert that tries to name its own account and risk budget.
