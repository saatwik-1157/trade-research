# PRODUCTION_OPERATIONS_RUNBOOK.md

Day-to-day operation. Written at L44.

**Every command here was run against the real stack.** Where a procedure has
not been exercised it says so rather than inventing steps.

Related, and not duplicated here: `DEPLOYMENT_GUIDE.md` (how to deploy),
`ROLLBACK_RUNBOOK.md` (how to go back), `PRODUCTION_RUNBOOK.md` (L41's alert
reference), `RECOVERY_ARCHITECTURE.md` (why recovery works as it does).

---

## Startup

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Order is enforced by `depends_on: service_healthy`: postgres → redis → api and
worker → nginx. Nothing waits on the frontend.

The API runs the 16-step recovery sequence before it serves a request. Watch it:

```bash
docker compose logs api --tail 50 | grep recovery_startup
# {"event":"recovery_startup","clean":false,"needs_attention":1,"safe_mode":false}
```

`clean: false` is common and is not an error — it means a step reported
something worth a person's attention. `safe_mode: true` means the platform will
refuse to trade until the condition clears.

## Shutdown

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop api worker
```

Verified behaviour: workers stop before the hub, the hub before the bus, and
each worker logs its pass and failure counts.

```
{"event":"worker_stopped","worker":"notification-delivery","passes":17,"failures":0}
{"event":"worker_stopped","worker":"monitoring","passes":6,"failures":0}
{"event":"shutdown"}
```

`stop_grace_period` is 60s, not Docker's default 10s, because the shutdown runs
the lifespan's ordered teardown.

## Health verification

Four endpoints, four different questions.

```bash
curl -s  $URL/health/live      # process alive
curl -s  $URL/health/ready     # dependencies answer
curl -s  $URL/health/trading   # should it be trading?
curl -s  $URL/health | jq .release
```

| | Expected today |
|---|---|
| `/health/live` | 200 |
| `/health/ready` | 200 |
| `/health/trading` | **503** — correct: no broker adapter is registered |
| `release.stamped` | **true** — false means you cannot identify or roll back this build |

**Do not wire a healthcheck or an autoscaler to `/health/trading`.** 503 is its
normal state; doing so restarts a healthy platform in a loop.

## Trading safety verification

```bash
curl -s $URL/health | jq '.trading_mode, .live_trading'   # "paper", false
curl -s $URL/api/v1/monitoring/summary -b "$COOKIES" | jq '.trading_safety'
curl -s $URL/api/v1/recovery/status   -b "$COOKIES" | jq '.safe_mode'
curl -s $URL/api/v1/security/posture  -b "$COOKIES" | jq '.controls[] | select(.enforced==false)'
```

## MT5 connection verification

**Not exercised.** The adapter exists (`app/brokers/mt5.py`, demo-only) and has
never been connected — there is no terminal on the deployment host and no
credentials. When one is available:

```bash
# On the Windows host with the terminal, NOT in a container:
python -m app.symbols.sync_mt5      # symbol specs; refuses without a terminal
```

`connect()` calls the toolkit's `assert_demo`, which raises `RefuseToTrade` on
REAL, CONTEST or an unrecognised `trade_mode`. **That refusal must never be
retried** — it is the fence working.

Operational hazards for the MT5 host are in `NIGHTLY.md`: the algo-trading
toggle does not persist across restarts, and a backgrounded launcher can orphan
its child process.

## Market-data verification

```bash
curl -s $URL/api/v1/monitoring/components -b "$COOKIES" | jq '.MARKET_DATA'
```

`market_data.freshness: UNKNOWN` with *"no bar has ever been stored"* is the
current honest state — `market_bars` covers one instrument. That is **not** staleness, and a
fresh install must not be treated as a stale one.

## Bot / queue / worker verification

```bash
docker compose logs worker --tail 30 | grep worker_started
curl -s $URL/api/v1/monitoring/components -b "$COOKIES" | jq '.WORKERS'
curl -s $URL/api/v1/bots -b "$COOKIES" | jq '.[].status'
```

**Do not run more than one worker container.** There is no leader election; two
are two monitoring workers and two notification consumers.

## Database verification

```bash
docker compose exec -T postgres pg_isready -U "$POSTGRES_USER"
docker compose exec -T api alembic current          # must equal:
curl -s $URL/health | jq -r .release.schema_head    # what the code expects
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT count(*) FROM trades;"
```

If `alembic current` and `schema_head` disagree, **stop**: the running code
expects a schema the database does not have.

## Redis verification

```bash
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" ping
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" config get maxmemory-policy
# must be: noeviction
```

**Verified behaviour with Redis stopped:** `/health/live` 200, `/health/ready`
503, and **the webhook still accepts and deduplicates signals** — durable state
is PostgreSQL. Redis loss costs the event fan-out, not correctness.

## Backup verification

**BLOCKED. There is no backup job, and no restore has ever been performed.**

This is the platform's top open risk (`FINAL_RISK_REGISTER.md` H-1). Until it
is closed, the honest RPO is *"since the last manual dump"*:

```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" \
  > "backup-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

A dump that has never been restored is not a backup. See `BACKUP_RESTORE.md`.

---

# Incident response

## Emergency stop

```bash
curl -sS -X POST $URL/api/v1/recovery/safe-mode/enter \
  -b "$COOKIES" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"reason":"why, in at least 8 characters"}'
```

**No step-up required, deliberately** — a control expensive to *set* is one
nobody engages in an emergency. It blocks order submission at three server-side
paths and blocks nothing observational.

Narrower: `POST /api/v1/paper-trading/kill-switch` (global, account, strategy or
bot). **Neither closes a position.** Closing on a halt would be trading a
decision nobody made, at a price nobody chose, at the moment something is known
to be wrong.

**Neither prevents you closing one either**, and that is deliberate. A close is
approved by `RiskEngine.approve_close`, which records every limit the close
breaches and enforces only the mode fence — because a kill switch that trapped
every open position behind it would be the opposite of an emergency control.
The overridden limits are written to the order's risk snapshot, so the record
of the close names what it went over.

## Broker disconnect

1. New orders are already blocked — the adapter refuses and the pipeline stops.
2. `GET /api/v1/monitoring/components` → `broker`.
3. Check the MT5 host: terminal running, logged in, **algo-trading toggle on**
   (it does not persist).
4. **Reconnection is not automatic, by design.** After the terminal is back,
   restart the API so the startup sequence reconciles before anything trades.
5. `GET /api/v1/recovery/status` before resuming.

## Unknown order

**The mandatory procedure. Never retry.**

```bash
curl -s $URL/api/v1/orders?status=unknown -b "$COOKIES"
curl -sS -X POST $URL/api/v1/orders/{id}/reconcile -b "$COOKIES" -H "X-CSRF-Token: $CSRF"
```

Reconciliation **asks the venue**; it never re-sends. Safe mode will already
have latched with `UNKNOWN_ORDER_STATE`. Only once the order's real state is
established may anything else happen.

**This became true of the automated path on 2026-09-06 and was not true
before.** Until the L45 C-1 fix the execution pipeline wrote no `orders` row,
so an unknown order from a signal left nothing for `unresolved_orders` to find,
safe mode did not latch, and a restart re-sent the intent. If you are reading
logs from before that date, do not assume a quiet startup meant a clean one.

A new outcome you may see: **`not_recorded`**. It means an order could not be
written to the database, so it was **not sent** — nothing reached the venue,
the order was discarded, and the signal is parked for a later pass rather than
retired. Treat it as a storage incident, not a trading one.

## Duplicate order

Should be impossible: `orders.intent_id` is UNIQUE, the signal key is UNIQUE,
and both webhook race paths return `duplicate`. **Since the L45 C-1 fix the
pipeline also writes the row that makes the UNIQUE constraint apply to the
automated path, and asks the database — not just in-process memory — before
creating an order.** If one appears anyway:

1. Safe mode immediately.
2. `SELECT intent_id, count(*) FROM orders GROUP BY 1 HAVING count(*) > 1;`
3. Reconcile against the venue before touching anything.
4. This would be a **CRITICAL** finding — record it in the risk register.

## Recovery procedure

The pattern the platform implements:

```
FAILURE -> SAFE STATE -> RECOVERY -> RECONCILIATION -> VALIDATION -> RESUME
```

Resuming requires re-authentication and re-runs the whole sequence:

```bash
curl -sS -X POST $URL/api/v1/security/step-up -b "$COOKIES" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"password":"...","scope":"SAFE_MODE_EXIT","subject":"safe_mode"}'
curl -sS -X POST $URL/api/v1/recovery/safe-mode/exit -b "$COOKIES" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' -d '{"reason":"..."}'
```

It clears **only** the latches whose condition has actually gone, and reports
which are still holding. The grant is single-use and lasts 300 seconds.

## Post-incident review

1. `GET /api/v1/monitoring/events` — what the platform observed.
2. `GET /api/v1/security/events` — who did what, and why (the reason is
   mandatory on every administrative write).
3. `GET /api/v1/recovery/status` — what the sequence found.
4. Update `FINAL_RISK_REGISTER.md`. **An incident that leaves no register entry
   is one the next person rediscovers.**

---

## Verified failure behaviour

Exercised against the running stack at L44:

| Scenario | Observed |
|---|---|
| Duplicate webhook | `accepted` then `duplicate`, **same signal id**, one signal |
| Invalid secret | 401 |
| Malformed JSON | 401 (secret is checked first) |
| Expired alert | 422, refused on its own timestamp |
| **Redis stopped** | live 200, ready 503, trading 503; **webhook still worked** |
| **Postgres stopped** | live 200, ready 503; webhook **500** — correct: nothing may be accepted that cannot be recorded |
| **Both restored** | ready back to 200, webhook working, dedup intact — **no restart needed** |
| Graceful shutdown | workers first, 0 failures |
| Restart | recovery sequence ran and reported honestly |

Covered by the test suite rather than by hand: unknown order, worker crash, bot
crash, market-data stale, AI failure, notification failure, OMS restart.
