# PRODUCTION_RUNBOOK.md

Operating the platform once it is running. What to look at, what each alert
means, and what to do.

Deployment is `DEPLOYMENT_GUIDE.md`; going back is `ROLLBACK_RUNBOOK.md`.

---

## The four health states

They are different questions and the distinction is the point.

| Endpoint | Question | 503 means |
|---|---|---|
| `/health/live` | is the process running? | restart it |
| `/health/ready` | do its dependencies answer? | pull it from the pool; check DB and Redis |
| `/health/trading` | should the platform be trading? | **normal.** Do not restart anything. |
| `/health` | which build, which mode? | n/a |

**`/health/trading` returning 503 is the expected steady state today.** No
broker adapter has ever been registered, so `broker` reports NOT_CONFIGURED and
the verdict is BLOCKED. Wiring a container healthcheck or an autoscaler to this
endpoint would restart a perfectly healthy platform in a loop; the production
overlay deliberately probes `/health/ready` instead.

It reveals a verdict and a count, never the reasons — it is unauthenticated,
and the reasons name components and their states. `GET /v1/monitoring/summary`
gives the reasons and needs a session.

## Daily

```bash
curl -s "$URL/health" | jq '.release, .trading_mode, .live_trading'
curl -s "$URL/health/ready" | jq '.status, .critical_unavailable'
curl -s "$URL/health/trading" | jq '.trading, .safe_mode, .blocker_count'
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Then, signed in: `GET /v1/monitoring/summary`, `GET /v1/monitoring/events`,
`GET /v1/recovery/status`, `GET /v1/security/posture`.

Four things worth a second look, all of them normal for this deployment and all
of them worth confirming are *still* the reason:

* `broker: NOT_CONFIGURED` — no adapter registered.
* `market_data.freshness: UNKNOWN` — `market_bars` covers one instrument; no bar has ever
  been stored. That is not staleness.
* `ai.models: NOT_CONFIGURED` — nothing deployed.
* `security` — HEALTHY in production, listing MFA and the managed secret store
  as known gaps rather than failures.

---

## Alerts

L34's `NotificationService` routes these; nothing new was built for L41.

### Critical — act now

| Alert | What it means | Do |
|---|---|---|
| **Unknown order** | the platform does not know whether an order exists at the venue | **do not retry.** `POST /v1/orders/{id}/reconcile` asks the venue. Safe mode has almost certainly latched already. |
| **Safe mode engaged** | the startup sequence or a gate found something | `GET /v1/recovery/status` for the latches. Fix the condition; the exit re-runs the sequence and clears only what has actually cleared. |
| **Position mismatch** | local and venue positions disagree | `POST /v1/recovery/reconcile` (reports, never repairs). Do not adjust by hand until you know which is right. |
| **Broker disconnect** | the adapter lost the terminal | new orders are already blocked. Check the MT5 host and the algo-trading toggle, which does not persist across restarts. |
| **Database failure** | `/health/ready` 503, `critical_unavailable: [database]` | the API is failing closed. Nothing is being written, which is the safe failure. |
| **Security incident** | a burst of failed logins, a step-up refused, a dangerous action taken | `GET /v1/security/events`. Session compromise → revoke that user's sessions. |
| **Reconciliation failure** | the sequence itself failed | safe mode latches with `recovery_failed`: **nothing was verified.** Treat as unknown state. |

### Warning — investigate

Stale market data · worker restart · queue backlog · high latency · AI
unavailable · notification failure.

**A notification failure never blocks a trade.** L40 asserts it: a raising
event bus does not stop an order. If Discord or SMTP is down, trading is
unaffected — deal with it in the morning.

---

## Common operations

### Halt trading immediately

```bash
POST /v1/recovery/safe-mode/enter   {"reason": "..."}
```

No step-up required, deliberately: a control that is expensive to *set* is one
nobody engages in an emergency. It blocks order submission at three
server-side paths and blocks nothing observational — reconciling is how you get
out.

Narrower: `POST /v1/paper-trading/kill-switch` (global, account, strategy or
bot). **Neither closes a position.** Closing on a halt would be trading a
decision nobody made, at a price nobody chose, at the moment something is known
to be wrong.

### Resume trading

```bash
POST /v1/security/step-up  {"password": "...", "scope": "SAFE_MODE_EXIT", "subject": "safe_mode"}
POST /v1/recovery/safe-mode/exit  {"reason": "..."}
```

The exit **re-runs the whole startup sequence** and clears only latches whose
condition has actually gone, then tells you which are still holding. A release
that skipped the re-check would let somebody resume into an unreconciled
account. The step-up grant is single-use and lasts 300 seconds.

### Suspected account compromise

```bash
POST /v1/security/step-up  {"password": "...", "scope": "ADMIN_SESSION_REVOKE", "subject": "<user id>"}
POST /v1/admin/users/{id}/revoke-sessions  {"reason": "...", "confirm": "<their email>"}
```

Signs them out everywhere. Their account stays active and every record they own
is untouched. If the blast radius is unknown, engage safe mode *first* — it
needs no step-up.

### Rotate a secret

Change it in the environment file and restart the service. **The process read
it at startup**; editing a running container's environment does nothing.

An unset `TV_WEBHOOK_SECRET` refuses every alert, which is the safe
intermediate state during a rotation.

### Restart a worker

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart worker
```

Safe: `RECOVERY_STARTUP_CHECKS=false` on the worker, so it does not re-run the
reconciliation the API owns. Do **not** `--scale worker=2`; see below.

---

## Things not to do

* **Do not scale the worker container past 1.** No leader election exists. Two
  workers are two monitoring workers and two notification consumers. The
  duplicate work is caught by unique constraints rather than prevented, and
  that is a property of today's workers, not a guarantee.
* **Do not wire an autoscaler or a healthcheck to `/health/trading`.** 503 is
  its normal state.
* **Do not use `--workers N` on uvicorn.** The process holds the safe-mode
  latch, the step-up grants and possibly the rate limiter; forking copies them
  invisibly. Add containers instead.
* **Do not restart to clear safe mode.** It is re-derived at every startup, so
  a restart re-latches the same condition — correctly.
* **Do not `docker compose down -v`.** The `-v` removes the volume holding the
  trade history.
* **Do not set `LIVE_TRADING=true` to "test the deployment".** It would not
  work — all 11 gates are false — and the attempt is recorded in the audit
  trail.

---

## Escalating to unknown state

If you cannot establish what happened — a deploy failed mid-migration, a
process died holding an order, the venue and the platform disagree — the
correct action is to **stop and leave safe mode engaged.**

Safe mode does not degrade. A platform sitting in it costs missed
opportunities; a platform guessing its way out of an unreconciled account costs
money. `RECOVERY_ARCHITECTURE.md` has the full model.
