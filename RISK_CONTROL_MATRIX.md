# Risk control matrix

Every control, where it lives, what it refuses, and what proves it.

## Authority order

```
  HARD SAFETY        assert_demo (code fence), LIVE_GATES, settings validators
        v
  LIVE GATE          may live execution be turned on
        v
  SAFE MODE          a latched second refusal, never a permission
        v
  RISK ENGINE        the final veto on every order
        v
  KILL SWITCHES      checked first inside the engine, global -> account -> strategy
        v
  PORTFOLIO / STRATEGY / BOT limits    combine by MOST RESTRICTIVE
        v
  POSITION SIZING    proposes a quantity; approves nothing
        v
  OMS                the only path to a venue, and only with an Approval
```

No lower row can override a higher one. The combination rule is *most
restrictive wins*, not *most specific wins* — a strategy layer cannot loosen an
account layer, and a bot cannot loosen its account's.

## The controls

| Control | Where | Refuses | Proof |
|---|---|---|---|
| Demo fence | `tools/mt5_paper.assert_demo` | any account with `trade_mode != 0`, unknown values included | called on connect by `MT5Adapter` |
| Mode validator | `core/settings.py` | `TRADING_MODE=live` without `LIVE_TRADING=true` — **refuses to start** | `test_settings.py::test_live_mode_without_flag_refuses_to_start` |
| Live gates | `LIVE_GATES` | live execution while any entry is False | `test_settings.py::test_no_live_gate_is_built_at_this_level` |
| CORS validator | `core/settings.py` | `*`, `null`, or an origin carrying a path — at startup | `test_cors.py` |
| Live gate | `app/live/gate.py` | activation on any mandatory check that is not PASS | `test_live_gate.py` (82 tests) |
| Account allowlist | `app/live/allowlist.py` | any account not explicitly listed; empty permits none | `test_live_gate.py::test_an_empty_allowlist_permits_nothing` |
| Activation machine | `app/live/state.py` | `DISABLED -> ACTIVE`, arming on a failed report, unsigned transitions | `test_live_gate.py::test_disabled_cannot_jump_anywhere_but_precheck` |
| Safe mode | `app/recovery/safe_mode.py` | new orders, new bot starts, automated execution — with reasons | `test_recovery.py` |
| Kill switch (global) | `app/risk/engine.py` | every order, first check, cannot be argued with | `test_risk.py::test_a_kill_switch_survives_a_restart` |
| Kill switch (account) | same | every order on that account | same |
| Kill switch (strategy) | same | every order from that strategy | same |
| Daily loss lock | `app/risk/state.py` | latches for the trading day; UTC-explicit | `test_risk.py::test_a_breached_daily_loss_latches_a_lock` |
| Drawdown lock | same | latches and **does not clear on its own** | `test_risk.py::test_a_drawdown_lock_does_not_clear_on_its_own` |
| Emergency stop | same | cannot be displaced by a weaker lock | `test_risk.py::test_a_weaker_lock_cannot_displace_an_emergency_stop` |
| Exposure / concentration / correlation | `app/risk/engine.py` | orders breaching portfolio limits | `test_risk.py`, `test_portfolio_risk.py` |
| Margin / leverage | `app/risk/engine.py` | orders breaching margin limits | `test_risk.py` |
| Spread / slippage / liquidity | `app/risk/engine.py` | orders in conditions outside limits | `test_risk.py` |
| Signal freshness | `app/risk/engine.py` | a stale signal, with the limit derived from the timeframe | `test_risk.py::test_a_signal_freshness_limit_is_derived_from_the_timeframe` |
| Market-open check | `app/risk/engine.py` | **an unknown market status vetoes rather than assuming open** | `test_risk.py::test_an_unknown_market_status_vetoes_rather_than_assuming_open` |
| Cooldown / frequency | `app/risk/engine.py` | a runaway strategy | `test_risk.py::test_a_runaway_strategy_is_stopped_by_the_frequency_limit` |
| Capital reservation | `app/risk/reservations.py`, migration 0027 | two concurrent orders taking the same headroom | `test_risk.py::test_two_concurrent_orders_cannot_both_take_the_same_headroom` |
| Approval binding | `app/risk/engine.py` | an approval used for a different order, or after any bound field changed | `test_risk.py::test_changing_any_bound_field_invalidates_the_approval` |
| Approval expiry | same | a stale approval | `test_risk.py::test_an_expired_approval_is_refused` |
| Sizing refusal | `app/sizing/calculator.py` | a size built from a missing measurement; rounds **down**, never up | `test_sizing.py` |
| Broker constraints | `app/brokers/validation.py` | a volume that is not a step multiple — refuses rather than rounding up | `test_brokers.py` |
| Order idempotency | `app/oms/service.py` | a second order for the same intent | `test_oms.py::test_a_duplicate_tradingview_alert_creates_no_second_broker_order` |
| Unknown-state parking | `app/oms/service.py` | any retry out of `unknown` | `test_oms.py` |
| Overfill refusal | `app/oms/service.py` | a fill exceeding the requested quantity | `test_oms.py::test_an_overfill_is_refused_rather_than_truncated` |
| Position close confirmation | `app/positions/manager.py` | marking closed without a confirmed fill | `test_positions.py` |
| Webhook auth | `app/webhooks/gateway.py` | every alert when the secret is unset | `test_webhooks.py` |
| Webhook replay | same | a duplicate or out-of-window alert | `test_webhooks.py` |
| Step-up re-auth | `app/security/stepup.py` | dangerous actions on a cookie alone; scoped, 300s, single-use | `test_security.py` |
| Bot restart gate | `app/bots/supervisor.py` | restart while a kill switch is on, or an order is unresolved | `test_bots.py` |

## The capital rule

> $10,000 approved risk budget must never become $15,000 automatically.

Four independent reasons it cannot:

1. **Most-restrictive combination.** No layer can loosen the one above it.
2. **The AI seat's type has no field for it.** Not rejected — inexpressible.
3. **Optimisation allocates within the budget.** It has no path to the budget,
   which is changed only through `RiskService.set_limits`: authenticated,
   role-gated, audited, version-bumped.
4. **The live gate reads `CapitalLimits` and has no setter.**

Tests: `test_capital.py`, `test_risk.py::test_a_strategy_cannot_loosen_an_account_limit`,
`::test_a_bot_cannot_loosen_its_accounts_limits`,
`::test_an_unenforced_limit_is_never_read_as_a_pass`.

## Unknown is not permission

The rule appears in three places, deliberately identically:

- `RiskEngine` — a check that cannot get its data vetoes.
- `LiveTradingGate` — an unsupplied field FAILS, and an empty report is not ready.
- `assert_demo` — an unrecognised `trade_mode` refuses along with the known bad ones.

An unenforced limit must not let an order through, and a gate that did not run
must not read as one that passed.
