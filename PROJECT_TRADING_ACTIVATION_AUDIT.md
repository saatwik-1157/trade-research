# Trading activation audit

Phase 1 of Level 70. Written 2026-09-07 by inspecting the repository and
querying the running deployment. Nothing was modified to produce it.

**The headline finding, before any detail: this repository contains two
separate trading systems, and only one of them has ever sent an order.**

    tools/          a standalone MT5 research harness. SENDS ORDERS. Demo-fenced
                    in code. Running right now, pid 19224. Knows nothing about
                    the platform: no RiskEngine, no OMS, no database.

    backend/        the platform. 318 modules, 27 migrations, 69 test files, a
                    RiskEngine, an OMS, a position manager, a webhook gateway,
                    a broker adapter layer. Has never touched MetaTrader.

The brief's target architecture is largely **already built** inside `backend/`,
to a standard higher than the brief asks for. What did not exist was the wire
between the two — and one deliberate hole at the far end: there is no live
broker adapter, and the code-level fence refuses any account that is not a
demo account.

> **Update, 2026-09-07 (L70b).** Half of that wire now exists: the platform can
> register the MT5 **demo** terminal as `mt5_demo`, through the `MT5Adapter`
> that already wrapped `tools/mt5_paper`. Verified against the live terminal.
> The live half is unchanged and remains structurally blocked — §7 Blocker 1.
> Sections below are as originally written except where marked.

---

## 1. Verified runtime state

Read from the running deployment at 2026-09-07 07:50–08:05 local, not from
configuration files.

| Fact | Value | Source |
|---|---|---|
| API | healthy | `GET /health` |
| Database | healthy, schema head `0027_capital_reservations` | `GET /health/ready` |
| Redis | healthy | `GET /health/ready` |
| Workers | 4 running, in-process | `GET /health/ready` |
| Environment | `development` | `/health` |
| Trading mode | `paper` | `/health` |
| `live_trading` | `false` | `/health` |
| `live_execution_allowed` | **false** | `/health` |
| Live blockers | **12** | `/health` |
| Containers | tr-api, tr-postgres, tr-redis, tr-frontend, tr-nginx — all up | `docker ps` |
| MT5 account | 5055473926 @ MetaQuotes-Demo, **DEMO**, balance 100,019.05 USD | `tools/mt5_account.py` |
| Broker accounts in DB | **0** | `PROJECT_STATE.json` |
| Paper accounts in DB | 7 | `PROJECT_STATE.json` |
| Orders / trades in DB | 271 / 252 — all paper | `PROJECT_STATE.json` |
| Harness session | pid 19224, started 07:46:17, stops 06:00 tomorrow | `Win32_Process` |

The twelve blockers, verbatim from `/health`:

```
trading_mode is paper, not live
LIVE_TRADING is false
gate not built: risk_engine_veto
gate not built: position_sizing_refusal
gate not built: kill_switch
gate not built: order_idempotency
gate not built: unknown_status_reconciliation
gate not built: broker_sync_on_connect
gate not built: restart_reconciliation
gate not built: structured_execution_logging
gate not built: monitoring_and_alerting
gate not built: live_broker_adapter
```

Ten of those twelve are `LIVE_GATES` entries in
`backend/app/core/settings.py:47`. **Nine of the ten name mechanisms that are
built and tested.** They read `False` because the repository's own convention
is that flipping a gate is an operator decision taken in a reviewed pass, not a
side effect of the level that wrote the code — three tests assert every entry
is False so that a flip has to be deliberate and visible.

The tenth, `live_broker_adapter`, is different. Its comment says `does not
exist`, and that is accurate.

---

## 2. The existing automated trading tool — `tools/`

This is the system the brief calls the primary asset, and it is the one
actually trading.

**Entry point today.** `C:\Users\Asus\Desktop\start-trading.bat` →
`tools/run_overnight.py` → imports `tools/take_profit.py` → imports
`tools/mt5_paper.py` → `MetaTrader5` Python package → terminal64.exe.

**Language and dependencies.** Python 3, standard library plus `numpy` and the
`MetaTrader5` package. No framework, no database, no async. Logs to JSONL under
`data/`.

| Concern | Where | What it does |
|---|---|---|
| Connection | `mt5_paper.connect` | opens the terminal, path overridable |
| **Demo fence** | `mt5_paper.assert_demo` | `trade_mode != 0` → `RefuseToTrade`. CONTEST, REAL and **any unrecognised value** all refuse |
| Algo-trading check | `assert_demo(live=True)` | refuses when the terminal's Algo Trading switch is off |
| Order tagging | `MAGIC = 770315` | the tool only ever modifies or closes its own positions |
| Entry rules | `rule_sma_cross`, `rule_rsi_reversion`, `rule_random` | closed bars only; no lookahead |
| Sizing | `lot_for_risk` | risk-per-trade off the stop distance; **refuses on a missing tick value**; rounds DOWN |
| Bracket | `place(...)`, `bracket_is_sane` | SL/TP from ATR multiples, sanity-checked against the actual fill |
| Filling mode | `filling_for` | bitmask translation per symbol |
| Caps | `cycle(...)` | max positions, max daily loss (server day, realised) |
| Exit | `take_profit.harvest` | closes at net floating ≥ threshold, **swap included** |
| Ledger | `track_record.py` | merges MT5 history into `data/track_record.jsonl`, R-multiple and clustered t |
| Alerts | `tv_webhook.py` | **records only**; holds no credentials, places no orders |
| Reads | `mt5_account.py` | read-only, cannot write |

**Safety controls it already has:** demo fence in code, dry-run by default
(`--live` required), magic-number isolation, lot cap, concurrent-position cap,
daily-loss halt, refusal on missing measurements, round-down sizing, swap-aware
exits, and — as of this session — a single-instance guard and a session log in
`run_overnight.py`.

**Safety controls it does not have, and does not claim to:** no RiskEngine, no
approval object, no order state machine, no idempotency key, no unknown-state
reconciliation, no persistence beyond JSONL, no kill switch other than Ctrl+C,
no account allowlist, no audit trail, no reconnection. It is a research harness
that was never asked to be more.

**It cannot reach a real account.** `assert_demo` fails closed on anything that
is not `trade_mode == 0`.

---

## 3. The platform — `backend/`

Audited module by module. The short version: nearly every component the brief
asks for exists, is documented, and is covered by tests.

### 3.1 Broker layer — `app/brokers/`

| File | What it is | Verdict |
|---|---|---|
| `base.py` | `BrokerAdapter` ABC, 12 methods. `OrderResult.status ∈ {ACCEPTED, REJECTED, UNKNOWN}` | **KEEP** |
| `mt5.py` | `MT5Adapter` — **already wraps `tools/mt5_paper.py` and `tools/mt5_account.py` rather than reimplementing them**. Calls the toolkit's own `assert_demo`. `mode` is `demo`; `connect()` refuses anything else | **KEEP** |
| `fake.py` | `FakeBroker`, the paper venue and the fault injector (`fail_next`, `unknown_next`, `disconnect_after`) | **KEEP** |
| `shadow.py` | `ShadowBroker` — acknowledges, never fills. Same interface, no `if shadow:` in the pipeline | **KEEP** |
| `registry.py` | one adapter per account, look-up by account id only, no global default | **KEEP** |
| `reconcile.py` | compares believed vs held. **Reports, never repairs** | **KEEP** |
| `validation.py` | broker constraints; **refuses** a volume that is not a step multiple rather than rounding up | **KEEP** |

The integration mapping the brief asks for in its §4 — *existing MT5 connector →
BrokerAdapter* — **has already been done.** `mt5.py` says so in its own
docstring and names the four places `connect()` used to be duplicated.

**The gap was reachability, not existence — and it has since been closed for
demo.** When this audit was written, `backend/app/api/v1/brokers.py` read
`_ADAPTERS = {"simulator": "paper"}`: only the simulator could be registered,
which is why `accounts_broker` was 0 and why the platform had never opened a
terminal.

It now reads `{"simulator": "paper", "mt5_demo": "demo"}`. The demo venue needs
no credentials stored in the platform — the terminal is already logged in and
the adapter reads the account it finds — which is exactly why it could be added
and a live venue still cannot. See `BROKER_MT5_INTEGRATION.md`.

A live entry is still impossible for the original reason: it needs a login, a
password and a server, and a route that accepted those would be a route that
stores them.

### 3.2 Risk — `app/risk/`

`RiskEngine` (919 lines) is the final veto and the design holds:

- The only way to get an `Approval` is `RiskEngine.approve`. There is no other
  constructor path in the codebase, and `test_risk.py::test_only_the_risk_engine_can_mint_an_approval` asserts it.
- `OrderManager.submit` takes an `Approval` as its first positional argument, so
  an unapproved order **is not expressible**.
- An `Approval` binds to its order by `request_hash`; changing any bound field
  invalidates it (`test_changing_any_bound_field_invalidates_the_approval`).
- Approvals expire.
- Kill switches are checked first, in order global → account → strategy.
- **Unknown is not permission**: a check that cannot get its data vetoes
  (`test_an_unenforced_limit_is_never_read_as_a_pass`).
- The AI layer has no path to an approval (`app/execution/ai.py`: `AiVerdict`
  has no field by which a model could approve, size, or raise a limit).
- Capital reservations (migration 0027) stop two concurrent orders taking the
  same headroom.

**Verdict: KEEP, unchanged.** This is the strongest component in the repository.

### 3.3 Sizing — `app/sizing/`

Deterministic, no clock, no I/O, no model. Missing measurement refuses. Rounds
down. Inherits its rules from `tools/mt5_paper.lot_for_risk` and says so.
**KEEP.**

### 3.4 OMS — `app/oms/`

Twelve-state machine (`state.py`), one `OrderManager` for every mode
(`service.py`, 1083 lines). Five rules, each tested:

1. No order without an `Approval`.
2. State durable **before** the venue call (`submitting` on disk).
3. Uncertain outcome → `unknown`, and **nothing retries it**. `can_resend` is
   False for `unknown` and `submitting`.
4. One intent → one order, ever. A repeat returns the existing order marked
   `duplicate`.
5. A requested quantity is never a filled quantity.

`test_oms.py` covers duplicate TradingView alerts, partial fills, overfill
refusal, repeated execution reports, refused cancels, and
`failed` vs `rejected` vs `unknown`. **KEEP.**

### 3.5 Execution — `app/execution/`

`ExecutionPipeline` (925 lines) runs signal → strategy → AI seat → risk →
sizing → risk binds → OMS → adapter. It holds no adapter and imports none; a
test asserts that by parsing every module in the package. Every stage fails
closed. One `execution_id` per attempt.

`ExecutionWorker` claims `signals` rows in the same transaction that reads them,
so two workers cannot both pick one up. Nothing is replayed on restart.

`EXECUTION_WORKER_ENABLED=true` is set in `.env` — the worker is running now,
in paper mode. **KEEP.**

### 3.6 Positions — `app/positions/`

`PositionManager` marks closed only on a confirmed outcome carrying a fill; a
REJECTED close leaves the position open; an UNKNOWN close parks it and it is
never acted on again without reconciliation. `reconciler.py` applies exactly two
corrections it can justify and leaves everything else for a person. **KEEP.**

### 3.7 Webhooks — `app/webhooks/`

`WebhookGateway`: body cap → parse → authenticate → validate → age → idempotency
→ symbol → strategy → `Signal` → event. Empty secret **refuses every alert**.
The gateway writes a row and publishes an event; it cannot trade.

**Doc drift found:** the module docstring still says "the risk engine (L17),
sizing (L18) and the OMS (L19) are not built". They are. **KEEP + MODIFY**
(comment only).

### 3.8 Recovery and safe mode — `app/recovery/`

`SafeMode` is a latch that always carries its reasons — never closed without a
`SafeModeReason` and a detail. It blocks new orders, new bot starts and
automated execution, and deliberately does **not** block monitoring,
reconciliation, position visibility or diagnostics, because reconciling is how
you get out. It is a *second* refusal and never releases anything.

`RecoveryManager.run_startup` walks disconnect → stop → alert → reconnect →
reconcile → validate → resume-only-if-safe, and any step needing a person closes
the latch. `RECOVERY_STARTUP_CHECKS=true`. **KEEP.**

### 3.9 Bots — `app/bots/`

`BotSupervisor` treats a heartbeat, not a database row, as evidence a bot is
alive. Restart is refused unless no kill switch is engaged, the bot is not
disabled, the account has no unresolved order, and the run is `crashed` rather
than `halted`. Nothing in the package imports `app.oms` or `app.brokers`.
**KEEP.**

### 3.10 Security — `app/security/`, `app/auth/`

Session cookies, CSRF double-submit, rate limiting, RBAC, admin audit,
security headers, posture reporting, and **step-up re-authentication**:
scoped, 300s, single-use, bound to the session. `STEP_UP_REQUIRED=true`.
CORS refuses `*`, `null`, or any entry carrying a path — **at startup**.

Honest gap, stated in the code itself: **there is no MFA/TOTP.** `stepup.py`
says calling it MFA would be security theatre. **KEEP.**

### 3.11 The rest

Monitoring (12 modules, worker running), notifications (8 modules; a test
parses the package to prove it cannot trade), journal, portfolio, analytics,
symbols with mapping, market data, AI (18 modules with a model registry and a
lifecycle), datasets, training, validation, replay, review, admin, realtime hub.
All **KEEP**.

---

## 4. Component classification

| Component | Where | Class | Note |
|---|---|---|---|
| MT5 order sending | `tools/mt5_paper.py` | **KEEP** | the only code that has ever sent an order |
| Harvest loop | `tools/take_profit.py` | **KEEP** | |
| Overnight launcher | `tools/run_overnight.py` | **KEEP + MODIFY** | guard + log added this session |
| Desktop launcher | `Desktop/start-trading.bat` | **KEEP** | unchanged; inherits both |
| Ledger | `tools/track_record.py` | **KEEP** | |
| TV alert receiver (CLI) | `tools/tv_webhook.py` | **KEEP** | records only; **not** a duplicate of the gateway — different trust model, documented in both |
| `BrokerAdapter` ABC | `app/brokers/base.py` | **KEEP** | |
| `MT5Adapter` | `app/brokers/mt5.py` | **KEEP** | already wraps the toolkit |
| `FakeBroker` / `ShadowBroker` | `app/brokers/` | **KEEP** | |
| `BrokerRegistry` | `app/brokers/registry.py` | **KEEP** | |
| Reconciliation | `app/brokers/reconcile.py`, `app/positions/reconciler.py` | **KEEP** | |
| `RiskEngine` | `app/risk/engine.py` | **KEEP** | do not touch |
| Sizing | `app/sizing/` | **KEEP** | |
| OMS | `app/oms/` | **KEEP** | |
| `ExecutionPipeline` / worker | `app/execution/` | **KEEP** | |
| `PositionManager` | `app/positions/manager.py` | **KEEP** | |
| Webhook gateway | `app/webhooks/gateway.py` | **KEEP + MODIFY** | stale docstring |
| Safe mode / recovery | `app/recovery/` | **KEEP** | |
| Bot supervisor | `app/bots/` | **KEEP** | |
| Step-up auth | `app/security/stepup.py` | **KEEP** | |
| Monitoring / notifications | `app/monitoring/`, `app/notifications/` | **KEEP** | |
| Adapter registration route | `app/api/v1/brokers.py` | **KEEP + MODIFY** | `mt5_demo` added; live still impossible without a credential seat |
| **LiveTradingGate** | — | **ADD** | does not exist |
| **Live account allowlist** | — | **ADD** | does not exist |
| **Live activation state machine** | — | **ADD** | does not exist |
| **Live preflight command** | — | **ADD** | only a per-bot preflight exists (`/v1/bots/{id}/preflight`) |
| **Live broker adapter** | — | **NOT ADDED** | see §7 |
| **Broker credential storage** | — | **NOT ADDED** | see §7 |

**Nothing is classified REMOVE.** No duplicate functionality was found that
warrants deletion. The two apparent duplicates are both deliberate and both
documented:

- `tools/tv_webhook.py` vs `app/webhooks/gateway.py` — a CLI an operator is
  watching may run unauthenticated; a server endpoint may not. The settings
  comment states the difference.
- `app/paper/oms.py` vs `app/oms/service.py` — the paper binding imports the
  *same* state machine and fill accounting from `app.oms`. One machine, two
  bindings.

---

## 5. Answers to the thirteen questions

**1. What automated trading functionality already exists.** A complete,
demo-fenced MT5 trading loop in `tools/`, currently running. A complete
paper-trading platform in `backend/` with risk, sizing, OMS, position
management, reconciliation, recovery, monitoring and a TradingView gateway —
which has executed 271 orders and 252 trades, all simulated.

**2. How it currently works.** `start-trading.bat` starts
`run_overnight.py`, which computes minutes to 06:00 and calls `take_profit.main`
with a fixed settings block: `random` rule, 7 FX majors, $5 risk per trade,
SL=TP=1.5×ATR, harvest at $0.50, max 7 positions, max daily loss $200, one pass
every 20s. Each pass harvests positions in profit, then opens new ones.

**3. How it connects to MT5/broker.** `MetaTrader5` Python package →
terminal64.exe → MetaQuotes-Demo. `assert_demo` gates every run. The platform
connects to MT5 **not at all** today.

**4. How strategies generate trades.** Two paths. In `tools/`, a rule function
reads closed bars and returns buy/sell/None. In the platform, either a
`Strategy` inside `PaperEngine` driving bars, or an external `Signal` (a
TradingView alert via the gateway) picked up by `ExecutionWorker`. The two share
every downstream gate.

**5. How risk is handled.** In `tools/`: hard caps only. In the platform:
`RiskEngine` as sole minter of `Approval`, with kill switches, latching locks,
capital reservations, and unknown-vetoes-rather-than-passes.

**6. How orders are executed.** In `tools/`: `mt5.order_send` directly. In the
platform: `ExecutionPipeline` → `OrderManager.submit(approval, ...)` →
`BrokerAdapter.place_order`. Nothing else may reach a venue.

**7. What safety controls already exist.** Listed in §2 and §3. In summary:
mode fail-closed defaults, a code-level demo fence, ten unbuilt live gates, an
approval-bound OMS, unknown-state parking with no retry, idempotent intents,
kill switches at three scopes, a reasoned safe-mode latch, startup
reconciliation, step-up re-auth on dangerous actions, CORS refusal at startup,
and a webhook that refuses everything without a secret.

**8. What is missing.** For controlled live activation: a `LiveTradingGate`,
a live account allowlist, an activation state machine, a live preflight command,
a live session record — and, at the far end, a live broker adapter and a place
to keep credentials. Also missing platform-wide: MFA/TOTP.

**9. What must be modified.** Only additive wiring plus one stale docstring.
No existing trading logic needs changing to support controlled activation.

**10. What can be reused unchanged.** Everything in §4 marked KEEP — which is
all of the trading path.

**11. What must NOT be changed.** `tools/mt5_paper.assert_demo`;
`RiskEngine.approve` as the sole approval constructor; `OrderManager.submit`'s
approval argument; the `unknown` state's lack of a retry edge; the `LIVE_GATES`
values; `_ADAPTERS = {"simulator": "paper"}` until credentials exist.

**12. PAPER, DEMO or LIVE capable.**

- **PAPER — capable and in use.** The platform is running paper now.
- **DEMO — split.** `tools/` is demo-capable and trading. The *platform* is
  not: `MT5Adapter` exists but nothing can register it, so the platform has no
  demo path today.
- **LIVE — not capable, by construction.** Four independent layers refuse:
  `TRADING_MODE`/`LIVE_TRADING`, the ten `LIVE_GATES`, `_ADAPTERS` having no
  live entry, and `assert_demo` refusing any non-demo account in code.

**13. Exact blockers preventing safe live trading.** §7.

---

## 6. Security and trading-safety concerns found

| # | Concern | Severity | Detail |
|---|---|---|---|
| 1 | **No live broker adapter** | blocker | `LIVE_GATES["live_broker_adapter"]` — accurate |
| 2 | **No broker credential storage** | blocker | no encrypted store; the registration route says so |
| 3 | **No MFA** | high | `stepup.py` and `SECURITY.md` both state it; step-up is password re-entry |
| 4 | Safe-mode latch is per process | medium | one API process today; a second needs a shared latch (a row and a lease) |
| 5 | `tools/` bypasses the platform entirely | medium | the running harness has no kill switch reachable from the UI, no audit trail, and its positions are invisible to `PositionManager` |
| 6 | Release not stamped | low | `/health` reports `"stamped": false`; a release you cannot identify is one you cannot roll back to |
| 7 | Stale docstring in the webhook gateway | low | claims risk/sizing/OMS are not built |
| 8 | 150+ documents uncommitted | low | the working tree carries most of the repository's documentation as untracked files |

**No secret was found committed.** `.env` is gitignored and holds one
non-secret flag; `.env.example` carries `<configured-secret>` as a placeholder;
`TV_WEBHOOK_SECRET` is documented as shell-exported, never written to a file.

---

## 7. Live-trading blockers

Reported in the brief's §52 format. These are the reasons Level 70 cannot end
in `READY_FOR_LIVE`.

**BLOCKER 1 — there is no live broker adapter.**
REASON: `MT5Adapter.connect()` refuses any account that is not demo, and calls
`tools/mt5_paper.assert_demo`, which refuses `trade_mode != 0` in code.
`_ADAPTERS` has no live entry.
AFFECTED: `app/brokers/mt5.py`, `app/api/v1/brokers.py`.
RISK: none today — this is the fence working.
FIX: a separate, deliberate piece of work with its own review. It is not part
of an activation layer, and building it during this level would be exactly the
"weaken the gate" the brief forbids.

**BLOCKER 2 — there is nowhere to keep broker credentials.**
REASON: the platform stores none by design; `registry.py` says credentials are
a later seat.
AFFECTED: `app/brokers/registry.py`, `app/api/v1/brokers.py`.
RISK: a live account cannot be authenticated without them; adding them
carelessly is how credentials reach a log.
FIX: encrypted credential storage with its own audit trail. Separate work.

**BLOCKER 3 — no live account exists to allowlist.**
REASON: the only MT5 account on this machine is 5055473926 @ MetaQuotes-Demo,
`trade_mode == 0`.
AFFECTED: operations.
RISK: none.
FIX: opening and funding a real account is an operator decision.

**BLOCKER 4 — ten `LIVE_GATES` entries are False.**
REASON: repository convention — a gate is flipped in a reviewed pass, never as
a side effect. Nine name mechanisms that exist and are tested.
AFFECTED: `app/core/settings.py:47`.
RISK: flipping them without review would assert that live execution may rely on
mechanisms nobody re-verified.
FIX: one reviewed pass, per gate, with the evidence for each.

**BLOCKER 5 — no MFA on the session that would authorize activation.**
REASON: stated in `stepup.py`.
AFFECTED: `app/security/`.
RISK: a stolen cookie plus a known password reaches every dangerous action.
FIX: TOTP enrolment, recovery codes, device binding. Named as next in
`SECURITY.md`.

**BLOCKER 6 — the platform has no demo path. RESOLVED 2026-09-07.**
REASON: `_ADAPTERS` was simulator-only.
FIX APPLIED: `_ADAPTERS["mt5_demo"] = "demo"` constructs the existing
`MT5Adapter`, behind the same mode fence, step-up and audit as the simulator,
and refuses on `assert_demo`, on an unreachable terminal, and on an account the
caller did not name. Verified against the live terminal: account 5055473926 @
MetaQuotes-Demo, 12,412 symbols, health usable at 1.1ms.
REMAINING CONSTRAINT: **Windows only.** The `MetaTrader5` package does not exist
for Linux, so the API container returns 503 and registers nothing; the API
process must run on the host that holds the terminal.

**BLOCKER 7 — the running harness is outside every platform control.**
REASON: `tools/` talks to MT5 directly.
AFFECTED: operations.
RISK: today, bounded — it is demo money and it is fenced. But no platform kill
switch stops it, and `PositionManager` cannot see its positions.
FIX: route the harness's entries through `ExecutionPipeline` once the platform
has a demo venue. That is the integration the brief describes, and Blocker 6 is
its prerequisite.

---

## 8. Recommended integration path

Ordered, and deliberately stopping short of live.

1. ~~**Add the activation layer**~~ — `LiveTradingGate`, account allowlist,
   activation state machine, preflight command. All refusal machinery: it can
   only block, never permit. **DONE (L70).**
2. ~~**Give the platform a demo venue.**~~ `_ADAPTERS["mt5_demo"]` constructs
   the existing `MT5Adapter`, still behind step-up, still refusing any mode
   mismatch, still fenced by `assert_demo`. **DONE (L70b)**, verified against
   the live terminal.
3. **Run the harness's strategy through the platform** against that demo venue.
   Compare the two ledgers. This is what proves the pipeline works end to end.
4. **Flip the nine built `LIVE_GATES`**, one reviewed pass, evidence per gate.
5. **Build credential storage.** Encrypted, audited, never logged.
6. **Build the live adapter.** Its own level, its own review.
7. **Add MFA.**
8. Only then: allowlist a real account and run the first-live procedure.

Steps 2 and 3 are where the value is. Step 8 is a long way past where this
repository's own evidence supports putting money — `CLAUDE.md` records that no
rule tested here separates from a coin flip, and that cost drag is the only
effect large enough to measure.

---

## 9. What this audit did not do

- Did not run the backend test suite (~20 minutes). Test *coverage* was read
  from the files; test *results* are not claimed here.
- Did not modify any trading logic.
- Did not touch the running session (pid 19224), and did not place an order.
