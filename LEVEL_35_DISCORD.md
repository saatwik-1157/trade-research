# Discord integration (L35)

Built 2026-09-05. A webhook, an embed, and nothing else. Discord is a
destination for notifications the platform already created; it decides nothing
and it cannot trade.

---

## 1. The audit: L34 left a seat, not a gap

Section 2 asks what exists before anything is built. The search for `discord`,
`webhook`, `discord.py`, `discord.js` and `notification channel` found:

| Found | Verdict |
|---|---|
| `Channel.DISCORD` in `app/notifications/contract.py` | **KEEP** — the enum already had it |
| A `DISCORD` row per category in the preference grid | **KEEP** — defaults to off, from L34 |
| `notification_deliveries.channel` accepting `'DISCORD'` | **KEEP** — the CHECK already allowed it |
| `discord_channel_from()` returning `UnavailableChannel` | **REPLACE** — one function body |
| L34's service, worker, retry policy, dedup, preferences | **KEEP**, reused unchanged |
| `app/webhooks/` (TradingView ingestion, L09) | **KEEP**, unrelated — that is an inbound receiver |
| `frontend/src/app/community/page.tsx` (a PlannedPage) | **REPLACE** |
| Any Discord code, token, client or library | **does not exist** |

**Nothing in L34 changed shape.** No migration, no new delivery status, no new
preference column, no new event type, no second event bus. That was the point of
building the seat: L35 is one adapter and two routes.

---

## 2. Webhook, not a bot — and why

Section 4 says to use a bot only when the project genuinely needs commands,
interactive responses, richer channel control or guild management. It needs none
of those. The requirement is outbound notification.

A webhook does that with:

* no gateway connection and no long-lived process to supervise,
* no token with guild-wide reach — a webhook can post to exactly one channel,
* no new runtime dependency.

Section 35 says not to implement commands unless required, so **there are
none**. `/status`, `/portfolio` and `/trades` would each be a read of somebody's
private trading data authorized by a Discord identity this platform has never
seen — that is a separate security design, and a webhook cannot be escalated
into it by accident.

**No HTTP client library was added.** `urllib.request` in `asyncio.to_thread`,
the same argument `email.py` makes for `smtplib`: the backend's runtime
dependencies are short and each is argued for in `requirements.txt`, and one
POST per notification, off the request path and bounded by a timeout, does not
justify one.

---

## 3. Where it sits

```
   RiskEngine / OMS / BotManager / Journal / Review / Monitoring
                              |
                    publishes a DOMAIN EVENT
                              v
                  app/core/events.py   (L07's bus)
                              v
              NotificationConsumer  ->  NotificationService
                              |
                    rule -> severity -> environment -> template
                              |
                    dedup -> cooldown -> PERSIST
                              |
                       PREFERENCE ENGINE
                              |
              +---------------+----------------+
              v               v                v
           IN_APP          EMAIL          DISCORD  <-- L35 is only this box
                                             |
                                    DiscordWebhookChannel
                                             |
                                       one webhook POST
```

There is no `TradingService -> Discord`, no `RiskEngine -> Discord` and no
`BotManager -> Discord`. A test enumerates every file in `app/` that mentions
the Discord module and asserts the list is exactly
`notifications/channels/__init__.py` — the channel registry.

---

## 4. Configuration and secrets

Three settings, and only the three the implementation needs (§5):

```
DISCORD_ENABLED=false
DISCORD_WEBHOOK_URL=<configured-secret>
DISCORD_USERNAME=trade-research
DISCORD_TIMEOUT_SECONDS=10
```

`.env` is already gitignored; `.env.example` carries placeholders only. Compose
passes `DISCORD_ENABLED` and `DISCORD_WEBHOOK_URL` through from the host
environment rather than writing them into the file, because a secret in a
committed compose file is a committed secret.

**The webhook never leaves the adapter.** It is read from settings in the
constructor and held on the instance. Nothing returns it:

* `describe()` has no branch that includes it, **not even redacted** — a
  redacted secret is still a statement about the secret's shape.
* `GET /v1/notifications/discord` and `GET /v1/notifications/channels` return
  `describe()`.
* `_redact()` scrubs both the whole URL *and* the token segment out of any
  provider message before that message is logged or written to a delivery row.
  Discord's error bodies can echo the request, and a delivery's
  `failure_reason` is readable by the user it belongs to.
* Four tests assert it: on the adapter, on the registry, on the API responses,
  and on a stored delivery row after a failure whose error text contained the
  URL.

**Rotation** (§36): a webhook cannot be rotated in place. Delete it in Discord
(Server Settings → Integrations → Webhooks) and configure a new one; the old URL
stops working the moment it is deleted. The status endpoint says so.

---

## 5. System-wide, and what that costs

Section 34 says to determine whether Discord is user-specific or global and not
to assume. **It is system-wide**, and the consequence is stated rather than
buried:

> One webhook serves the deployment. Every user who enables the Discord channel
> shares the destination.

That is why the Discord channel **defaults to off for every category and every
user**, which it has since L34. A user turning it on is consenting to their own
notifications appearing in a shared channel. `scope: "system"` is on the status
response and rendered on the page.

The alternative — a per-user webhook stored against the user row — was refused
for this level: it means storing a bearer credential per user, which needs
encryption at rest, a rotation flow and a key-management decision that nobody
has made. Storing it in plaintext because the feature was convenient would be
the worse outcome. If the platform ever becomes genuinely multi-tenant, that is
the design to do properly.

---

## 6. The message

An embed, built only from what the notification carried.

```
🚨 CRITICAL — [LIVE] Risk limit breached
The risk engine reports that new trading is blocked.

Environment   LIVE
Symbol        EURUSD
Reason        daily loss limit reached
Account       a1b2c3d4

RISK · CRITICAL · RISK_ALERT
```

* **The environment is first, and it is a field as well as a title.** §9. A
  title is truncated in a phone notification; the environment is the one thing
  that must not be. `PAPER`, `DEMO`, `LIVE`, `BACKTEST` and `UNKNOWN` all render
  distinctly, and a test asserts the five are distinguishable.
* **A field whose key is absent produces no field.** §14 and §8. A P&L the event
  did not carry does not appear as `0.00`, and there is no branch of
  `build_embed` that can invent one.
* **Colour and marker by severity.** §12 and §30. Decoration with a job: an
  operator scanning a channel reads the stripe before the text.
* **`ORDER_UNKNOWN` says reconciliation, never failure.** §13. The adapter does
  not reinterpret; it renders the sentence `templates.py` already wrote, which
  says the venue state could not be established and no conclusion has been
  drawn.
* **Identifiers travel** (§28) so a message in a channel traces back to the
  notification, the event and the entity. An id is not a capability anywhere in
  this platform — every read is authorized against the database.
* **A link only when `APP_BASE_URL` is set** (§31), pointing at the notification
  centre, carrying no token.
* Title, description and field values are truncated to Discord's limits rather
  than being rejected as a 400.

---

## 7. Failure handling

| Response | Retryable | Why |
|---|---|---|
| 204 | — | delivered |
| 429 | **yes**, after Discord's own `retry_after` | §24: what the provider asked for, not a guess. Read from the header, then the JSON body, and capped at 300s so a broken value cannot park a delivery for a day. |
| 500–5xx | yes | their server |
| timeout, DNS, connection refused | yes | weather |
| 400 | **no** | our message. An identical retry produces an identical 400. |
| 401 / 403 | **no** | the webhook was revoked. It needs rotating, not retrying. |
| 404 | **no** | the webhook was deleted. Retrying it never drains the queue. |
| not a webhook URL | **no** | configuration, reported as FAILED with the expected shape |
| `DISCORD_ENABLED=false` | — | **SKIPPED**, not FAILED. "Switched off" is not "broken". |

Retries themselves are **L34's**, unchanged: three attempts, 30s → 120s → 600s,
stored on the delivery row. L35 added no retry system.

**Rate limits** (§24) are handled twice: a client-side minimum interval of 0.5s
between sends keeps a burst under Discord's ~5-per-2-seconds without reaching
the 429 path at all, and the 429 path exists anyway because two API processes
each hold their own gate.

---

## 8. Failure isolation

Section 25 and section 49, the mandatory ones.

Discord being unavailable affects Discord delivery and nothing else. The
adapter is called by the delivery worker, which runs on its own loop, off every
request path, and reports into a `notification_deliveries` row. Nothing reads
that row to decide anything except whether to try again.

Tested by making every HTTP call raise and asserting: the paper account balance
is unchanged, the notification is still there and still unread, the `IN_APP`
delivery is `DELIVERED` while `DISCORD` is `RETRYING`, and the failure is
recorded with a reason and a next attempt rather than lost.

---

## 9. Deduplication

Reused, not reinvented (§22 and §51). The same `event_id` delivered twice
produces one notification — `UNIQUE (user_id, dedup_key)` — and therefore one
delivery row per channel — `UNIQUE (notification_id, channel)` — and therefore
one Discord message. A test sends the same event twice and asserts exactly one
POST.

---

## 10. API

Two routes, added to L34's router because that is where channels already live
(§40).

| Route | Gate | What |
|---|---|---|
| `GET /v1/notifications/discord` | signed in | State, transport, scope, counters, how to enable, how to rotate. **Never the URL.** |
| `POST /v1/notifications/discord/test` | `manage_system_settings` | Sends a message that says it is a test. |

`GET /v1/notifications/channels` (L34) already reports Discord alongside the
other two, so a client that only wants "is it on" does not need the specific
route.

The test send is rate limited (10/min), CSRF-protected like every state-changing
route, and **audited**: an `admin_action` row against `notification_channel`
recording who, when, the request id and the result. `audit._scrub` means no URL
could reach that row even if the detail carried one.

---

## 11. The test message

Sections 38, 39 and 54:

```
🔧 Discord configuration test
This is a test of the notification webhook. No trading action was performed
and nothing was recorded: no trade, no order, no alert.
```

It creates no notification row, publishes no domain event, and names no trade,
symbol, price or account. A synthetic `TRADE_CLOSED` sent to prove the wiring
works is a trade in the channel that never happened, and somebody would act on
it.

---

## 12. Frontend

`/community` — status, scope, counters, and the test button for administrators.

* **There is no field to type a webhook into.** A test asserts the page renders
  zero `<input>` and `<textarea>` elements. The webhook is a bearer credential
  and it is server configuration; the browser never receives it and has no way
  to set it.
* The test control is gated twice: the backend answers 403 without
  `manage_system_settings`, and the button is hidden for anyone else as a
  convenience. Hiding a control is not authorization — the first is the one that
  matters, and a backend test proves a trader gets 403.
* `NOT_CONFIGURED`, `DISABLED` and `CONFIGURED` render distinctly. "Switched
  off" and "never set up" are different operational facts.
* The page states that the destination is shared and every category is off by
  default, and points at Alerts → *What you are told about* for the preferences.
* `/community` is marked **partial** in `nav.ts`: Discord is real, the
  shared-research half of that route is not built.

---

## 13. Tests

`backend/tests/test_discord.py`, 54 tests:

| Test | Section |
|---|---|
| `test_the_discord_adapter_cannot_reach_anything_that_trades` | 1, 7, 63 |
| `test_no_trading_service_reaches_discord_directly` | 59 |
| `test_the_webhook_never_appears_in_anything_a_caller_can_read` | 36, 50 |
| `test_a_provider_error_that_echoes_the_url_is_redacted` | 36, 43 |
| `test_no_secret_reaches_a_delivery_row` | 43 |
| `test_a_404_is_not_retried_and_a_429_is` (parametrised over 400/401/403/404/500/503) | 23, 47 |
| `test_discord_honours_the_retry_after_it_was_given` | 24 |
| `test_an_absurd_retry_after_is_capped` | 24 |
| `test_the_rate_gate_spaces_consecutive_sends` | 24 |
| `test_every_environment_is_distinguishable` | 9, 52 |
| `test_a_missing_field_produces_no_field_rather_than_a_placeholder` | 8, 14 |
| `test_an_unknown_order_state_is_not_reported_as_a_failure` | 13 |
| `test_one_event_delivered_twice_is_one_discord_message` | 22, 51 |
| `test_discord_being_down_changes_nothing_about_the_trade` | 25, 49 |
| `test_discord_is_not_used_when_the_preference_says_no` | 11 |
| `test_only_an_administrator_may_send_a_test` | 50 |
| `test_a_test_send_is_audited_without_the_webhook` | 43 |
| `test_l35_added_no_delivery_status_and_no_channel` | 45 |

`frontend/src/components/DiscordSettings.test.tsx`, 10 tests: state rendering,
no input field, admin gating, test send and its failure, the shared-destination
statement.

---

## 14. Known limitations

1. **System-wide only.** Per-user webhooks need encryption at rest and a key
   decision nobody has made — see §5.
2. **No commands.** Deliberate (§35). Adding them is a bot and a separate
   security design.
3. **Never sent to a real Discord server** from this machine. Every response
   code is exercised against a mocked transport; no live send has been made.
4. **Most categories still cannot fire.** Unchanged from L34: nothing emits an
   order, position, bot, risk or broker event yet, so a user enabling Discord
   for `RISK` today will correctly receive nothing.
5. **No message editing or threading.** A notification is posted once; a
   condition that resolves posts a second message rather than editing the first.
   Editing needs `?wait=true` and a stored message id, which is worth doing only
   if somebody asks for it.
6. **The rate gate is per process.** Two API processes each hold their own, so
   the 429 path is the real backstop rather than a fallback.
