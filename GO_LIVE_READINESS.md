# GO_LIVE_READINESS.md

Subsystem-by-subsystem readiness for controlled production operation.
Assessed at L44, 2026-09-05.

**The question this document answers is not "is the code written?" It is "has
the behaviour been observed?"** A subsystem with complete code and no
observation is READY WITH CONDITIONS at best.

**Five readiness levels, and they are not equivalent:**

```
PAPER READY                       yes
SHADOW READY                      yes  (added at L44)
DEMO READY                        BLOCKED — adapter EXISTS, never connected
PRODUCTION INFRASTRUCTURE READY   yes, with one condition (backups)
LIVE TRADING READY                NO
```

---

## Matrix

| # | Component | State | Evidence | Tests | Known issues | Operational risk | Status | Required action |
|---|---|---|---|---|---|---|---|---|
| 1 | Frontend | built, 22 routes, 0.9 MB JS | `npm run build` succeeds; `tsc`, `eslint` clean | 247 | tested only against mocks | Low — no trading authority | **READY WITH CONDITIONS** | contract test |
| 2 | Authentication | Argon2id, server-side revocable sessions | 19 + 21 tests; live register/login through nginx | 40 | login CSRF accepted since L04 | Low | **READY** | — |
| 3 | RBAC | 16 permissions, 3 roles, server-enforced | every write route probed unauthenticated → none 2xx | in 162 | — | Low | **READY** | — |
| 4 | Database | PG16, 55 tables, 25 migrations | **live 0021→0025 with 252 trades preserved**; round-trip downgrade passes | 3 + 5 | — | **High — no backup** | **READY WITH CONDITIONS** | H-1 |
| 5 | Redis | AOF, noeviction, password in prod | **stopped live**: ready 503, webhook still worked | 15 | — | Low — not authoritative | **READY** | — |
| 6 | API | `/v1`, error envelope, pagination | 162 tests; live through nginx | 162 | — | Low | **READY** | — |
| 7 | WebSockets | hub, 52-type catalogue, per-user cap | **HTTP 101 through nginx** (404 before L41) | 51 | — | Low | **READY** | — |
| 8 | Market Data | provider abstraction, freshness | freshness measured against the alert's own time | 56 | **`market_bars` covers one instrument** | Medium — no live feed | **READY WITH CONDITIONS** | connect a feed |
| 9 | TradingView | secret, 120s replay window, idempotency | **live: accepted→duplicate(same id)→401→401** | 62 | — | Low | **READY** | — |
| 10 | Signal Engine | persisted, deduplicated | one signal per alert under a 10-way race on PG | in 37 | — | Low | **READY** | — |
| 11 | Strategy Engine | common interface | cannot bypass risk (AST) | 60 | — | Low | **READY** | — |
| 12 | Strategy Builder | spec → validation | malformed rejected; **no code execution** | 62 | — | Low | **READY** | — |
| 13 | Backtesting | wraps `tools/rule_backtest` | one simulator, not two | 47 | **does not run the full pipeline** | Medium — see consistency doc | **READY WITH CONDITIONS** | — |
| 14 | Market Replay | chronological, paper-only | no future access | 61 | same as 13 | Low | **READY** | — |
| 15 | Paper Trading | full chain | verified end to end | 93 | — | Low | **READY** | — |
| 16 | Risk Engine | mandatory veto | **venue receives nothing on veto**; refuses unknown modes | 86 | — | Low | **READY** | — |
| 17 | Position Sizing | stop→tick→qty→venue | refuses below minimum, never rounds up | 59 | — | Low | **READY** | — |
| 18 | OMS | state machine, `intent_id` UNIQUE | **unknown never retried** | 63 | — | Low | **READY** | — |
| 19 | BrokerAdapter | ABC; **three** implementations: Fake, Shadow, **MT5 (demo)** | interface verified; `app/brokers/mt5.py` is the ONLY module in the codebase that calls `order_send` | 64 + 12 | the MT5 one has never been connected | **High** | **READY WITH CONDITIONS** | H-2 |
| 20 | MT5 | **a demo adapter exists and is complete** | `MT5Adapter.mode = "demo"` (class attribute); `connect()` calls the toolkit's `assert_demo`, which raises `RefuseToTrade` on REAL/CONTEST/unknown and is re-raised rather than retried | — | **never connected — no terminal, no credentials** | **High** | **BLOCKED** | credentials + a Windows terminal |
| 21 | Position Management | SL/TP/trailing/exits | all through the adapter | 65 | untested against a venue | Medium | **READY WITH CONDITIONS** | H-2 |
| 22 | Bot Manager | backend workers | browser-independent; separate container | 36 | — | Low | **READY** | — |
| 23 | AI Data Pipeline | features, datasets | leakage controls tested | 68 | — | Low | **READY** | — |
| 24 | AI Models | registry, metadata | — | 56 | **none deployed** | Low — advisory only | **READY WITH CONDITIONS** | deploy one |
| 25 | Model Registry | lifecycle, promotion gated | promotion above training permission | 59 | — | Low | **READY** | — |
| 26 | AI Validation | no default PF, no hardcoded PASS | 75 tests | 75 | — | Low | **READY** | — |
| 27 | AI Strategy Integration | advisory | **approving AI cannot override a veto** | 59 | stub provider | Low | **READY** | — |
| 28 | Portfolio | balance/equity/exposure/drawdown | 98 tests | 98 | — | Low | **READY** | — |
| 29 | Trade Journal | signal→…→trade traceable | idempotent | 62 | — | Low | **READY** | — |
| 30 | Analytics | derived from records | 91 tests | 91 | — | Low | **READY** | — |
| 31 | AI Trade Review | deterministic facts → review | no decision-time leakage | 57 | stub provider | Low | **READY** | — |
| 32 | Notifications | one service, 4 channels | **failure isolated from trading** | 74 | — | Low | **READY** | — |
| 33 | Discord | adapter only | env labels, redaction, retry | 54 | — | Low | **READY** | — |
| 34 | Admin | users/roles/audit | dangerous actions need reason + phrase + step-up | 40 | — | Low | **READY** | — |
| 35 | Monitoring | 5-state health, hysteresis | **observed running live** | 50 + 23 | — | Low | **READY** | — |
| 36 | Recovery | 16-step startup, safe mode | **observed live**; DB+Redis outage recovered with no restart | 46 | — | Low | **READY** | — |
| 37 | Security | headers, CORS, step-up, socket cap | 44 tests; secret scan clean | 44 | **no MFA** | Medium | **READY WITH CONDITIONS** | build TOTP |
| 38 | Deployment | 3 compose files, TLS, stamping | verified live end to end | 30 | — | Medium | **READY WITH CONDITIONS** | H-1 |
| 39 | Backup/Restore | **nothing** | — | 0 | **never taken, never restored** | **HIGH** | **BLOCKED** | H-1 |
| 40 | Integration Tests | 37 cross-subsystem | **37/37 on PostgreSQL** incl. genuine concurrency | 37 | — | Low | **READY** | — |

## Totals

**READY 26 · READY WITH CONDITIONS 10 · BLOCKED 2 · NOT APPLICABLE 0**

## The two BLOCKED items

Both are the same shape: **something that has never happened**, not something
built wrongly.

**39 — Backup/Restore.** No backup has ever been taken and no restore has ever
been performed. Step 41 of L41's own brief says a backup is not operational
until a restore has been tested. Closing it needs a decision about where
backups go, which this repository cannot make alone.

**20 — MT5.** A complete **demo** adapter exists (`app/brokers/mt5.py`) and has
**never been connected**: there is no terminal on this host and no credentials.
Every reconciliation, disconnect, rejection and unknown-order path is therefore
exercised against `FakeBroker` — one that agrees with the platform's model of a
venue because it was written from that model. **No test can close this.**

*Corrected during L44.* An earlier pass of this document said "no adapter
exists" and that MT5 appeared only in read-only modules. Both were wrong: the
adapter is there, it wraps `tools/mt5_paper` rather than reimplementing it, and
it is the **only** module in the entire codebase that calls `order_send`. The
correction makes the position better, not worse — what is missing is a
connection, not the code.

## Discrepancies found between documented, actual and tested state

Phase 1 asks for these explicitly. Four were found and all four are now
corrected:

0. **This document itself claimed no MT5 adapter existed.** It does —
   `app/brokers/mt5.py`, demo-only by class attribute and by the `assert_demo`
   fence. Found and corrected within L44 by scanning for `order_send` rather
   than for `import MetaTrader5`, which the adapter does not do directly (it
   calls the toolkit). **A scan is only as good as its pattern**, and this one
   is the reason the level re-ran its own Phase 7 check a second way.

1. **`KNOWN_TEST_LIMITATIONS.md` claimed the full suite had never completed.**
   It had, three times. Corrected at L42.
2. **The same document claimed migrations were untested backward.** A
   round-trip downgrade/upgrade against PostgreSQL had been added at L40.
   Corrected at L42.
3. **The L44 brief names six documents that do not exist**
   (`00_MASTER_CONTEXT.md`, `FINAL_AUDIT.md`, `SECURITY_AUDIT.md`, and three
   others). Their content exists under different names —
   `FINAL_AUDIT_REPORT.md`, `SECURITY.md`, `PRODUCTION_RUNBOOK.md`. **No
   duplicates were created**; this level added only the four genuinely new
   documents it names.

No discrepancy was found between documented behaviour and actual code
behaviour in any trading-safety claim. Every such claim was re-verified
structurally at L42 and again at L44.
