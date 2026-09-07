# Backup, restore and disaster recovery

Written at L38, 2026-09-05.

---

## 1. The honest position

**There is no automated backup on this deployment, and L38 did not write one.**

That is a refusal, not an omission. A backup script this project cannot test is
a backup nobody should trust: `pg_dump` in a container this repository does not
deploy, on a schedule nothing runs, to storage nobody has chosen, with an
encryption key nobody holds, is a file that will be discovered to be empty on
the day it matters.

Step 22 of the brief says **do not claim an RPO or RTO the infrastructure
cannot actually achieve.** So:

| | Value | Why |
|---|---|---|
| **RPO** | *since the last manual dump* | Nothing takes one automatically. On a machine where nobody has run one, the RPO is the age of the database. |
| **RTO** | *however long a person takes* | There is no restore automation, no standby and no runbook rehearsal. |

Both improve the moment somebody runs the two commands in §4 on a schedule and
verifies the result. Until then, writing "RPO: 15 minutes" here would be the
single most dangerous sentence in this repository.

---

## 2. What must be backed up

In order of how badly it hurts to lose.

| Data | Where | Reconstructible? |
|---|---|---|
| `trades`, `executions`, `orders`, `order_events`, `positions`, `position_events` | PostgreSQL | **No.** This is the trading record. |
| `audit_logs` | PostgreSQL | **No.** Append-only security history. |
| `system_events` | PostgreSQL | **No.** Incident and recovery history. |
| `journal_entries`, `trade_tags`, `trade_reviews` | PostgreSQL | **No.** Human and generated analysis. |
| `strategies`, `strategy_versions`, `strategy_parameters` | PostgreSQL | **No.** Historical trades reference the version they ran under. |
| `models`, `model_versions`, `model_deployments`, `model_lifecycle_events` | PostgreSQL | Partly — the metadata, not the artifacts. |
| `users`, `roles`, `auth_sessions` | PostgreSQL | No; sessions are disposable, users are not. |
| `notifications`, `notification_deliveries`, `notification_preferences` | PostgreSQL | Preferences no; the rest is disposable. |
| `market_bars` | PostgreSQL | **Yes**, from the provider — but re-ingesting changes replay determinism, which is exactly why the table exists. Back it up. |
| Model artifacts | filesystem | No. |
| Redis | Redis | **Yes.** Nothing authoritative lives there; see §6. |
| Secrets | environment / secret manager | **Never in a database dump.** See §3. |

---

## 3. Secrets are not backed up here

Step 22: do not back up secrets insecurely.

`SMTP_PASSWORD`, `DISCORD_WEBHOOK_URL`, `TV_WEBHOOK_SECRET`, `DATABASE_URL` and
`BOOTSTRAP_ADMIN_PASSWORD` live in the environment. **None of them is in the
database**, so a `pg_dump` contains none of them — and that is a property worth
keeping rather than a gap to fill. Broker credentials are not stored at all:
MT5 is reached over IPC to a terminal somebody logged in by hand, and
`BrokerRegistry` says so.

Back up secrets the way your platform backs up secrets. Not here.

---

## 4. Taking a backup

```bash
# Full logical dump, custom format so pg_restore can be selective.
docker compose exec -T postgres \
  pg_dump -U trade -d trade -Fc --no-owner \
  > "backups/trade-$(date -u +%Y%m%dT%H%M%SZ).dump"

# Verify it is readable. A dump nobody has opened is a file, not a backup.
pg_restore --list "backups/trade-<stamp>.dump" | head -40
```

Encrypt at rest with whatever your platform uses; the dump contains the whole
trading record.

**Retention has not been chosen.** Neither has one for `notifications` (L34),
`system_events` (L37) or `audit_logs` (L36) — all three levels documented the
same gap. They should be decided together, because they have opposite answers:
an informational notification can age out in weeks and a security audit row
should not.

---

## 5. Restoring

Step 23. **Never restore over an active trading state.**

```
 1  STOP.                     Enter safe mode first:
                              POST /v1/recovery/safe-mode/enter
                              A restore over a running platform is a restore
                              racing the thing that is writing.
 2  Preserve evidence.        Copy the current database and the logs BEFORE
                              touching anything. The corrupted state is the
                              only record of what went wrong.
 3  Choose the backup.        Newest dump that `pg_restore --list` reads.
 4  Restore to an ISOLATED    Never straight over production.
    database.                   createdb trade_restore
                                pg_restore -d trade_restore --no-owner <dump>
 5  Validate integrity.       Row counts on trades, orders, positions,
                              audit_logs. Compare against what you expected
                              to lose.
 6  Validate the schema.      alembic current, then alembic upgrade head
                              against the restored database. A dump older than
                              a migration needs the migration, and applying it
                              in isolation is how you find out it fails.
 7  Point the application     One process. Do not start workers yet.
    at it.
 8  RECONCILE.                GET  /v1/recovery/reconciliation
                              This is the step that makes the restore safe: the
                              restored database believes things about the venue
                              that are as old as the dump.
 9  Settle every unknown      POST /v1/orders/{id}/reconcile, one at a time.
    order.                    It asks the venue. It never re-sends.
10  Reconcile positions.      A position the venue holds that the restored
                              database does not is NOT closed automatically,
                              and one the database holds that the venue does
                              not is NOT re-opened. Both need a person.
11  Check the RiskEngine.     GET /v1/risk — limits and kill switches restored
                              as they were at dump time. A limit that has since
                              changed has changed back.
12  Run the checks.           GET /v1/monitoring/summary. Trading safety must
                              read SAFE before anything resumes.
13  Resume in PAPER first.    TRADING_MODE=paper. Watch one full cycle.
14  Release safe mode.        POST /v1/recovery/safe-mode/exit. It re-runs the
                              startup sequence and refuses while any condition
                              still holds -- which is the point.
15  Live only by explicit     LIVE_TRADING is false and every LIVE_GATES entry
    authorization.            is false. Neither is changed by a restore, and
                              nothing in the platform can change them.
```

**Step 8 is the one that cannot be skipped.** A restored database is a
consistent snapshot of what this platform believed at dump time. The venue has
not been restored, and it did not stop trading while the dump sat on a disk.

---

## 6. Redis

Nothing authoritative lives in Redis. It carries:

* the event bus (pub/sub — messages are transient by design, and L07's hub
  documents that a disconnected subscriber misses them);
* the shared rate limiter, when `RATE_LIMIT_SHARED=true`.

After a Redis restart: the client reconnects, the hub's next publish reports
whether the bus is healthy, and the rate limiter's counters start from zero —
which is a slightly more permissive limiter for one window, not a correctness
problem.

**Nothing is rebuilt because nothing needs rebuilding.** Step 16 asks to
determine which data is ephemeral and which is authoritative rather than
assume; this is the determination.

---

## 7. Failure scenarios

Step 24. Detection → safe state → recovery → reconciliation → resume condition.

| Scenario | Detected by | Safe state | Resume condition |
|---|---|---|---|
| **API process dies** | container healthcheck; `/health/live` | nothing is executing; workers died with it | the startup sequence runs and does not latch |
| **Worker crashes** | `WorkerStatus.is_stale`, L37 `worker:*` | the loop stopped; nothing partial was sent | the worker restarts; `Worker.run` survives a failing tick already |
| **Database down** | `check_database`, readiness 503 | every write refuses; `/health/ready` returns 503 | the check passes and the schema matches |
| **Database corrupted** | the `migrations` step; row counts | safe mode: `DATABASE_INCONSISTENT` | restore per §5, then reconcile |
| **Redis down** | `check_redis`; the hub's `bus_healthy` | events are not delivered; the OMS reconciles against the broker, never the bus | the client reconnects; the hub reports healthy |
| **Broker/MT5 disconnected** | `BrokerRegistry.health()` | safe mode: `BROKER_UNREACHABLE`; new orders blocked | reconnect, reconcile, then release the latch |
| **Order state unknown** | the `oms_reconciliation` step | safe mode: `UNKNOWN_ORDER_STATE` | `POST /v1/orders/{id}/reconcile` per order |
| **Position mismatch** | the `position_reconciliation` step | safe mode: `POSITION_MISMATCH` | a person decides; nothing is closed automatically |
| **Market data stale** | L37 `market_data.freshness` | strategies refuse for want of bars | a fresh bar within the threshold |
| **TradingView silent** | `webhook_events` stop arriving | no signals, which is not a failure | alerts resume |
| **Discord/email down** | L34 delivery rows; L37 channel state | notification delivery retries; **trading is unaffected** | the provider answers |
| **AI provider down** | the strategy's own configuration | the configured fallback — `AI_DISABLED` runs deterministic | the provider answers |
| **Host restart** | absence | everything is stopped | the startup sequence, on boot |
| **Deployment gone wrong** | health checks fail readiness | the old container keeps serving until the new one is ready | roll back the image; the schema is forward-compatible or the migration is reverted deliberately |

---

## 8. Deployment

Step 25.

* **Graceful shutdown** already exists in `create_app`'s lifespan, in the right
  order: workers stop first, then the notification consumer, then the
  background services, then adapters disconnect, then the hub, then the bus,
  then the engine is disposed. Stopping a worker before the hub it publishes
  onto is the ordering that avoids a publish into a closed bus.
* **Restart policies** were added to `docker-compose.yml`: `unless-stopped` on
  postgres, redis, the api and the frontend. A container that dies comes back;
  a container somebody stopped stays stopped.
* **Readiness gates the traffic.** `/health/ready` returns 503 while a critical
  dependency is unavailable, which is what an orchestrator should read before
  sending a request.
* **Migrations are not run on boot.** The startup sequence *checks* the schema
  and refuses; it does not apply anything. N processes racing to migrate is a
  worse failure than a process that will not start.
* **Two processes must not both reconcile.** The startup sequence is read-only,
  so two running it concurrently is harmless — but the safe-mode latch is
  per-process, so two API processes can disagree about whether new orders are
  blocked. On this deployment there is one. See §12.5 of
  `RECOVERY_ARCHITECTURE.md`.
