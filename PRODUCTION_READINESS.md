# PRODUCTION_READINESS.md

Assessed at L41, 2026-09-05. Every status has evidence and every status was
checked rather than assumed.

## The verdict

**READY to deploy in PAPER mode. NOT READY for live trading, and not close.**

Two blockers stand between this platform and real money, and neither is a
deployment problem:

1. **No backup has ever been taken or restored.** Step 41 is explicit that a
   backup is not operational until a restore has been tested. Nothing here has
   been.
2. **No broker adapter has ever been registered.** Every reconciliation,
   disconnect, rejection and unknown-state recovery in this platform has been
   exercised against a fake this repository wrote. Nothing has ever told the
   code it is wrong.

A third is not a blocker for paper but would be for live: **CI is red** — 16
`mypy` errors and 12 unformatted files, all pre-existing in L34–L38 code, all
gating jobs the workflow runs.

---

## Scorecard

| Category | Status | Evidence |
|---|---|---|
| **Architecture** | READY | Five services + one non-containerisable MT5 host. Documented in `DEPLOYMENT_ARCHITECTURE.md`; k8s and blue/green evaluated and refused with reasons. |
| **Infrastructure** | READY | Compose v2, three files. Both configurations validated with `docker compose config -q`; CI asserts the merged production config publishes only nginx 80/443. |
| **Security** | READY (paper) | L39 controls verified: headers on every response incl. errors, CORS wildcard refused at startup, step-up on four dangerous actions, per-user socket cap. 44 tests. **MFA still not built** and the posture says so in every response. |
| **Database** | PARTIAL | PostgreSQL 16, named volume, healthcheck, `max_connections=100`, `wal_level=replica`. Migrated live from 0021→0025 at L41 with **252 trades preserved**, verified by query. Blocked only on backups. |
| **Redis** | READY | AOF `everysec`, `maxmemory 512mb`, **`noeviction`** (the default `allkeys-lru` silently drops the rate limiter's counters), password required in production, no host port. L40 verified a dead Redis costs latency, not correctness. |
| **Backend** | READY | Starts, validates config, connects, serves, shuts down gracefully — workers stopped before the hub, 0 failures, verified from container logs at L41. Independent of any browser. |
| **Frontend** | READY | Multi-stage build, standalone output, non-root, no secret reaches it (`NEXT_PUBLIC_API_URL=/api` is the only public value). 247 tests, `tsc` and `eslint` clean. |
| **Workers** | PARTIAL | Now a separate container (`WORKERS_ENABLED`). Startup, heartbeat, graceful stop and idempotency verified. **Must not be scaled past 1: no leader election exists.** |
| **TradingView** | READY | HTTPS → nginx (5 r/s, burst 10, 64 KB cap) → gateway (constant-time secret, 120 s replay window, idempotency key, IP allowlist). Verified live at L41: valid→accepted, repeat→duplicate *same signal id*, wrong secret→401, malformed→401. |
| **Market data** | PARTIAL | Pipeline built and tested. **`market_bars` covers one instrument** — `market_data.freshness` reports UNKNOWN with *"no bar has ever been stored"*, which is honest rather than broken. |
| **MT5** | NOT APPLICABLE (this deployment) | Cannot run on Linux; needs a Windows host with a logged-in terminal. Nothing in the stack starts it and nothing depends on it. `NIGHTLY.md` holds the hazards. |
| **Broker** | **BLOCKED** | **No adapter has ever been registered.** `broker` reports NOT_CONFIGURED; `/health/trading` reports BLOCKED. This is the largest gap in the platform and no test can close it. |
| **OMS** | READY | State machine, `intent_id` uniqueness, unknown-order reconciliation — all tested, including that an unknown state is never retried. |
| **RiskEngine** | READY | Final veto, verified at L40 to stop every later stage: the venue receives nothing, and an approving AI cannot override it. |
| **PositionSizing** | READY | Refuses below the venue minimum rather than rounding up; the quantity reaching the venue is a multiple of the venue's own step. |
| **Bots** | READY | Backend services; browser closure is irrelevant. Supervisor, recovery gate and safe-mode interaction tested. |
| **AI** | PARTIAL | Seats and failure paths tested against stubs. **No model is deployed**; no provider is called. The property that matters — AI cannot bypass risk — is tested. |
| **Notifications** | READY | In-app, email and Discord channels; dedup, retry, severity, preferences. Verified at L40 that a failing bus does not stop a trade. |
| **Monitoring** | READY | L37 deployed and running. Confirmed live at L41: `worker_started` for both workers, and a `SERVICE_RECOVERED` incident observed in container logs. |
| **Recovery** | READY | L38 sequence runs on every boot. Confirmed live at L41 after a restart: `startup recovery complete, clean=false, needs_attention=1, safe_mode=false`. |
| **Backups** | **BLOCKED** | **No automated backup, and no restore has ever been tested.** `wal_level=replica` was added so a base backup *can* be taken. `BACKUP_RESTORE.md` keeps an honest RPO of "since the last manual dump". |
| **CI/CD** | PARTIAL | Four jobs; images built, stamped and verified, gated on tests. **The `backend` job is currently RED** — 16 `mypy` errors and 12 unformatted files, all pre-existing in L34–L38 code (found at L40). No deploy stage: there is no registry and no target. |
| **Testing** | READY | 2338 backend + 247 frontend passing, 1 skipped. Migrations tested against real PostgreSQL. Limits recorded in `KNOWN_TEST_LIMITATIONS.md`. |
| **Rollback** | PARTIAL | Documented per case, including the lossy 0024 downgrade. Images are now version-tagged rather than `latest`. **Not yet rehearsed**, and a schema rollback depends on the untested restore. |
| **Documentation** | READY | Seven deployment documents, all written against what was actually run. |

**READY 15 · PARTIAL 7 · BLOCKED 2 · NOT APPLICABLE 1**

---

## Production safety gate (Step 44)

| | Check | Result |
|---|---|---|
| ✅ | Paper/live separation | mode compared on signal *and* registration; a `live` signal cannot execute on a paper venue |
| ✅ | `LIVE_TRADING=false` by default | explicit in the production overlay, asserted on every test run, all 11 `LIVE_GATES` false |
| ✅ | Broker credentials protected | **none are stored anywhere in the platform** |
| ✅ | MT5 configuration | `assert_demo` refuses any non-demo `trade_mode` |
| ✅ | TradingView webhook secured | verified live: 401 without a secret |
| ✅ | RiskEngine / OMS / reconciliation / recovery / monitoring / notifications enabled | verified in the running stack |
| ✅ | Security controls enabled | L39 suite, 44 tests |
| ❌ | **Database backups** | **no automated backup exists** |
| ❌ | **Restore procedure verified** | **never tested** |
| ✅ | Health checks | four endpoints, all verified through nginx |
| ✅ | Graceful shutdown | verified from logs: workers first, 0 failures |
| ✅ | Rollback documented | `ROLLBACK_RUNBOOK.md` |
| ⚠️ | CI/CD verified | pipeline correct; **currently red on pre-existing lint/type errors** |
| ✅ | No secrets committed | scanned; production overlay uses `${VAR:?}` |
| ✅ | No destructive migrations | no `DROP DATABASE`, `DROP TABLE` or `RESET` anywhere |
| ✅ | No duplicate workers | `WORKERS_ENABLED` split; worker container is single-instance |
| ✅ | No duplicate scheduler | same mechanism |
| ✅ | No unsafe live activation | three independent gates, none passable by configuration |

**Two hard failures.** Both are backup/restore. The gate does not pass for
live trading.

---

## The blockers, in priority order

### 1. No tested backup or restore — BLOCKS live trading

Step 41 is explicit: *a backup is not considered operational until restore has
been tested.* There is no scheduled `pg_dump`, no storage target, no retention
policy, no encryption, and no restore has ever been performed.

L38 refused to write a script this project could not test, and L41 did not
overturn that — but it did add `wal_level=replica` so the WAL exists when
somebody takes a base backup. Closing this needs a decision about *where*
backups go, which is a deployment decision this repository cannot make alone.

### 2. No broker adapter has ever been registered — BLOCKS live trading

Every failure mode is exercised against `FakeBroker`, which agrees with the
platform's model of a venue **because it was written from that model**. Real
retcodes, partial fills, requotes and reconnect behaviour are unverified.

No test can close this. It closes the first time a real demo account is
connected, and that is a separate, deliberate decision.

### 3. CI is red — BLOCKS a trustworthy pipeline

16 `mypy` errors and 12 files failing `ruff format --check`, all in L34–L38
code and older tests, all pre-existing (found at L40). The workflow gates on
both, so **no commit since roughly L34 can have passed CI**. Cheap to fix and
it belongs to the levels that own the files.

### 4. Dependency floors, not pins — minor

`backend/requirements.txt` uses `>=`, so two builds a month apart can resolve
different patch versions. Every build is identified by commit, so this affects
reproducibility rather than traceability.

### 5. The worker container cannot be scaled — accepted, documented

No leader election. One worker replica only. Documented in three places.

---

## What was verified live at L41

Not claimed — run, against the deployed stack:

* Both compose configurations validate; production publishes **only** nginx.
* The image builds, is stamped (`stamped: true`), runs as **uid 10001**, and
  **all 293 `app.*` modules import inside it**.
* `alembic upgrade head` on the live database: **0021 → 0025**, applying the
  two migrations L40 fixed, with **252 trades from 2026-08-24 to 2026-09-02
  preserved** and the widened severity CHECK present.
* All four health endpoints answer correctly through nginx, including
  `/health/trading` → 503, which is the honest state.
* **WebSocket through nginx: HTTP 101.** With the pre-L41 config: HTTP 404.
* Webhook: accepted → duplicate (same signal id) → 401 → 401.
* No order was created by any of it. The webhook records; it does not execute.
* Graceful shutdown: workers stopped first, 0 failures.
* Restart: the recovery sequence ran and reported honestly.
