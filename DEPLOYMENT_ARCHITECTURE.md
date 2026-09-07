# DEPLOYMENT_ARCHITECTURE.md

How the platform is deployed, and the decisions behind it. Written at L41.

`DEPLOYMENT.md` remains the record of *how the system runs today* and of the
research toolkit's entry points; this document is the deployment architecture
proper. They are not duplicates and neither replaces the other.

---

## The audit, and what it changed

The audit found a working development stack and **no production story**. What
existed was good and was kept; what was missing was mostly the difference
between "runs on a laptop" and "runs where somebody can reach it".

| Component | Verdict | Note |
|---|---|---|
| `backend/Dockerfile` | **KEEP + MODIFY** | already slim, non-root (uid 10001), correct root build context for `tools/`. Added release build args only. |
| `frontend/Dockerfile` | **KEEP** | multi-stage, standalone output, non-root. Nothing to change. |
| `docker-compose.yml` | **KEEP + MODIFY** | healthchecks and `depends_on: service_healthy` were already right. Published ports moved out; build args added. |
| `nginx/nginx.conf` | **KEEP + MODIFY** | **contained a real defect**; see below. |
| `.github/workflows/tests.yml` | **KEEP + MODIFY** | lint/types/tests already gated. Added an image build-and-verify job. |
| `.env.example` | **KEEP + MODIFY** | already placeholders only. Added the L39 and L41 settings. |
| `.dockerignore` | **KEEP** | already excludes ~580 MB of `node_modules` and measured data. |
| Health endpoints | **KEEP + ADD** | `/health`, `/health/live`, `/health/ready` existed. Added `/health/trading`. |
| L37 monitoring, L38 recovery, L34 notifications | **KEEP** | already the monitoring, disaster-recovery and alerting layers this level was told to use. Nothing was rebuilt. |
| Kubernetes / Helm / Terraform | **NOT ADDED** | see "Why not Kubernetes" below. |
| `docker-compose.override.yml` | **ADD** | development publishes |
| `docker-compose.prod.yml` | **ADD** | production overlay |
| `nginx/nginx.prod.conf` | **ADD** | TLS |
| `app/core/release.py` | **ADD** | release identity |

Nothing was removed. Nothing was replaced.

---

## The two defects the audit found

### 1. The WebSocket was dead behind the proxy

`nginx.conf` set `proxy_http_version 1.1` and the `Upgrade`/`Connection`
headers on `location /` (the frontend) and **not** on `location /api/` — which
is where `/v1/realtime/ws` is served. Without them nginx does not forward the
upgrade.

Reproduced, not inferred. The same handshake against the same running API:

```
pre-L41 nginx config : HTTP 404 Not Found
post-L41 nginx config: HTTP 101 Switching Protocols
```

Every live update in the platform — orders, positions, notifications, health,
security alerts — was unreachable through the proxy. It went unnoticed because
`docker-compose.override.yml` publishes the API directly on `127.0.0.1:8000`,
and that is the address a developer's browser connects to. **The development
convenience was hiding the production defect.**

### 2. The production overlay could not close a port

`docker-compose.prod.yml` was first written with `ports: []` on postgres, redis
and api, and a comment saying that closed them. It does not: **Compose merges
port lists by appending**, so the base file's publishes survived. Verified by
reading `docker compose config` rather than trusting the overlay — the merged
production configuration was still exposing PostgreSQL on 5440, Redis on 6390
and the API on 8000.

The fix is the idiomatic layout, and it changes development behaviour not at
all:

```
docker-compose.yml            the stack. Publishes NOTHING.
docker-compose.override.yml   development publishes. Loaded AUTOMATICALLY.
docker-compose.prod.yml       production. Loaded only when named.
```

`docker compose up -d` still gives a developer 5440, 6390, 8000 and 8080 on
loopback. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up
-d` does not load the override at all, so being private in production is a
property of **how the files load**, not of anybody remembering to override
something.

CI asserts it: the `images` job parses the merged production config and fails
if any service other than nginx publishes a host port.

---

## Topology

```
                        internet
                            |
                    :443  (:80 redirects only)
                            |
                     +--------------+
                     |    nginx     |   TLS terminates here
                     +--------------+   the ONLY published container
                       /           \
              /api/  /               \  /
                    v                 v
            +--------------+   +--------------+
            |     api      |   |   frontend   |
            | WORKERS=false|   |  Next.js     |
            +--------------+   +--------------+
                    |
        +-----------+-----------+
        |           |           |
        v           v           v
  +----------+ +---------+ +----------+
  | postgres | |  redis  | |  worker  |   WORKERS=true, exactly 1
  +----------+ +---------+ +----------+
        ^                        |
        +------------------------+

  [Windows host, separate]
     MT5 terminal + adapter worker  ---- speaks over the database and Redis
```

**Public**: nginx on 443, and 80 solely to redirect and answer the ACME
challenge. **Private**: everything else. PostgreSQL, Redis, the API and the
worker publish no host port in production.

---

## The decisions

### Why the API and the workers are separate containers

They are the same image and the same code. The difference is one environment
variable, `WORKERS_ENABLED`.

The reason is Step 9's rule about not accidentally running duplicate workers.
Before L41 the API process ran the notification consumer, the notification
delivery worker and the monitoring worker in-process — so **a second API
replica was a second monitoring worker and a second notification consumer**.

The duplicate work is *caught* rather than prevented: `notifications` has a
UNIQUE dedup key and monitoring writes `system_events`. But "caught" is a
property of today's workers, not a rule a future worker inherits, and a
platform whose safety depends on nobody scaling it is a platform that will be
scaled. So: **API replicas scale; the worker container does not.**

`RECOVERY_STARTUP_CHECKS` follows the same logic in the other direction. It is
**true on the API and false on the worker**, because the startup sequence
decides whether the platform may consume a signal at all and must run in the
process that serves `POST /v1/orders`. Two processes reconciling the same
account at boot would produce two safe-mode latches that cannot see each other.

### Why the worker container must not be scaled past 1

There is no leader election and no distributed lock. Adding one is real work —
a lease row, a renewal, and a decision about what a worker does when it loses
the lease mid-pass — and this level did not do it. The limit is documented
here, in `docker-compose.prod.yml`, and in `PRODUCTION_READINESS.md`, and it is
the single largest scaling constraint the platform has.

### Why one uvicorn process per container

The `CMD` has no `--workers`. The process holds the safe-mode latch, the
step-up grants and (when `RATE_LIMIT_SHARED=false`) the rate limiter, and
`--workers N` forks N copies of all three that cannot see each other. Scale by
adding containers, where at least the shared state is visibly shared.

`RATE_LIMIT_SHARED=true` in production for exactly this reason: two containers
with a per-process limiter each allow the full login rate.

### Why two nginx files rather than one with a flag

A configuration that switches TLS on a variable has a code path in which TLS is
off, and that path is reachable by a typo in an environment file. `nginx.prod.conf`
has no `listen 80` that proxies anything — port 80 exists only to redirect and
to answer the ACME challenge. A plain-HTTP path to the API is a plain-HTTP path
to a session cookie, whatever the redirect says.

### Why Redis is `noeviction`

Redis holds **no authoritative trading state**. Signals, orders, positions and
journal rows are all in PostgreSQL, and L40 measured that a dead Redis costs
latency, not correctness. What Redis holds is the shared rate limiter's
counters and in-flight events.

The default `maxmemory-policy` is `allkeys-lru`, which silently **drops** data
under memory pressure. A rate limiter that silently forgets is one that stops
limiting exactly when traffic is heaviest. `noeviction` refuses writes instead,
which is loud, and loud is recoverable.

AOF with `everysec` is on for the same reason and no stronger: losing the
limiter's counters on a restart is not a disaster, and one fsync a second is
cheap insurance against a burst that was being limited stopping being limited.

### Why not Kubernetes

Evaluated and refused. The platform is five containers with one that must not
be replicated, one that cannot be containerised at all (MT5, which needs a
Windows host with a logged-in terminal), and no autoscaling requirement — the
load is one webhook per signal.

Kubernetes would add a control plane, an ingress controller, a secret store, a
manifest set and a whole operational surface to run **five containers on one
host**, and its main benefit, horizontal scaling, is the thing the worker
constraint says not to do. Compose expresses this topology exactly and the
operator can read the whole deployment in three files.

Revisit when there is more than one host, or when a second worker becomes safe
to run.

### Why not blue/green or canary

Step 33 asks for an evaluation. Refused, and the reason is specific rather than
"too small".

Blue/green means both versions running at once. For a stateless API that is
fine; for **this** platform it means two processes each holding a safe-mode
latch, each running a startup reconciliation against the same broker account,
and each potentially consuming the same signal. The idempotency that makes that
survivable (`orders.intent_id` UNIQUE, signal deduplication) has never been
tested under two concurrent application versions, and L40 established that true
concurrency is the least-tested area of the codebase.

**The safe deployment for a trading platform is a brief, controlled outage:**
stop, migrate, start, verify, and reconcile on the way up — which the startup
sequence does automatically. `DEPLOYMENT_GUIDE.md` documents that sequence. It
costs seconds of downtime and it means only one process ever believes it owns
the account.

### Why there is still no automated backup

L38 refused to write one and L41 does not overturn that. A backup script this
project cannot test is a backup nobody should trust: `pg_dump` on a schedule
nothing runs, to storage nobody has chosen, with a key nobody holds, is a file
discovered to be empty on the day it matters.

What L41 added instead is the thing that makes a backup *possible*:
`wal_level=replica` and `max_wal_size=1GB` in the production Postgres, so the
WAL is there when somebody takes a base backup. `BACKUP_RESTORE.md` keeps its
honest RPO of *"since the last manual dump"*.

**This is an open blocker, and `PRODUCTION_READINESS.md` records it as one.**

---

## MT5

Unchanged, and it cannot be containerised on Linux. It needs a Windows host
with a logged-in terminal, so it runs as a process on that host and speaks to
the rest of the platform over PostgreSQL and Redis.

Nothing in this deployment starts it, and nothing depends on it starting. **No
broker adapter has ever been registered**, so `broker` reports NOT_CONFIGURED,
the trading-readiness endpoint reports BLOCKED, and that is the honest state
rather than a misconfiguration.

`NIGHTLY.md` holds the operational hazards: the algo-trading toggle does not
persist, and a backgrounded launcher can orphan its child process.

---

## What is deliberately NOT in this deployment

* **Live trading.** `TRADING_MODE=paper` and `LIVE_TRADING=false` are set
  explicitly in the production overlay, not merely inherited, so a reader of
  the production file can see it. All 11 `LIVE_GATES` remain False.
* **A registry push or a deploy step in CI.** There is no registry and no
  target host. A pipeline that pretends to deploy is worse than one that does
  not try.
* **Secrets in any committed file.** The production overlay uses `${VAR:?...}`
  for every secret, so a missing one fails the deploy instead of silently
  falling back to a development value.
