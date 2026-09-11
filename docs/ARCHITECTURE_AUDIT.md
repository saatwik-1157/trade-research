# ARCHITECTURE_AUDIT.md

**Status: `AUDIT_COMPLETE`** · 2026-09-10 · read-only pass, no architecture changed.

Source of truth is the repository and the venue, not the design documents. Where
a docstring and the code disagree, the code is reported and the drift is noted.

---

## The headline

**The TradingView-first architecture in §5 is already built.** It is
`app/execution/pipeline.py`, it has every stage the brief specifies in the
specified order, it fails closed at each stage, and its own tests assert that it
holds no venue adapter.

The danger is not a missing architecture. It is that **a second, older path to
the broker exists beside it and bypasses all of it**, and that path is the only
one that has ever traded this account.

```
PATH A — the research harness              PATH B — the platform
(has placed every real order)              (has never placed one at this venue)

run_overnight.py                           TradingView alert
  └ take_profit.py                           └ webhooks/gateway.py
      └ mt5_paper.py                             body cap → parse → hmac auth
          └ mt5.order_send()  ×4                 → validate → AGE CHECK
                                                 → idempotency → symbol
  imports nothing from app/                      → strategy → Signal(new)
  no RiskEngine, no OMS, no signal                   └ ExecutionWorker (claims batch)
  no kill switch, no journal                             └ execution/pipeline.py
  a timer is its only trigger                                validate signal
                                                             validate strategy
                                                             AI seat (may only decline)
                                                             RISK  ← only source of Approval
                                                             SIZING ← only source of quantity
                                                             RISK binds (request_hash)
                                                             OMS   ← only path to a venue
                                                                 └ brokers/mt5.py
                                                                     └ order_send() ×1
```

Every §46 invariant is satisfied on Path B and violated on Path A. **The
programme is therefore to fence or retire Path A, not to build Path B.**

*Amended 2026-09-11.* Path A was fenced rather than retired: it now reaches
`RiskEngine` through `tools/risk_gate.py` and cannot send an opening order
without an `Approval`. The diagram above is otherwise unchanged and still
accurate — Path A has no signal, no OMS and no `orders` row, and those are
what remain of this finding.

---

## A. Architecture map

| Layer | Technology | Notes |
|---|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, Redis | 327 modules, 90,084 lines |
| Frontend | Next.js / React / TypeScript | 122 sources, 16,879 lines, 25 routes |
| Database | PostgreSQL 27 migrations, head `0027_capital_reservations` | SQLite in tests — see **I** |
| Broker | MetaTrader5 Python package | one adapter, `app/brokers/mt5.py` |
| Research | `tools/`, 27 standalone scripts | separate lineage, own CI gates |
| Deploy | Docker Compose (dev + prod), nginx TLS | prod publishes only nginx 80/443 |
| CI | GitHub Actions: 7 toolkit gates × py3.10/3.12/3.14, backend lint+types+tests, frontend, image build+verify | `.github/workflows/tests.yml` |
| Packaging | **none** | see **L** |

Backend packages by size: `api` 13,373 · `ai` 5,846 · `portfolio` 4,304 ·
`notifications` 3,873 · `models` 3,367 · `paper` 3,180 · `monitoring` 3,083 ·
`risk` 2,995 · `safety` 2,923 · `validation` 2,838 · `datasets` 2,796 ·
`review` 2,684 · `brokers` 2,615 · `observability` 2,433 · `positions` 2,425 ·
`strategies` 2,369 · `oms` 2,335 · `execution` 2,211 · + 20 more.

## B. Component inventory and disposition

| Component | Disposition | Why |
|---|---|---|
| `app/execution/pipeline.py` | **KEEP** | Is §5. Every stage present, fails closed, holds no adapter |
| `app/webhooks/gateway.py` | **KEEP + MODIFY** | Has hmac auth, age check, idempotency, body cap. Docstring is stale (**F**) |
| `app/risk/` engine + config | **KEEP + MODIFY** | 30+ veto codes. Missing weekly loss, consecutive losses, explicit correlation veto (**G**) |
| `app/oms/` state machine | **KEEP** | Full §13 vocabulary incl. `unknown` = "reconciled, never retried" |
| `app/brokers/mt5.py` | **KEEP** | The single `order_send` on Path B. Already the BrokerAdapter §14 asks for |
| `app/recovery/reconciliation.py` | **KEEP** | Startup reconciliation exists |
| `app/workers/` + 4 workers | **KEEP + MODIFY** | Heartbeats, stale detection, cooperative shutdown. No cross-worker watchdog (**J**) |
| `app/safety/` invariants + gates | **KEEP** | 31 invariants (29 ENFORCED), 12 gates, `CONDITIONALLY_CERTIFIED` honestly |
| `app/ai/` | **KEEP** | Advisory seat; may only decline. §8 satisfied by construction |
| `app/journal/`, `app/replay/` | **KEEP** | §19/§24 substantially present; `execution_id` is the correlation id |
| `app/paper/engine.py` | **KEEP** | Bar-driven simulator; shares every gate with the pipeline |
| `tools/` research scripts | **KEEP** | Separate lineage, own CI, no execution authority. §28 satisfied |
| `tools/take_profit.py`, `run_overnight.py`, `mt5_paper.py` | **REFACTOR — highest priority** | Path A. Bypasses every control (**E**) |
| Windows packaging | **ADD** | Does not exist (**L**) |
| Watchdog / supervisor-of-supervisors | **ADD** | (**J**) |
| 177 root markdown files | **MERGE** | 52 stubs under 2.5 KB, heavy overlap, flat namespace |
| Nothing | **REMOVE** | No dead component identified with confidence this pass |

## C–G. Flow, crash and loss analysis

Full detail in `EMERGENCY_STABILITY_AUDIT.md` and `LOSS_ROOT_CAUSE_REPORT.md`.
Summary of what was measured:

**D. Crash root cause.** Eight consecutive sessions, all terminated before
their deadline, **none ran the `--flat-by` flush**. Two hypotheses refuted by
evidence: Windows power log shows no sleep during the three most recent
sessions (the keep-awake hold works), and all 27 logs contain zero tracebacks
and zero `KeyboardInterrupt`. The processes are terminated externally — a
closed console, not a code fault. One real defect found and fixed: a dropped
terminal returns `None` from `account_info()`, which was caught as a formatting
error and logged "the session continues" for 23 consecutive blind passes, with
`--max-daily-loss` unable to evaluate.

**E. Loss analysis.** 476 closed trades over 6 days, read from the venue:
net **−10.51 USD on a 100,000 demo deposit**, max drawdown −65.50 (−0.065%).
Win rate 80.5% against a break-even requirement of 82.1% — of 470 decided
trades, 386 wins were needed and 383 arrived. The cause is the harvest
geometry plus `--rule random`, not software, not AI (no model is in this
path), and **not excessive risk** — loss control held throughout.

**F. TradingView gap analysis.** Smaller than expected.

| §6/§7 requirement | State |
|---|---|
| Webhook gateway | present |
| Secret authentication | present, `hmac` |
| Schema validation | present, `webhooks/schema.py` |
| Signal age / expiry check | **present** — in the pipeline, before idempotency |
| Idempotency / dedupe | present, indexed, `IntegrityError` path |
| Symbol normalization | present, `app/symbols/` |
| Strategy validation | present |
| Signal audit log | present, `WebhookEvent` |
| Body size cap | present |
| **Gap: gateway docstring is stale** | claims risk/sizing/OMS "are not built"; all three exist. Misleads any reader auditing the gate |
| **Gap: `SIGNAL_CREATED` has no consumer** | `ExecutionWorker` polls `signals` in `new` instead. Works, but the event is decorative and the docstring implies otherwise |

**G. Risk gap analysis.** Veto vocabulary already covers kill switches
(global/account/strategy), trading mode, market open, signal freshness,
duplicate signal, max open positions, one-position-per-symbol, max trades/day,
max daily loss, max drawdown, max exposure, max leverage, max risk/trade,
spread, margin, cooldown, stop-loss-required, max position size, max
concentration, risk/reward, and account/bot/strategy/symbol state.

Genuine gaps: **weekly loss** (0 hits), **consecutive losses** (present only as
a monitoring alert, not a veto), **correlation** (present in AI config and
portfolio, not as a risk veto), **liquidity** (thin). "Circuit breaker" does
not exist by that name; `risk_halted` is functionally one.

## H. MT5 / broker gap analysis

One adapter, one `order_send`, `unknown` never retried, reconciliation module
present. **Gap:** the disconnect→reconnect→reconcile→resume sequence of §14 is
implemented for Path B but was absent on Path A; Path A's version was the bug
fixed this week. **Gap:** no broker account is registered
(`accounts_broker: 0`), so Path B refuses with `no_venue` — it has never been
exercised against this venue end to end.

## I. Database gap analysis

27 migrations, no destructive resets, history preserved. **The significant
gap is test-side, and it is documented in the repository's own
`KNOWN_TEST_LIMITATIONS.md`:** SQLite runs with foreign keys OFF and nothing
issues the pragma, so *"any route can write a dangling reference, pass every
test, and fail on the real database — which is exactly what `POST /v1/orders`
did, through 174 API tests."* Only `orders.signal_id` is covered. **2,700 test
functions are therefore weaker evidence than the count suggests**, and no
Postgres-backed integration job exists in CI.

## J. Overnight stability gap analysis

Present: per-worker heartbeats, stale-heartbeat detection at 2× interval,
cooperative shutdown, `MonitoringWorker`, UTC internally, `server_day_start()`
for day boundaries (a documented fix for a limit whose day moved with the
operator's timezone).

Absent: a **watchdog** across workers (0 hits), a **crash journal**
(`CRASH_REPORTS/` does not exist), restart-loop limiting, and any mechanism
that survives its console being closed — which is the actual observed failure.

## K. AI / model gap analysis

`ModelTrainingNeedAssessment` verdict: **DO_NOT_TRAIN.** Not because the
pipeline is weak, but because the repository has already measured the answer.
`CLAUDE.md` records six search families across five universes — FX majors,
crosses, metals, indices, crypto, candle shape — and none separates from its
own permutation null. The shape search scored a best in-sample t of **−0.04**
against a shuffled null averaging 0.37. A model trained on these features
would search the same space with more parameters, which is the condition under
which the 36-cell bracket sweep scored the **random** rule at 1.76 against the
best real candidate's 0.83. §29's preconditions are not met and the brief's own
§57 rule applies.

## L. Windows packaging gap analysis

**Nothing exists.** No PyInstaller spec, no Inno/NSIS script, no
electron-builder, no MSIX. The application runs as Docker Compose (postgres,
redis, api, worker, frontend, nginx) plus, for Path A, a `.bat` file that
probes for an interpreter. §37–§39 are entirely unbuilt.

The stack constrains the answer: a FastAPI + Postgres + Redis + Next.js system
does not become one `.exe`. Realistic target is an installer that provisions
the runtime and registers a Windows Service, not a single binary. This needs
its own level and its own decision.

## M. Security gap analysis

Good: no secret in `.env` (the TradingView secret must be exported in the
shell, and unset means the gateway **refuses every alert** — correct default);
`public_summary()` deliberately omits URLs and secrets; prod compose publishes
only nginx 80/443, verified in CI; image runs non-root, verified in CI; argon2
password hashing. Not audited this pass: injection surfaces, CORS/CSRF
specifics, log redaction under load. Deferred to its own level.

## N. Prioritised fix plan

| P | Work | Effort | Status |
|---|---|---|---|
| **P0** | Stop uncontrolled trading | — | **Already satisfied.** Nothing running, account flat, paper mode, 12 live blockers, `assert_demo` |
| **P1** | Crash root causes | done | Identified; disconnect defect fixed and tested |
| **P1b** | Recoverable flush + console-independent session + `CRASH_REPORTS/` | S | **done** — proven by session `20260910-224639`, which reached its deadline and flushed |
| **P2** | Fence Path A — route through RiskEngine, or mark harness-only and refuse unattended runs | M | **done 2026-09-11**, by the first option. `tools/risk_gate.py`; `place()` refuses a live order with no `Approval`; closes recorded, never refused. Not yet exercised on a live session |
| **P3** | Fix stale gateway docstring; decide whether `SIGNAL_CREATED` gets a consumer | S | |
| **P4** | Add weekly-loss, consecutive-loss and correlation vetoes to RiskEngine | M | **done** — all three fail closed on a MISSING input. Weekly loss and correlation have no data source on the harness path and are reported `not_enforced` there |
| **P5** | Postgres-backed integration job in CI; widen FK coverage | M | highest test-integrity win |
| **P6** | Register a broker account and exercise Path B end to end on demo | M | never yet done |
| **P7** | Watchdog + restart-loop limiting | M | |
| **P8** | 24h paper soak, then 72h | L | needs P1b first |
| **P9** | Windows installer + service | L | own level, own decision |

## O. File-by-file change plan (P1b–P3 only)

| File | Change |
|---|---|
| `tools/take_profit.py` | recoverable flush entry point; refuse unattended run without a supervisor |
| `tools/run_overnight.py` | detach from console; write `CRASH_REPORTS/` on any exit |
| `tools/mt5_paper.py` | add an explicit "harness mode" banner naming what it bypasses |
| `backend/app/webhooks/gateway.py` | correct the stale docstring |
| `backend/app/risk/engine.py`, `config.py` | three new veto codes + limits |
| `.github/workflows/tests.yml` | add a `postgres:16` service job |
| new `CRASH_REPORTS/.gitkeep` | crash journal directory |

## P. Test plan

Per change: the documented gate (`tests/test_rule_backtest.py` for anything
touching order construction, wind-down or keep-awake) plus a regression test
naming the observed failure. Per level: all 7 toolkit gates, backend
`ruff`/`mypy`/`pytest`, frontend lint/types/tests/build. Before any promotion:
24h paper soak with CPU, RSS, connection count and queue depth sampled.

## Q. Current project status

See `PROJECT_STATUS.md`.

---

## Working-tree note

Uncommitted changes are present from the preceding session and were **not**
made during this audit:

- `tools/mt5_paper.py`, `tools/take_profit.py`, `tests/test_rule_backtest.py` —
  the disconnect fix and its two regression tests. All 7 gates pass.
- `backend/app/core/toolkit.py`, `tools/__init__.py`, `tools/pyproject.toml`,
  4 backend call sites — the `tr_toolkit` packaging change. `ruff`, `mypy` and
  all gates pass.
- `EMERGENCY_STABILITY_AUDIT.md`, `LOSS_ROOT_CAUSE_REPORT.md`.

Nothing is committed. They are listed here so the audit does not present the
tree as untouched.
