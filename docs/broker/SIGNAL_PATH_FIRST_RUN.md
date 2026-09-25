# The signal path, first run against a real venue

2026-09-07, continuing `DEMO_VENUE_LIFECYCLE.md`. Every order before this was a
manual `POST /v1/orders`. This is the other half: a TradingView alert carried
through the gateway, the execution worker, the strategy stage, the AI seat,
risk, sizing and the OMS to MetaTrader — the path that decides *what* gets
traded rather than *how*.

Everything below is copied from the database, the API responses and the
terminal.

## The chain, end to end

```
POST /v1/webhooks/tradingview   secret in the body, as TradingView sends it
  -> WebhookGateway             signal 3c98699c, mode demo, auth strong
  -> ExecutionWorker            claimed it in the same transaction that read it
  -> route_for                  bot c8461d2a -> broker account 8ea1cacb
  -> ExecutionPipeline          strategy, AI seat, RISK, SIZING
  -> OrderManager               order tv:id:demo-signal-1788782446
  -> MT5Adapter -> MetaTrader   ticket 58332563074, filled 0.01 @ 1.16237
  -> positions row              long 0.01 @ 1.16237, sl 1.16038, tp 1.16438,
                                source=pipeline
  -> POST /v1/positions/{id}/close
                                CONFIRMED, realized_pnl +0.02 (the venue's own)
```

The alert named a strategy and a price. **It did not name an account** — it
cannot, and must not: the account is a routing decision the platform owns, and
a payload that could select an account could select somebody else's.

## What was missing, and what the platform told me

### 9. No demo bot could exist — FIXED

`route_for` hands a signal to the one enabled bot for its strategy version in
the platform's mode. There was no such bot and **no way to create one**: the
only bot-creation route is `/v1/paper/bots`, which hard-codes `mode="paper"`
deliberately — *"the mode is not a field a caller can set."*

So the routing join L45 built could never be made for a broker account. The
same shape as `_ADAPTERS` this morning: the mechanism exists, nothing can fill
it.

**Fix:** `POST /v1/bots`, demo only, with the fences the venue registration
uses — the platform's own mode (never the caller's), an active
`broker_accounts` row whose `account_mode` matches, an existing strategy
version, step-up re-authentication, a reason, a security event and an admin
audit row. It refuses a second enabled bot on the same strategy version rather
than letting `route_for` discover the ambiguity later.

Created enabled, deliberately: `is_enabled` means "may be started", `route_for`
requires it, and a bot created disabled with nothing able to enable it would be
the seat nobody can sit in all over again. The deliberateness lives in the
step-up and the reason.

### The platform diagnosed the next two itself

The first alert was recorded and then retired `signal_not_executable`. The
signal's own metadata said why, in the row:

```
account_id:     8ea1cacb-...        <- routed correctly
bot_id:         c8461d2a-...        <- to the new bot
risk_amount:    2.0000              <- from the bot's max_risk_per_trade
price_reported: None                <- the alert carried no price
bracket_source: none
bracket_detail: "this bot has not opted into the alert's bracket, and the
                 platform has no bracket policy of its own; sizing will
                 refuse this signal"
```

Both are designed knobs, not defects. The alert needs a price, and the bot must
opt in to the alert's bracket through `POST /v1/bots/{id}/bracket-source` —
which required its own step-up scope (`BOT_BRACKET_SOURCE`), because promoting
an external payload's stop to the platform's own bracket lets that payload
decide position size.

The response says what stays true afterwards: *"The alert's suggestion is still
recorded separately under advisory_ignored; what was suggested and what was used
stay independently auditable."*

### 10. The pipeline filled without recording a position — FIXED

The first complete alert executed: order `tv:id:demo-signal-1788782233`, ticket
58332500531, filled 0.01 @ 1.16231, the alert's bracket applied at the venue.
**And no `positions` row.**

`record_fill` had been wired into the manual `/v1/orders` route at L70d and
nowhere else, so the gap that left `PositionManager` unable to manage what the
platform opened was still standing on the path that actually trades.

**Fix:** `DatabaseOrderStore.record` now writes the position in the same session
as the order it came from, so an order recorded filled and the position it
implies cannot disagree. It resolves the `broker_accounts` row rather than
trusting `ManagedOrder.account_id` — one indexed read against the mistake
`orders.signal_id` made.

Proved on the second alert: ticket 58332563074, `positions` row `long 0.01 @
1.16237 sl 1.16038 tp 1.16438 source=pipeline`, closed through the platform for
the venue's own **+0.02**.

## What behaved correctly, verified

* **The gateway refused nothing it should have accepted and accepted nothing
  blindly** — 200 with `"a recorded signal, not a trade; nothing was executed"`.
* **The worker claimed the signal in the same transaction that read it**, so two
  workers could not both take it.
* **Routing refused to invent an account.** The alert named a strategy; the bot
  named the account.
* **`max_risk_per_trade` came from the bot**, not from the alert. The alert's
  suggested size stays quarantined under `advisory_ignored`.
* **Step-up was required three times** — venue registration, bot creation,
  bracket source — each scoped to its own subject and consumed by one use.
* **The venue's own P&L** was booked on the close, not a price difference.

## Known and not worked around

**One position from the first alert is open at the venue with no platform
record** — ticket 58332500531, opened before the ingestion fix, carrying a valid
20-pip bracket. It is left rather than closed out of band. Note that the
`unattributed` report above cannot help here and is not meant to: that is a
position the platform has no row for, and this is a row that names no account.
The venue's own sweep already flags it `unexpected_at_broker`.

**A position with a NULL `broker_account_id` was invisible to the account-scoped
reconciliation sweep — FIXED.** One legacy row (58326177606, from when the
harness harvested a platform position before accounts existed) sits `open` in
the platform and closed at the venue, and `POST
/v1/positions/reconcile?account_id=` could not see it because it belongs to no
account. It was not filtered out by a rule anybody could read: `_local` scopes
by account, and a row matching no account simply never appeared.

The sweep now reports it. `unattributed` lists every live position in the
sweep's mode that names no account at all, and a report carrying one is **not
`clean`** — a sweep that says everything agrees while a position sits outside
every account it could have swept is describing a subset and calling it the
whole. It is reported and never touched: this sweep holds one account's adapter
and cannot know the orphan was held there, so closing it or attaching it here
would be exactly the attribution the reconciler refuses to make when it declines
to adopt an unexpected broker position.

**The bracket came from the alert.** That is one of two supported sources and it
is opt-in per bot, but it means this run's stop was TradingView's suggestion
rather than a bracket the platform computed. A strategy with its own bracket
policy would not need the opt-in.

## Status

    A TradingView alert now becomes a real order at a real venue through the
    whole platform, and the position it opens is recorded, reconciled and
    closeable.

    Both halves — manual and signal-driven — are proven end to end.
