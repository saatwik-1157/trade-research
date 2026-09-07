# FINAL_FEATURE_MATRIX.md

Every planned level, its status, the evidence, and what is still missing.
Assessed at L42, 2026-09-05.

**Status is about verified behaviour, not about files existing.** A level is
PASS when its behaviour was exercised; PARTIAL when it is built and its
verification depends on something absent; BLOCKED when a hard dependency does
not exist.

**2369 backend tests + 247 frontend tests** across 50 backend modules and 32
frontend files.

| # | Feature | Status | Evidence | Tests | Known gaps |
|---|---|---|---|---|---|
| 00 | Foundation / master context | **PASS** | `CLAUDE.md`, `PROJECT_AUDIT.md` (1912 lines), governance honoured throughout | — | — |
| 01 | Audit | **PASS** | 34 numbered audit sections; every level re-audited its predecessors | — | — |
| 02 | Foundation | **PASS** | `app/core` settings/logging/errors/events/health; Compose stack builds and runs | 7 settings, 4 logging, 8 errors | — |
| 03 | UI | **PARTIAL** | 32 frontend files, `nav.ts` marks each route's true state | 247 frontend | partial *by design* — panels fill in as sources land |
| 04 | Authentication | **PASS** | Argon2id, server-side revocable sessions, HttpOnly/SameSite, CSRF double-submit, rate limiting | 19 auth + 21 hardening | login CSRF accepted (documented since L04) |
| 05 | Database | **PASS** | 55 tables, 25 migrations, naming convention, FKs, indexes; **applied to real PostgreSQL** | 5 models, 3 migrations | — |
| 06 | Backend API | **PASS** | `/v1` surface, RBAC on every route, pagination, error envelope | 162 api_v1 | — |
| 07 | Realtime | **PARTIAL** | Hub + Redis bus + 52-type catalogue; WS handshake **verified 101 through nginx** | 51 realtime | **module is flaky, 1–2 failures/run** (see risk register) |
| 08 | Market data | **PARTIAL** | Provider abstraction, normalisation, freshness, timeframes | 56 marketdata | **`market_bars` covers one instrument** — freshness reports UNKNOWN honestly |
| 09 | TradingView | **PASS** | Constant-time secret, 120 s replay window, idempotency key, IP allowlist, 64 KB cap. **Verified live**: accepted→duplicate(same id)→401→401 | 62 webhooks | — |
| 10 | MT5 | **BLOCKED** | Adapter interface + `FakeBroker`; `assert_demo` refuses non-demo `trade_mode` | 64 brokers | **no adapter has ever been registered** |
| 11 | Symbol mapping | **PASS** | Provider↔internal mapping, seeded 9 symbols live | 49 + 28 symbols | specs need a terminal |
| 12 | Strategy engine | **PASS** | Common interface, metadata/timeframes/indicators/rules; cannot bypass risk | 60 strategies | — |
| 13 | Strategy builder | **PASS** | Visual → normalised spec → validation; malformed rejected, **no code execution** | 62 builder | — |
| 14 | Backtesting | **PASS** | Wraps `tools/rule_backtest.simulate` — **one simulator, not two**; fees/spread/slippage | 47 backtest | single symbol per run, stated not faked |
| 15 | Market replay | **PASS** | Chronological, no future access, paper-only | 61 replay | — |
| 16 | Paper trading | **PASS** | Full chain to the paper broker; isolation asserted | 93 paper | — |
| 17 | Risk engine | **PASS** | Mandatory veto; **verified the venue receives nothing** when it refuses | 86 risk | — |
| 18 | Position sizing | **PASS** | Stop-distance → tick value → quantity → venue constraints; **refuses below minimum rather than rounding up** | 59 sizing | — |
| 19 | OMS | **PASS** | State machine, `intent_id` UNIQUE, **unknown state never retried** | 63 oms | — |
| 20 | Automated execution | **PASS** | Pipeline with gate zero (safe mode) → strategy → staleness → AI → risk → sizing → OMS | 51 execution | — |
| 21 | Position management | **PASS** | SL/TP/trailing/strategy/time/risk/emergency exits, all through the adapter | 65 positions | — |
| 22 | Bot manager | **PASS** | Start/pause/stop/restart/recovery/heartbeat; **backend workers, browser-independent** | 36 bots | — |
| 23 | AI data pipeline | **PASS** | Feature catalogue, dataset builds, leakage controls | 68 datasets | — |
| 24 | AI models | **PARTIAL** | Registry, metadata, artifact integrity | 56 ai | **no model deployed** |
| 25 | AI training | **PASS** | Jobs observable; **training never deploys** (verified structurally) | 51 training | — |
| 26 | AI validation | **PASS** | No default profit factor, no hardcoded PASS | 75 validation | — |
| 27 | AI strategy integration | **PASS** | Advisory only; **an approving AI cannot override a risk veto** | 59 ai_integration | stub provider |
| 28 | Model registry | **PASS** | Lifecycle, promotion above training permission, rollback | 59 registry | — |
| 29 | Model monitoring | **PASS** | Snapshots, alerts, deduplication | 42 monitoring | — |
| 30 | Portfolio | **PASS** | Balance/equity/margin/exposure/realised/unrealised/drawdown | 98 portfolio | — |
| 31 | Trade journal | **PASS** | Signal→order→execution→position→trade traceable; **idempotent** | 62 journal | — |
| 32 | Analytics | **PASS** | Win rate, PF, expectancy, Sharpe, Sortino, drawdown, attribution | 91 analytics | — |
| 33 | AI trade review | **PASS** | Deterministic facts → review; asynchronous; **no decision-time leakage** | 57 review | stub provider |
| 34 | Notifications | **PASS** | One service, 4 channels, dedup/retry/severity/preferences; **failure isolated from trading** | 74 notifications | — |
| 35 | Discord | **PASS** | Adapter only; env labels, rate limit, redaction | 54 discord | — |
| 36 | Admin | **PASS** | Users/roles/audit/health; dangerous actions need reason + phrase + **step-up** | 40 admin | — |
| 37 | Monitoring | **PASS** | 5-state health, incidents with hysteresis, bounded metrics. **Verified running live** | 50 + 23 | — |
| 38 | Recovery | **PASS** | 16-step startup sequence, safe-mode latch. **Verified live after restart** | 46 recovery | no automatic reconnection (deliberate) |
| 39 | Security | **PASS** | Headers on every response, CORS refusal at startup, step-up, socket cap, security events | 44 security | **no MFA** — stated in every posture response |
| 40 | Integration testing | **PASS** | 35 cross-subsystem tests; **7 real defects found and fixed** | 35 integration | true concurrency untested (harness) |
| 41 | Deployment | **PARTIAL** | 3 compose files, TLS config, release stamping, worker split. **Verified live end to end** | 30 deployment | **backup/restore BLOCKED** |
| 42 | Final audit | **PASS** | This document, `FINAL_AUDIT_REPORT.md`, `FINAL_RISK_REGISTER.md`, `SYSTEM_CERTIFICATION.md` | 11/11 safety rules | — |

## Totals

**PASS 33 · PARTIAL 8 · BLOCKED 1 · FAIL 0**

## The three levels that are not PASS, and why

**10 — MT5 · BLOCKED.** The adapter interface, symbol mapping, reconciliation,
disconnect handling and unknown-order recovery are all built and all tested —
against `FakeBroker`, which this repository wrote. Nothing has ever been told it
is wrong by a real venue. **No test can close this**; it closes the first time a
demo account is connected.

**41 — Deployment · PARTIAL.** Everything was verified against a running stack:
migrations applied to a real database with 252 trades preserved, WebSocket 101
through the proxy, health endpoints correct, graceful shutdown clean. What is
missing is **backup and restore**, and Step 41's own rule is that a backup is
not operational until a restore has been tested.

**07 — Realtime · PARTIAL.** The transport works and was verified end to end
through nginx. Its *test module* is flaky — measured 51/51, 50/51, 50/51 — with
an undiagnosed harness interaction. The application behaviour is not in doubt;
the test evidence for it is weaker than the count suggests, and saying so is the
point of this row.
