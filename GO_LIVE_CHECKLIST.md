# GO_LIVE_CHECKLIST.md

L44, 2026-09-05.

```
[x] verified with evidence      [ ] not verified      [!] blocked
```

**Nothing is marked `[x]` without evidence named on the line.** Items that
could only be verified against a real venue or a real restore are `[!]`, not
`[ ]` — the distinction is that no amount of further work on this machine
closes them.

---

## A. Infrastructure

- [x] Stack builds and starts — `docker compose up -d --build`, 5 containers healthy
- [x] Both compose configurations valid — `docker compose config -q` on base and prod
- [x] Production publishes **only nginx 80/443** — merged config parsed; asserted by CI
- [x] Resource limits on every container — `test_every_container_has_a_memory_limit`
- [x] Startup order enforced — `depends_on: service_healthy`
- [x] Graceful shutdown — observed: workers before hub, 0 failures
- [x] Release stamping — `/health` returns `stamped: true`, commit `c961abfc1667`
- [ ] Deployed on a host that is not this laptop

## B. Security

- [x] No tracked secrets — `git ls-files` clean; no `.env` on disk
- [x] No secret-shaped literals — AWS/GitHub/Slack/PEM patterns absent
- [x] Frontend receives only `NEXT_PUBLIC_*` — only `NEXT_PUBLIC_API_URL=/api`
- [x] No `eval`/`exec`/`subprocess`/`pickle`/`shell=True` in `app/`
- [x] Security headers on every response including errors — 44 tests
- [x] CORS wildcard refused **at startup**
- [x] Step-up re-authentication on 4 dangerous actions — scoped, single-use
- [x] Per-user WebSocket cap — 8, refusal counted
- [x] Webhook: constant-time secret, 120s replay window, idempotency, IP allowlist
- [x] Container non-root — uid 10001
- [x] Frontend dependency audit — `npm audit --omit=dev`: **0 vulnerabilities**
- [ ] **MFA** — NOT BUILT; reported as absent in every posture response
- [ ] TLS certificate installed and renewing (config written, never deployed)

## C. Database

- [x] Migrations apply to real PostgreSQL — **0021→0025 live**
- [x] Existing history preserved — **252 trades, 2026-08-24 to 2026-09-02**
- [x] Round-trip downgrade and re-upgrade — `test_round_trip_downgrade_then_upgrade`
- [x] Schema head reported and comparable — `/health` vs `alembic current`
- [x] No destructive operation reachable in production — every `DROP` is test-only
- [x] Migration tests refuse a non-scratch database — added L42, verified
- [x] Persistent volume, not ephemeral — named `tr-postgres-data`
- [!] **Backup job** — none exists
- [!] **Restore tested** — never performed

## D. Market Data

- [x] Provider abstraction, normalisation, timeframes — 56 tests
- [x] Freshness measured against the alert's own timestamp, not arrival
- [x] Stale data cannot create a signal — verified
- [ ] A live feed connected — `market_bars` covers one instrument; reports UNKNOWN honestly

## E. TradingView

- [x] Valid alert accepted — **live through nginx**
- [x] Duplicate → `duplicate`, **same signal id**, one signal — live
- [x] Invalid secret → 401 — live
- [x] Malformed JSON → 401 — live
- [x] Expired alert → 422 on its own timestamp — live
- [x] **10-way concurrent duplicate → one signal, no 5xx** — on PostgreSQL
- [x] Distinctness control — 10 different alerts → 10 signals
- [x] Rate limited at the proxy (5 r/s) and in the app (120/60s)
- [x] Webhook records; it does **not** execute — no order created
- [x] **An alert routes to a bot, an account and a risk budget** — L45 F-1;
      the gateway wrote none of the five keys the worker reads, so all 118
      signals sat at `new`
- [x] **Alert → order proven end to end through the deployed pipeline** —
      `test_an_alert_becomes_an_order_through_the_deployed_wiring`
- [x] An alert **cannot** name its own account or risk budget — neither is in
      the schema's vocabulary; unknown keys land in `extra`
- [x] Two enabled bots on one strategy version → **refused, never resolved**
- [ ] **Bracket opt-in enabled for any bot** — `use_alert_bracket` is false
      everywhere; turning it on is an approval, not a default

## F. MT5

- [x] Adapter exists and is demo-only — `mode = "demo"` class attribute
- [x] `assert_demo` called on connect; `RefuseToTrade` re-raised, not retried
- [x] **`app/brokers/mt5.py` is the only module calling `order_send`**
- [x] Read-only MT5 use elsewhere is genuinely read-only
- [!] **Connected to a demo terminal** — no terminal, no credentials
- [!] Order submission, fills, partial fills, reconnect against a real terminal

## G. Strategy

- [x] Common interface; cannot bypass risk — AST-verified
- [x] Builder rejects malformed specs; **no code execution**
- [x] Backtest wraps the toolkit — one simulator, not two
- [x] Replay chronological, no future access
- [ ] Backtest through the **full** pipeline — see `EXECUTION_CONSISTENCY.md` §1

## H. AI

- [x] Advisory only; **approving AI cannot override a risk veto**
- [x] AI reaches neither broker nor OMS — AST-verified
- [x] Training cannot deploy a model — AST-verified
- [x] Promotion gated above the training permission
- [x] Validation has no default profit factor and no hardcoded PASS
- [ ] A real provider called; a model deployed — every AI test drives a stub

## I. Risk

- [x] Mandatory and unbypassable — **the venue receives nothing on a veto**
- [x] Refuses any mode outside `paper`/`demo` — fails closed on unknown
- [x] Kill switches; safe mode blocks at gate zero
- [x] Risk verdict now **serialised** in every decision record — fixed at L44

## J. OMS

- [x] State machine; invalid transitions rejected
- [x] `intent_id` UNIQUE — the idempotency backstop, and **it now guards the
      automated path**: until the L45 C-1 fix the pipeline wrote no `orders`
      row at all, so the constraint had nothing to constrain
- [x] **Unknown state never blindly retried — including across a restart.**
      This held only within one process lifetime until L45; every duplicate
      test ran inside one process, which is why it survived six audits.
      `tests/test_execution_durability.py`
- [x] No duplicate order under a genuine race
- [x] An order that cannot be recorded is **not sent** — outcome
      `not_recorded`, order discarded, signal not consumed
- [x] The approval binding check **can fail** — L45 C-4; it previously compared
      the approval to itself

## J2. Risk (added L45)

- [x] `Approval` is minted by **one class**, asserted over every file in `app/`
- [x] A close is approved by `approve_close`, which records every limit it went
      over and enforces only the mode fence — **a limit that bounds opening
      risk must not refuse a reduction of it**

## K. Position Management

- [x] SL/TP/trailing/strategy/time/risk/emergency exits — 71 tests
- [x] All broker actions go through the adapter **and through the OMS**. Until
      the L45 C-2 fix the close called `manager.adapter.close_position`
      directly — no Approval, no `intent_id`, no order record — while the
      module docstring asserted the opposite. `.adapter` is now read nowhere in
      the module, and a test asserts that.
- [x] Two concurrent identical closes reach the venue **once**
- [x] A breached risk limit **cannot trap an open position**, and the override
      is recorded on the order
- [!] Verified against a real venue

## L. Bots

- [x] Backend services; browser closure irrelevant
- [x] Separate worker container in production
- [x] Heartbeat, recovery, safe-mode interaction
- [x] **Worker container must not be scaled past 1** — documented in 3 places

## M. Monitoring

- [x] Five-state health with hysteresis — **observed running live**
- [x] Bounded metric cardinality
- [x] Monitoring reports; it does not gate trading
- [x] Security posture is one component of it, not a second dashboard

## N. Recovery

- [x] 16-step startup sequence — **observed live after restart**
- [x] Safe mode latches with a reason; blocks 3 paths; nothing observational
- [x] Release requires step-up **and** re-runs the whole sequence
- [x] **DB + Redis outage recovered with no restart** — verified live
- [x] Reconcilers report; they never repair

## O. Notifications

- [x] One service, 4 channels, dedup, retry, severity, preferences
- [x] **Failure isolated from trading** — a raising bus does not stop an order
- [x] Discord: environment labels, redaction, retry
- [ ] Exercised against a real Discord/SMTP endpoint

## P. Backup

- [!] **Backup job configured** — none
- [!] **Restore tested** — never
- [x] WAL configured so a base backup is possible — `wal_level=replica`
- [x] Restore procedure documented — `BACKUP_RESTORE.md`, 15 steps

## Q. Deployment

- [x] Three compose files; dev unchanged, prod private by construction
- [x] TLS config written, WebSocket upgrade **verified 101 through nginx**
- [x] Release versioned; never tagged `latest`
- [x] Rollback documented per case, including the lossy 0024 downgrade
- [ ] Rollback **rehearsed**
- [x] CI gates images on tests passing

## R. Testing

- [x] **2421 backend tests pass, 6 skipped, 0 failed** — 2026-09-06, after the
      C-1 to C-4 fixes (22:06 wall clock)
- [x] 247 frontend tests pass
- [x] Integration suite **37/37 on real PostgreSQL**
- [x] `mypy` clean over 346 files; `ruff check` + `format --check` clean
- [x] `tsc --noEmit`, `eslint`, `npm run build` clean
- [x] Shadow mode — 12 tests
- [x] Deployment assertions — 30 tests
- [ ] Coverage measured — no `--cov` run; **no percentage is claimed anywhere**
- [x] Load/stress testing — performed at L48 against the deployed stack; it
      found the nginx 503-on-rate-limit defect (see
      `PRODUCTION_PILOT_READINESS.md`)
- [x] **Cross-restart regression** — 27 tests, foreign keys enforced

## S. Human Approval

- [x] `TRADING_MODE=paper` default — asserted every run
- [x] `LIVE_TRADING=false` default — asserted every run
- [x] All 11 `LIVE_GATES` false
- [x] **Live refused even with both flags set** — 10 blockers remain
- [x] `TRADING_MODE=live` without the flag **refuses to start**
- [x] RiskEngine refuses any mode outside paper/demo
- [x] `assert_demo` fence below the platform
- [x] Dangerous actions: permission + reason + confirmation phrase + step-up + audit
- [ ] **A named human has authorised live trading** — nobody has, and nobody should until P and F are closed

---

## Tally

**Verified `[x]`: 71 · Not verified `[ ]`: 14 · Blocked `[!]`: 6**

## The six blocked items

All six reduce to two facts: **no backup has ever been restored**, and **no
terminal has ever been connected**. Neither is a code defect and neither can be
closed on this machine.

## Go-live decision

**PAPER: go.** **SHADOW: go.** **DEMO: blocked on credentials.**
**LIVE: no**, and the checklist is not the reason — section F and section P are.
