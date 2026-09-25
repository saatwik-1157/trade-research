# Live trading preflight

## The command

```bash
cd backend
python -m app.live.preflight              # human-readable
python -m app.live.preflight --json       # machine-readable
python -m app.live.preflight --strict     # exit 1 unless READY_FOR_LIVE
```

Exit codes: `0` the report was produced, `1` with `--strict` when the verdict is
not `READY_FOR_LIVE`, `2` when the command itself failed (which is also
reported as `NOT_READY_FOR_LIVE` — a preflight that crashes is not a preflight
that passed).

It places nothing, changes nothing and enables nothing. It reads settings,
probes the database and Redis with `app.core.health`'s own checks rather than
new ones, and reads the connected MetaTrader account read-only.

**Run it on the Windows host.** The API image is Linux and has no `MetaTrader5`
package, so from inside the container `account_readable` fails — correctly, and
uselessly.

## The checks

The count depends on configuration: 52 for a fully-populated context (seven
market-data checks per symbol set, four AI checks when the seat is enabled),
46 in the current deployment. A group never disappears -- an unsupplied
component produces failures, not omissions.

### configuration (5)
- [ ] `environment_is_production` — a live account reached from a development
      environment is the accident this check exists for
- [ ] `trading_mode_is_live`
- [ ] `live_trading_flag_set`
- [ ] `explicit_confirmation_phrase` — `ENABLE_LIVE_TRADING_CONFIRMATION` equals
      `YES_I_UNDERSTAND`, exactly. A near miss fails
- [ ] `live_gates_all_built` — every `LIVE_GATES` entry True

### account (8)
- [ ] `allowlist_configured` — **empty permits nothing**
- [ ] `account_readable` — read from the terminal, not from config
- [ ] `account_allowlisted`
- [ ] `account_matches_expected` — connected vs `LIVE_EXPECTED_ACCOUNT_ID`
- [ ] `server_matches_expected` — connected vs `LIVE_EXPECTED_SERVER`
- [ ] `account_type_is_live` — REAL, not DEMO, CONTEST or UNKNOWN
- [ ] `account_trading_permitted`
- [ ] `account_has_equity` / `account_has_free_margin`

If the account cannot be read, every identity check below it still appears in
the report, as a failure. A check that vanishes is a check nobody notices is
missing.

### venue (4)
- [ ] `broker_adapter_registered` — otherwise a routed signal stops at `no_venue`
- [ ] `broker_adapter_mode_is_live`
- [ ] `broker_connection_healthy`
- [ ] `terminal_reachable`

### market_data (7, per symbol)
- [ ] `symbols_present` · `symbols_mapped` · `quotes_available`
- [ ] `quotes_fresh` — `LIVE_QUOTE_MAX_AGE_SECONDS`, default 60s. Much tighter
      than `MARKET_DATA_STALE_SECONDS` (900s), which governs the dashboard: a
      bar fifteen minutes old is fine to look at and not fine to trade on
- [ ] `market_open` · `quotes_not_crossed` · `spread_within_limit`

### strategy (3)
- [ ] `strategy_authorised` — status approved/active/live
- [ ] `strategy_version_locked` — version id **and** fingerprint
- [ ] `strategy_has_protection` — a stop and a target. A position that cannot be
      protected must not be entered

### ai (1 or 4)
Disabled: one PASS — no opinion is not approval, and it is not a refusal either.
Enabled: `ai_model_present`, `ai_model_approved`, `ai_model_version_locked`,
`ai_fallback_defined`.

### risk (5)
- [ ] `risk_engine_healthy` · `risk_limits_configured` · `capital_limits_configured`
- [ ] `kill_switches_available` · `no_kill_switch_engaged`

`capital_limits_configured` requires all eight of: approved capital, risk budget,
max daily loss, max drawdown, max exposure, max position size, max open
positions, max daily orders. Any one unset fails and names itself.

### execution (8)
- [ ] `position_sizer_healthy` · `oms_healthy` · `position_manager_healthy`
- [ ] `no_unresolved_orders` · `no_unknown_order_states`
- [ ] `positions_reconciled` · `orders_reconciled` · `no_unexpected_positions`

### platform (10)
- [ ] `safe_mode_clear` · `recovery_available` · `monitoring_active`
- [ ] `database_healthy` · `redis_healthy` · `workers_healthy`
- [ ] `audit_logging_active` · `step_up_required`
- [ ] `notifications_healthy` *(non-blocking)*
- [ ] `release_stamped` *(non-blocking)*

## Reading the result

The last line is `READY_FOR_LIVE` or `NOT_READY_FOR_LIVE`. There is no third
value and no partial pass.

A blocker is fixed by **meeting** it. Editing the gate to stop asking is not a
fix, and `test_live_gate.py` asserts each condition blocks on its own so a check
cannot be quietly softened without a test going red.

## What the CLI cannot see

The broker registry, the OMS, the risk service, the position manager and the
safe-mode latch live inside the API process. A CLI is a different process, so
those checks report FAIL with "nothing was supplied to check".

That is the correct answer from a CLI rather than a limitation to apologise for:
the question is "is this deployment ready for live trading", and a deployment
whose execution machinery cannot be observed is not ready. An in-process caller
holding those objects builds a fuller `LiveContext` and gets a fuller answer
from the same gate.

## Current result

Run 2026-09-07 on the Windows host against the running stack:

```
  10 passed, 36 failed, 0 warning, 34 blocking

  NOT_READY_FOR_LIVE
```

The ten that passed, exactly:

    account_readable            account_trading_permitted
    account_has_equity          account_has_free_margin
    terminal_reachable          ai_seat_configured
    no_kill_switch_engaged      database_healthy
    redis_healthy               step_up_required

Two non-blocking failures (`notifications_healthy`, `release_stamped`) and 34
blockers. The connected account it read: `5055473926 @ MetaQuotes-Demo`,
**DEMO**, USD, equity 100,012.00, free margin 99,695.27, leverage 100,
trading permitted. See `LIVE_TRADING_READINESS_REPORT.md` for the itemised
blocker list.
