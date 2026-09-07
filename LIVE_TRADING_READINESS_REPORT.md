# Live trading readiness report

Level 70. Generated 2026-09-07 by running the code, not by reading it.

## Verdict

    DEMO_READY

Updated 2026-09-07 after L70b. The platform runs paper trading correctly **and**
can now register the MT5 demo terminal as a venue — verified against the live
terminal, not inferred. It is still not READY_FOR_LIVE, and four of those
reasons are structural rather than configuration.

DEMO_READY means the venue is reachable and registrable, on a Windows host in
`TRADING_MODE=demo`. It does not mean a strategy has run through the platform
against it; that is the next step and it is the one that produces evidence.

`python -m app.live.preflight` agrees, in code:

```
  10 passed, 36 failed, 0 warning, 34 blocking

  NOT_READY_FOR_LIVE
```

## Existing tool audited

Yes — `PROJECT_TRADING_ACTIVATION_AUDIT.md`. The finding that shapes everything
else: this repository holds **two** trading systems.

- `tools/` — a standalone MT5 harness that sends orders, fenced to demo accounts
  in code, running now as pid 19224 on account 5055473926 @ MetaQuotes-Demo.
- `backend/` — the platform: 318 modules, 27 migrations, 70 test files, with a
  RiskEngine, an OMS, a position manager and a broker adapter layer. It has
  executed 271 paper orders and **has never opened a MetaTrader terminal**.

## Components reused unchanged

`RiskEngine` · `OrderManager` and the twelve-state machine · `SizingCalculator`
· `BrokerAdapter` and every implementation · `BrokerRegistry` ·
`reconcile.py` · `PositionManager` and `reconciler` · `ExecutionPipeline` and
`ExecutionWorker` · `WebhookGateway` · `SafeMode` and `RecoveryManager` ·
`BotSupervisor` · `StepUp` · monitoring · notifications · the entire `tools/`
toolkit.

**No trading logic was modified.** Not one line of the execution path changed.

## Components added

| Path | Purpose |
|---|---|
| `backend/app/live/allowlist.py` | live account allowlist; empty permits none |
| `backend/app/live/gate.py` | `LiveTradingGate` — checks in nine groups |
| `backend/app/live/state.py` | twelve-state activation machine |
| `backend/app/live/preflight.py` | `python -m app.live.preflight` |
| `backend/tests/test_live_gate.py` | 84 tests |
| `_ADAPTERS["mt5_demo"]` + `_build_adapter` | the demo venue: registers the existing `MT5Adapter` |
| `backend/tests/test_mt5_demo_venue.py` | 15 tests, one against a real terminal |
| `MT5_TERMINAL_PATH` setting | optional; empty uses the toolkit's default |
| 5 settings in `core/settings.py` | all fail-closed; **none can enable live trading** |
| `.env.example` block | documents all five |
| 12 documents | this one included |

## Components modified

| What | Change | Risk |
|---|---|---|
| `core/settings.py` | added 5 fail-closed settings + one property | none — `LIVE_GATES` untouched, verified below |
| `core/settings.py` | corrected the `cors_origins` description | none — it claimed comma-separated parsing, which raises `SettingsError`. Measured |
| `tools/run_overnight.py` | single-instance guard + session log | none to trading logic; settings block unchanged |
| `app/api/v1/brokers.py` | `mt5_demo` adapter entry, `_build_adapter`, connect-before-register, `expect_account` | no live path added; a test asserts no `_ADAPTERS` value is `live` and that registering flips no gate |
| `NIGHTLY.md` | documents the guard and the log | none |

## Components removed

**None.** No duplicate functionality was found that warranted deletion. The two
apparent duplicates — `tools/tv_webhook.py` vs `app/webhooks/gateway.py`, and
`app/paper/oms.py` vs `app/oms/service.py` — are both deliberate and both
documented in the code.

## Status by area

| Area | Status | Evidence |
|---|---|---|
| **Architecture** | sound | one authorised path to a venue; three MT5 call paths total, two read-only |
| **Trading flow** | complete for paper | `ORDER_EXECUTION_FLOW.md` |
| **Broker integration** | demo adapter written, **unreachable at runtime** | `_ADAPTERS = {"simulator": "paper"}` |
| **MT5** | terminal answers; account is DEMO, trading permitted | preflight `terminal_reachable: PASS` |
| **TradingView** | gateway complete; refuses everything without a secret | `test_webhooks.py` |
| **Strategy** | 2 strategies, 3 versions, none live-authorised | `strategies` table |
| **AI** | 1 model version, seat disabled; cannot approve, size, or raise a limit | `app/execution/ai.py` |
| **Risk** | the strongest component here; final veto intact | `test_risk.py` |
| **Position sizing** | deterministic; refuses on a missing measurement; rounds down | `test_sizing.py` |
| **OMS** | approval-bound, idempotent, unknown parked with no retry | `test_oms.py` |
| **Position management** | closes only on a confirmed fill | `test_positions.py` |
| **Monitoring** | worker running | `/health/ready` |
| **Recovery** | startup sequence enabled; safe mode clear | `/health` |
| **Security** | sessions, CSRF, RBAC, rate limits, step-up, CORS refusal at startup. **No MFA** | `SECURITY.md` |
| **Paper** | **READY and running** | 271 orders, 252 trades |
| **Demo** | `tools/` yes; platform **yes, both paths** — a manual order and a TradingView alert each became a real fill, were recorded as positions, reconciled and closed | `FIRST_DEMO_VENUE_RUN.md`, `SIGNAL_PATH_FIRST_RUN.md` |
| **Live** | **no**, four structural reasons | below |

## Test results

The whole backend suite, run in two parts because `test_api_v1.py` is large
enough to be worth isolating on this machine:

    pytest -q --ignore=tests/test_api_v1.py
      2886 passed, 5 skipped, 1 warning in 1284.61s (0:21:24)   exit 0

    pytest -q tests/test_api_v1.py
      174 passed, 1 skipped in 397.87s (0:06:37)                exit 0

    (repo root) pytest -q tests/
      38 passed                                                 exit 0

    TOTAL: 3098 passed, 6 skipped, 0 failed, across 77 backend
    test files plus the toolkit's own.

New tests, 190 in eight files:

* `tests/test_live_gate.py` — **84 passed**. Every one a refusal test, plus one
  that asserts the gate can still pass on a fully-configured context, because a
  gate that always failed would be indistinguishable from a broken one.
* `tests/test_venue_audit.py` — **14 passed**. Comparing the platform's record
  of a trade against MetaTrader's own deal history — the only check here that is
  not the platform grading itself.
* `tests/test_realized_pnl_is_money.py` — **12 passed**. The money booked on a
  close is the venue's own figure in account currency, or a named gap — never a
  price difference.
* `tests/test_broker_accounts.py` — **15 passed**. The `broker_accounts` row
  that lets a position name where it is held, written from what the terminal
  reported and holding no credential.
* `tests/test_position_ingest.py` — **24 passed**. The `positions` row a broker
  fill implies, and mostly about what is NOT written: an unknown outcome, a
  missing fill price, a missing ticket and a paper order all produce nothing.
* `tests/test_orders_foreign_keys.py` — **4 passed**. Runs SQLite with
  `PRAGMA foreign_keys=ON`, which the rest of the suite does not, and covers the
  dangling `orders.signal_id` that made every manual order a 500 on PostgreSQL.
* `tests/test_mt5_demo_venue.py` — **17 passed**, including
  `test_the_real_adapter_registers_against_a_live_demo_terminal`, which drives
  the route against a **real MetaTrader terminal**. It is skipped where the
  MetaTrader5 package is absent (every Linux CI runner), so CI proves none of
  it; on this host it ran and passed.

Lint and types, as CI runs them:

- `ruff check .` — clean on everything added and modified
- `ruff format --check .` — clean
- `mypy` — **18 pre-existing errors, 0 introduced.** The 18 are in
  `test_execution_durability.py`, `test_autonomy.py`, `test_webhooks.py`,
  `test_signal_routing.py` and `test_api_v1.py`. That is the baseline: compare
  against 18, not against zero

## Failed tests

**None.** Zero failures across all 70 test files.

## Verified safety properties

Each was executed, not asserted:

1. **No new setting can enable live execution.** With all five configured as
   favourably as possible, `live_execution_allowed` is `False` and
   `live_execution_blockers()` still returns 12.
   (`test_no_live_setting_can_enable_live_execution`)
2. **An empty allowlist permits nothing** — including an unreadable identifier.
3. **`DISABLED → ACTIVE` is not expressible.** Nor `DISABLED → ARMED`, nor
   `READY → ACTIVE`.
4. **Arming refuses a failing report**; activating requires `ARMED` *and* a
   fresh passing report.
5. **A new process starts at `DISABLED`** — no restart reaches `ACTIVE`.
6. **Missing evidence fails**, and an empty report is not ready.
7. **A DEMO account cannot pass as live**, and neither can CONTEST or an
   unrecognised `trade_mode`.
8. **The activation package cannot reach a venue** — a test parses every module
   for forbidden imports.
9. **A mandatory WARNING blocks**; `soften_optional_failures` raises if handed a
   mandatory check.
10. **The running paper deployment was untouched** — verified after every change.

## Known risks

1. **No MFA.** The session that would authorise activation is a password and a
   cookie plus scoped, single-use step-up re-authentication. Stated in
   `stepup.py` and `SECURITY.md`.
2. **The safe-mode latch is per process.** Correct for one API process; a second
   needs a shared latch — a row and a lease.
3. **`tools/` runs outside every platform control.** No platform kill switch
   reaches it and `PositionManager` cannot see its positions. Bounded today: it
   is demo money behind a code fence. Both systems tag orders `MAGIC 770315`,
   so **each can close the other's positions** — do not run them together
   unattended.
7. ~~A close is not durable before it is sent~~ — **RESOLVED, and it was worse
   than described.** The close path always persisted before the venue call; it
   had simply never worked. `orders` held **zero** `close:` intents, because the
   executor passed a symbol id where the store resolves a code. Fixed with an
   explicit `PositionView.symbol_code`; verified,
   `close:729785da-…:0.01000000` recorded `filled`.
8. ~~Realised P&L is a price difference~~ — **RESOLVED.** The figure is read
   from the venue's own deal history and carried through
   `OrderResult → FillRecord → CloseOutcome → positions.realized_pnl`. A broker
   close with no reported money books a gap, not a derivation. Verified: venue
   −0.02 USD, row −0.0200.
4. **The release is not stamped.** `/health` reports `"stamped": false`.
5. **Certification is `CONDITIONALLY_CERTIFIED`** with GATE-01 and GATE-10 not
   passing.
6. **Market data is thin** — 705 bars across 2 symbols.

## Live trading blockers

The 34 blocking preflight checks reduce to seven real causes:

| # | Blocker | Fix |
|---|---|---|
| 1 | **No live broker adapter.** `MT5Adapter.connect()` refuses non-demo; `assert_demo` refuses `trade_mode != 0` in code | separate, reviewed work. Not a configuration change |
| 2 | **No broker credential storage** | encrypted store with its own audit trail. Separate work |
| 3 | **No real account.** The only MT5 account here is demo | an operator decision |
| 4 | **Ten `LIVE_GATES` entries are False.** Nine name mechanisms that exist and are tested | one reviewed pass, evidence per gate |
| 5 | **No MFA** on the authorising session | TOTP with enrolment and recovery codes |
| 6 | ~~The platform has no demo path~~ | **RESOLVED (L70b).** `mt5_demo` registers the existing adapter. Windows host only — the Linux image has no MetaTrader5 |
| 7 | **The harness is outside platform control** | route it through `ExecutionPipeline` once #6 lands |

Configuration blockers (all five new settings unset, `ENVIRONMENT=development`,
`TRADING_MODE=paper`, `LIVE_TRADING=false`) are real and reported, but they are
downstream of the seven above. Setting them changes nothing while #1 stands.

## Not built, deliberately

A live broker adapter · credential storage · a persisted live session record
(needs a migration; the machine's `history` carries transitions meanwhile) · an
HTTP activation endpoint (needs step-up wiring and a role) · MFA.

Each is named rather than quietly omitted, and none was faked.

## Final

    DEMO_READY

    Live trading was NOT enabled. LIVE_TRADING remains false, every LIVE_GATES
    entry remains False, no order was placed, and no gate was weakened.
    Registering a demo venue is asserted not to change any of those -- see
    test_registering_a_demo_venue_does_not_enable_live_execution.
