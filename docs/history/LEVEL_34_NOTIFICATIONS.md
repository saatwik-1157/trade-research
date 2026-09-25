# Notifications and alerting (L34)

Built 2026-09-05. One event bus, one notification service, one record per
person per event — and a message that never contains a figure the platform did
not record.

---

## 1. The audit: what already existed

Section 2 asks what is there before anything is built. Nine of the twelve
pieces this level needs already existed, which is why L34 is mostly wiring.

| Component | Where | Verdict |
|---|---|---|
| Event bus (Redis pub/sub + in-process) | `app/core/events.py` | **KEEP**, reused. No second bus. |
| Realtime hub, one bus reader per process | `app/realtime/hub.py` | **KEEP**, untouched |
| Event catalogue, 49 types with scopes | `app/realtime/catalogue.py` | **KEEP + MODIFY** — one line: `NOTIFICATION_CREATED` joins `PRODUCED_NOW` |
| `user` channel scope, authorized to that user | `app/realtime/channels.py` | **KEEP**, untouched — it already had the rule |
| `NOTIFICATION_CREATED` event type | catalogued at L07, no producer | **KEEP** — L34 is the producer it was waiting for |
| `notifications` table | `app/models/ops.py`, migration 0002 | **KEEP + MODIFY** — nine columns added, nothing dropped |
| Alert dedup + cooldown reasoning | `app/monitoring/alerts.py` (L29) | **KEEP**, its reasoning reused with a different key |
| Worker base: loop, heartbeat, registry | `app/workers/base.py` | **KEEP**, reused for delivery |
| Rate limiter, two backends | `app/auth/ratelimit.py` | **KEEP**, reused on the two write routes |
| Pagination with a hard ceiling | `app/api/pagination.py` | **KEEP**, reused |
| `ResetDelivery` port with a refusing stub | `app/auth/reset.py` (L04) | **KEEP + IMPLEMENT** — see §9 |
| `notificationService` frontend stub | `frontend/src/lib/services.ts` | **REPLACE** with the real client |
| `/notifications` 501 route | `app/api/pending.py` | **REMOVE** — a real router took the path |
| `/alerts` PlannedPage | `frontend/src/app/alerts/page.tsx` | **REPLACE** with the notification centre |
| Discord, anywhere | — | **does not exist.** Two placeholders name it; neither is code. |
| Email / SMTP, anywhere | — | **does not exist.** `ResetDelivery` is the port waiting for one. |
| `app/notifications/*`, `notification_deliveries`, `notification_preferences` | — | **ADD** |

**Nothing was replaced blindly.** The one genuinely awkward finding is
described in §10.

---

## 2. The architecture

```
  RiskEngine / OMS / BotManager / Journal / Review / Monitoring / Portfolio
                                  │
                                  │  publishes a DOMAIN EVENT
                                  ▼
                     app/core/events.py  (the L07 bus)
                          │                      │
          ┌───────────────┘                      └──────────────┐
          ▼                                                     ▼
   app/realtime/hub.py                          app/notifications/worker.py
   fans out to browsers                         NotificationConsumer
   (never writes)                                       │
                                                        ▼
                                         app/notifications/service.py
                                            1 rule lookup
                                            2 recipients
                                            3 severity
                                            4 environment
                                            5 template
                                            6 dedup (per event)
                                            7 cooldown (per condition)
                                            8 PERSIST
                                            9 preferences
                                           10 queue a delivery per channel
                                                        │
                        ┌───────────────────────────────┼───────────────────┐
                        ▼                               ▼                   ▼
                 IN_APP (inapp.py)             EMAIL (email.py)      DISCORD (L35)
                 stores + publishes            SMTP provider          NOT_CONFIGURED
                 NOTIFICATION_CREATED          behind a port
```

**Two subscribers, one bus.** The hub fans events out to browsers and is
deliberately incapable of writing anything — its own docstring says *the hub
delivers; it never decides*. Giving it a database session would put a write
path inside the object whose design property is that it has none. So the
consumer opens its own subscription onto the same bus. One Redis, two readers,
different jobs.

---

## 3. Which events become notifications

`app/notifications/catalogue.py` holds one `Rule` per event type: its category,
its audience, its severity, the entity it is about, and its cooldown.

**L34 invents no event.** Every type in the table already exists in
`app/realtime/catalogue.py`. A test asserts `RULES ⊆ EventType` and that
`RULES ∪ NOT_NOTIFIED` covers the catalogue exactly, so a type added later is a
test failure rather than a silent omission.

**Most have no producer yet, and the contract says so.** `live_now()` reads
`PRODUCED_NOW` — the platform's own answer — rather than keeping a second list
that could drift. `GET /v1/notifications/contract` reports `producing_now` per
type, so "quiet" can be told apart from "not built", exactly as
`/v1/realtime/catalogue` already does for events.

Producing today (23 types): `TRADE_RECORDED`, `TRADE_UPDATED`,
`TRADE_RECONCILIATION_REQUIRED`, `TRADE_REVIEW_COMPLETED`,
`TRADE_REVIEW_FAILED`, `TRADE_PATTERN_DETECTED`, `DRAWDOWN_ALERT`,
`PORTFOLIO_HEALTH_CHANGED`, the eight `MODEL_*` lifecycle types,
`MODEL_HEALTH_CHANGED`, `MODEL_ALERT_CREATED`, `MODEL_ALERT_RECOVERED`,
`SYSTEM_ALERT`.

Declared and waiting for their emitter: every `ORDER_*`, `POSITION_*`, `BOT_*`,
`RISK_*` and `BROKER_*` rule. Each is a contract L17, L19, L21, L22 and L38
will satisfy; none can fire until then.

**Fourteen types are deliberately silent**, listed in `NOT_NOTIFIED` with the
reason served by the contract endpoint. `ORDER_CREATED`, `ORDER_UPDATED`,
`ORDER_SUBMITTED` and `ORDER_ACKNOWLEDGED` are intermediate states on the way to
a fill — five notifications per order is a muted notification centre.
`PORTFOLIO_UPDATED` is a figure moving, which is the normal state of a
portfolio. `RISK_APPROVED` on every signal is the flood the deduplication
exists to prevent. `NOTIFICATION_CREATED` would loop.

---

## 4. Severity, category, channel

Five severities (`app/notifications/contract.py`), and no more: `INFO`,
`SUCCESS`, `WARNING`, `ERROR`, `CRITICAL`. `SUCCESS` shares `INFO`'s rank —
"the thing you asked for worked" is a different colour on the same shelf, not a
step up.

Severity is a constant per type **except** where the event genuinely carries
one. Four are graded from the payload:

| Type | Graded from | Because |
|---|---|---|
| `RISK_ALERT` | the engine's own words | §19: the notification layer must not hold a risk threshold |
| `MODEL_ALERT_CREATED` | L29's `severity` field | §39: the layer that measured it is the only one that may state it |
| `PORTFOLIO_HEALTH_CHANGED` / `SYSTEM_ALERT` | the health word used | `stale` and `unavailable` are different operational facts |
| `DRAWDOWN_ALERT` | L30's own label | as above |

Everything else is fixed, because a severity computed from a payload is a
severity that can be computed wrongly.

**Ten categories** are what a preference is expressed against. A per-event-type
preference table would be fifty rows per user, forty naming events nothing
produces yet.

**Three channels**: `IN_APP`, `EMAIL`, `DISCORD`. All three exist in the enum,
the preference grid and the delivery table from L34, so L35 replaces one adapter
and changes no schema.

---

## 5. Deduplication and cooldowns

Two mechanisms, deliberately separate, because they answer different questions.

**"Is this the same event?"** → `dedup_key = sha256(event_id, user_id)`, under
`UNIQUE (user_id, dedup_key)`. Redis pub/sub delivers at-least-once and a
reconnecting subscriber gets a replay, so the same `TRADE_RECORDED` arrives
twice routinely. The database is where that guarantee belongs; a check the
worker has to remember is one a worker eventually forgets. The service checks
first as a cheap read and catches `IntegrityError` as the real guarantee, so two
consumers racing on one replay still produce one row.

**"Is this the same problem?"** → `condition_key = sha256(type, entity,
severity)`, plus a bounded query over the cooldown window. Fifty workers
noticing one disconnected terminal produce fifty events sharing this key; one
notification results.

**Severity is in the condition key, and that is the point.** A `WARNING` that
becomes a `CRITICAL` hashes differently and is never suppressed — suppressing it
would hide the transition an operator most needs to see. That is the reasoning
`app/monitoring/alerts.py` recorded for L29's fingerprints, restated here rather
than imported because L29 keys on a monitoring fingerprint and L34 keys on an
event.

**A discrete fact has no cooldown at all.** A trade closes once. Debouncing it
would silently drop the second of two trades on one symbol in ten minutes. Only
five types carry a cooldown (600s): the three `BROKER_*`,
`PORTFOLIO_HEALTH_CHANGED` and `SYSTEM_ALERT` — all of them *conditions* that
more than one observer reports.

---

## 6. Preferences, and the floor

Three layers: the default for a `(category, channel)` pair; the user's stored
row if there is one; then the floor.

Defaults are quiet but never silent about something critical. In-app takes
everything. Email defaults to `CRITICAL` only — except `RISK`, `BROKER` and
`SECURITY`, where an `ERROR` is already something somebody has to go and look
at. Discord defaults off everywhere: defaulting a channel on before it is
configured would queue a delivery row per event that nothing can send.

**The floor: `ERROR` and `CRITICAL` always reach the notification centre.** A
user may quieten what they are told; they may not switch off being told a risk
limit broke. `resolve()` enforces it even against a row written straight into
the database, and the `CHECK (channel <> 'IN_APP' OR enabled)` constraint means
such a row cannot be written at all. The API **refuses** rather than clamping,
because a clamped setting is one the user believes they made — they would find
out from the notification they were trying to silence still arriving.

**Section 17, stated plainly.** A preference decides whether somebody is *told*.
It decides nothing else. `notification_preferences` is read in exactly one place
in the whole application — `app/notifications/service.py` — and a test asserts
that. Disabling risk email disables the email.

---

## 7. Environment labelling

Every trading notification is stamped: `[PAPER] Trade recorded`.

Five categories are stamped (`TRADING`, `RISK`, `PORTFOLIO`, `BOTS`, `BROKER`).
A model registration is not paper or live, and stamping it would say something
untrue about where it applies — so those carry `environment = NULL`, which means
*not about a trading environment*.

That is different from `"unknown"`, which is stored when the event **was** about
trading and did not say where. It renders as `[UNKNOWN]`, never as `[PAPER]`: a
live trade shown as paper is the mistake that costs money, and a paper trade
shown as unknown is one that costs a second look.

---

## 8. Templates: nothing is invented

**A figure that is not on the event does not appear in the message.** Not as a
zero, not as "unknown P&L", not as a plausible default — the line is left out.
A notification saying *"closed with +$0.00"* because the payload carried no
`net_profit` is a false statement about a trade and is indistinguishable by eye
from a true one.

`READABLE_KEYS` is an allow-list applied on the way in. An event that grows a
field cannot start emailing it, and the stored `context` is that intersection —
which is also all a channel adapter is ever shown.

**Uncertain broker state is described as uncertain.** `ORDER_UNKNOWN` produces:

> **Order state requires reconciliation**
> The broker's state for EURUSD could not be established. The order may or may
> not have reached the venue. It is being reconciled; no conclusion has been
> drawn.

Never "order failed". The OMS has a separate `ORDER_FAILED` type for the case
where it does know, and conflating them would tell somebody their order is dead
when it may be live at the venue.

**A risk alert does not claim trading was blocked unless the engine said so.**
Only the RiskEngine can make a claim about enforcement; the template reports the
alert and adds the enforcement sentence only when the payload carries it.

**A review notification is a pointer, not the review.** The outcome and the
confidence are two recorded fields; the narrative, the lessons and the follow-up
questions stay behind the platform's access control.

---

## 9. Channels

### IN_APP

The row **is** the delivery. `service.py` persists before it queues anything,
which is §13 as an ordering. `send()` therefore reports `DELIVERED` because the
durable part already happened, and the WebSocket frame is a nudge on top. A
publish failure is recorded in the delivery's detail and does **not** fail the
delivery — retrying something that already succeeded is not a retry. A browser
disconnected for an hour catches up on its next `GET /v1/notifications`.

The frame is `NOTIFICATION_CREATED` on `user:{id}` — a type catalogued at L07,
on a scope `channels.py` has authorized to exactly that user since L07. No new
type, no new scope, no new authorization rule. It carries the headline and the
identity, not the body.

### EMAIL

`EmailProvider` is the port; `SMTPEmailProvider` is the one implementation.
Nothing above `email.py` knows what SMTP is.

Stdlib `smtplib` in `asyncio.to_thread`, not an async SMTP client: the backend's
runtime dependencies are short and each is argued for, and one blocking call per
email off the request path is not worth a new one.

Failures are **classified**, not counted: a 4xx or a socket error is retryable;
a 5xx, a refused authentication and a refused recipient are not. Retrying a bad
password every thirty seconds is how a queue stops draining — and how an account
gets locked.

No secret is logged, returned or written to a delivery row. The provider holds
the password inside itself; `describe()` reports host, port and *whether*
credentials are set.

**`NotificationResetDelivery` finally implements L04's port.** `app/auth/reset.py`
has said since L04 that delivery *"needs the notification engine (level 34)"*.
With SMTP configured, a password reset is now delivered; with none configured it
refuses exactly as `UnconfiguredDelivery` did, so an unconfigured deployment
behaves as it did before. The token is placed in the message body and nowhere
else — not in a log line, not in a delivery row, not in the audit trail.

### DISCORD

The seat, not the adapter. `discord_channel_from()` returns an
`UnavailableChannel` reporting `NOT_CONFIGURED`, and anything routed to it is
recorded `SKIPPED` rather than `FAILED` — *"nobody set this up"* is a different
operational fact from *"the provider broke"*. L35 replaces one function body.

---

## 10. The one awkward finding

`app/monitoring/monitor.py::ModelMonitor._persist` has written to
`notifications` since L29, with `user_id = NULL`. Nothing ever read those rows:
no route served them, and their `status` stayed `pending` forever.

They are **KEPT**, not deleted, and not migrated into L34's shape:

* Deleting them would destroy the only record those monitoring runs left.
* `user_id IS NULL` now has a documented meaning — *a platform record with no
  recipient* — and since every read in the notifications API is scoped to the
  signed-in user, they can never appear as somebody's notification.
* The user-addressed version of the same fact **does** flow through L34, from
  the `MODEL_ALERT_CREATED` event `app/monitoring/service.py` already publishes.

What changed is the vocabulary: `_persist` now writes L34's severity words and
sets `category`, imported from `app.notifications.contract` rather than spelled
out again, so the column has one vocabulary rather than two. Migration 0024
upper-cases the existing rows — case, not meaning.

**This is the honest state and it is a known limitation**: there are two writers
to `notifications`, and only one of them produces something a person can read.
Collapsing `_persist` into the event path is the right eventual move and belongs
with L37, which is the level that owns monitoring's own reporting.

---

## 11. Delivery, retries and priority

| Concern | Answer |
|---|---|
| Queue | `notification_deliveries` rows, drained by `NotificationDeliveryWorker` on the L02 worker base. No new queue system. |
| Priority | `ORDER BY priority DESC, created_at ASC` over `ix_notification_deliveries_queue`. §29 as an index, not a second queue. |
| Retries | Bounded: 3 attempts, backoff 30s → 120s → 600s, stored on the row so a restarted worker honours the wait it decided on. |
| Permanent errors | Not retried at all. The failure reason says why. |
| Idempotency | `UNIQUE (notification_id, channel)`. One notification cannot be emailed twice. |
| Isolation | One channel failing is one delivery row failing. `IN_APP` delivered + `EMAIL` failed is a normal, fully-recorded outcome. |
| Adapter crash | Caught by the service and treated as retryable — the safe reading. An adapter that means "permanent" says so. |
| Batch size | 50 per pass, bounded like every other sweep in this platform. |
| Fan-out cap | 500 recipients per event, logged when it bites. |

---

## 12. API

All under `/v1/notifications`, all signed-in, all scoped **in the query**.

| Route | What |
|---|---|
| `GET /v1/notifications` | Yours, newest first. Filters: category, severity, environment, unread, event type, date range. Paginated with the platform's hard ceiling. |
| `GET /v1/notifications/unread-count` | One indexed count, plus a breakdown by severity so a badge can be coloured by the worst thing waiting. |
| `PATCH /v1/notifications/{id}/read` | Idempotent. A notification belonging to somebody else answers **404, not 403** — distinguishing them is a membership oracle. |
| `POST /v1/notifications/read-all` | One scoped `UPDATE`. Returns how many were actually unread. |
| `GET /v1/notifications/preferences` | The full grid with `source` (`user`/`default`) and `locked`, not only saved rows. |
| `PATCH /v1/notifications/preferences` | Yours only. An unsafe setting is refused with the reason. |
| `GET /v1/notifications/deliveries` | Joined to your own notifications only. |
| `GET /v1/notifications/channels` | Status only — configured, never *how*. |
| `GET /v1/notifications/contract` | The routing table, the vocabulary, the retry policy, and what the layer cannot do. |

**There is no POST that creates a notification**, and a test asserts it. A
notification exists only as a consequence of a domain event the platform
published, which makes a fabricated trading alert *unrepresentable* rather than
merely discouraged (§58).

Rate limits: 60/min on the write routes, 10/min on `read-all`, through the
existing limiter.

---

## 13. Frontend

* **`NotificationBell`** in the top bar: unread badge coloured by the **worst**
  unread severity, not sized by the total. Twelve unread where one is a risk
  breach must not look like twelve trade confirmations. It renders nothing at
  all when there is no session — a bell showing "0" while the API is down says
  "all clear", which it must never say without having asked.
* **`NotificationCenter`** on `/alerts`: filters, pagination, read state,
  channel status, and the preference grid with the in-app column disabled and
  the reason attached.
* **Deep links** resolve `(entity_type, entity_id)` in
  `notificationHref()` — one table, in the browser. The backend never stores a
  URL, so renaming a page changes one line rather than invalidating every stored
  notification. An entity with no destination renders without a link, which is
  honest; a link to a page that does not exist is not.
* **Realtime is a nudge, never a render.** `NOTIFICATION_CREATED` invalidates
  the queries; the list stays whatever the API answers. That is why a reconnect
  after an outage loses nothing.
* `/alerts` is marked **partial** in `nav.ts`, not `available`: every control is
  real, but most categories have no producer yet and their filters will stay
  empty until those levels land.

---

## 14. Database

Migration `0024_notifications`. **One table altered, two created, nothing
dropped, no row deleted.**

`notifications`: nine nullable columns added (`event_id`, `category`,
`environment`, `entity_type`, `entity_id`, `read_at`, `dedup_key`,
`condition_key`) plus `updated_at` NOT NULL with a server default so existing
rows get a value without a backfill pass. The severity CHECK is widened to L34's
five words and the existing rows are upper-cased in place. `UNIQUE (user_id,
dedup_key)` is added: both columns are NULL on every pre-L34 row and SQL treats
NULLs as distinct, so those rows neither collide nor constrain anything — the
"NULLs are distinct" behaviour that migration 0020 hit as a trap is what makes
this safe here.

`notification_deliveries` and `notification_preferences` are new. Both take
their CHECK vocabularies from the same enums the models do, so a value the code
can produce and the schema rejects cannot exist.

Indexes, each justified: `(user_id, read_at)` for the unread count that runs on
every page load; `(user_id, created_at)` for the listing; `(user_id,
condition_key, created_at)` for the cooldown window; `(status, priority,
next_attempt_at)` for the worker's own query.

**Retention: none is implemented, and that is deliberate.** No existing table in
this platform has an automatic retention policy, and §49 is explicit that
records must not be deleted without one. The configurable approach documented
for a later level: delete `read` notifications older than N days per category,
never `SECURITY` or `CRITICAL`, and never `notification_deliveries` rows that
are the audit trail of a failed delivery.

---

## 15. What this layer cannot do

`app/notifications/` imports no risk engine, no order manager, no position
manager, no sizing service and no broker adapter, and a test parses every module
in the package to keep that true. It reads events and writes rows.

A bug here can fail to tell somebody something. It cannot trade.

`on_event` catches everything it does not expect and returns a report rather
than raising, and it is called by a background consumer that no execution path
waits on. Notification generation cannot block trade execution, order
processing, position updates or broker reconciliation — there is no code path
from a slow SMTP server back to an order.

---

## 16. Tests

`backend/tests/test_notifications.py`, 74 tests. The ones that matter:

| Test | Section |
|---|---|
| `test_the_notification_package_cannot_reach_anything_that_trades` | 24, 56 |
| `test_a_redelivered_event_creates_one_notification` | 20, 31 |
| `test_the_unique_constraint_is_what_actually_holds` | 31 |
| `test_a_severity_change_is_never_suppressed_by_the_cooldown` | 21 |
| `test_a_figure_the_event_did_not_carry_never_appears` | 22 |
| `test_an_unknown_order_state_is_not_reported_as_a_failure` | 44, 45 |
| `test_a_risk_alert_does_not_claim_trading_was_blocked_unless_it_was` | 41 |
| `test_a_paper_trade_is_never_shown_without_its_label` | 23 |
| `test_critical_in_app_cannot_be_switched_off` | 16, 17 |
| `test_a_channel_that_raises_does_not_affect_the_others` | 56 |
| `test_a_permanent_failure_is_not_retried_and_a_temporary_one_is` | 30 |
| `test_one_user_cannot_read_or_change_another_users_notifications` | 46, 55 |
| `test_no_route_can_create_a_notification` | 58 |
| `test_a_preference_is_not_a_switch_on_any_safety_system` | 17 |

`frontend/src/components/NotificationCenter.test.tsx`, 23 tests: badge tone by
worst severity, empty and error states, deep-link resolution, environment
rendering, preference save and refusal, no secret in the channel panel.

---

## 17. Known limitations

1. **Two writers to `notifications`** — see §10. L29's direct-run path still
   writes recipient-less rows.
2. **Most categories cannot fire yet.** Orders, positions, bots, risk and broker
   rules are contracts; their emitters arrive with L17, L19, L21, L22 and L38.
   The contract endpoint reports this per type.
3. **No retention policy** — §14. Documented, not implemented, deliberately.
4. **Email is untested against a real SMTP server** on this machine. The
   provider is exercised through its error classification, not through a live
   send.
5. **`SECURITY` category has no producer.** The audit found no security domain
   event; `app/core/audit.py` writes rows but publishes nothing. Wiring it needs
   an event type L39 should define, and inventing one here would violate §6.
6. **Redis is required for the consumer.** Without it the platform falls back to
   the in-process bus, which works in a single process and is labelled as such
   — the same honesty `app/core/events.py` already applies.
