# Live trading safety

The invariants, where each is enforced, and which test proves it.

## The rule underneath all of them

**Unknown is not permission.**

`RiskEngine` runs on it — a check that cannot get its data vetoes, because a
limit that cannot be evaluated is a limit that is not enforced. `LiveTradingGate`
runs on it too: every `LiveContext` field defaults to `None`, and `None` is a
FAIL with a detail naming what was missing. A gate that reported "not checked"
for what it could not reach would go green on a machine where nothing was
connected, which is exactly the machine on which a green preflight is most
dangerous.

## The eighteen invariants

| # | Invariant | Enforced by | Test |
|---|---|---|---|
| 1 | RiskEngine has final veto | `Approval` is constructible only by `RiskEngine.approve`; `OrderManager.submit` takes one positionally | `test_risk.py::test_only_the_risk_engine_can_mint_an_approval`, `::test_the_oms_cannot_be_reached_without_an_approval` |
| 2 | AI cannot bypass RiskEngine | `AiVerdict` has no field expressing an approval, a size or a limit | `test_ai.py`, `app/execution/ai.py` |
| 3 | Strategy cannot bypass RiskEngine | the pipeline holds no adapter and imports none | `test_execution.py` (package parse) |
| 4 | TradingView cannot execute MT5 | the gateway writes a `Signal` row and publishes an event; it holds no adapter | `test_webhooks.py` |
| 5 | UI cannot execute MT5 | execution runs in `ExecutionWorker`, not in a request | `test_workers.py` |
| 6 | Research cannot execute MT5 | `tools/` is a separate process with no platform path; `app/research` imports no adapter | audit §4 |
| 7 | Recovery cannot execute MT5 | `app/recovery` engages a latch; it approves nothing | `test_recovery.py` |
| 8 | Unknown broker state requires reconciliation | `can_resend` is False for `unknown` and `submitting`; `unknown` has no retry edge | `test_oms.py::test_an_unresolved_order_cannot_be_cancelled` |
| 9 | Stale critical data blocks new entries | gate `quotes_fresh`, and the RiskEngine's signal-age limit | `test_live_gate.py::test_a_stale_quote_blocks_new_entries` |
| 10 | Wrong account blocks trading | gate `account_matches_expected`, `account_allowlisted` | `test_live_gate.py::test_each_condition_blocks_on_its_own` |
| 11 | Paper mode cannot trade live | gate `trading_mode_is_live`; `_ADAPTERS` has no live entry | `test_live_gate.py`, `test_settings.py` |
| 12 | Demo mode cannot trade live | gate `account_type_is_live`; `assert_demo` refuses non-demo | `test_live_gate.py::test_a_demo_account_cannot_pass_as_live` |
| 13 | Unapproved strategy cannot trade live | gate `strategy_authorised` | `test_live_gate.py::test_an_unapproved_strategy_cannot_go_live` |
| 14 | Unapproved model cannot trade live | gate `ai_model_approved` | `test_live_gate.py::test_an_unapproved_model_cannot_go_live` |
| 15 | Risk budget cannot automatically increase | see below | `test_capital.py`, `test_risk.py::test_a_strategy_cannot_loosen_an_account_limit` |
| 16 | Leverage cannot automatically increase | same mechanism | as above |
| 17 | Browser closure cannot stop safety work | workers are supervised loops with heartbeats | `test_workers.py`, `test_bots.py` |
| 18 | Safety failures fail closed | every `None` is a FAIL; an empty report is not ready | `test_live_gate.py::test_missing_evidence_fails_rather_than_skips`, `::test_an_empty_report_is_not_ready` |

## The capital rule

> Approved risk budget = $10,000. The system must NEVER autonomously turn it
> into $10,001.

The mechanism is structural rather than a check somebody remembers to run.

- Limits combine by **most restrictive wins**, not most specific. A strategy
  layer cannot loosen an account layer; a bot cannot loosen its account's.
  Restricting booleans combine by OR, floors by maximum.
  (`test_risk.py::test_the_most_restrictive_limit_wins_not_the_most_specific`,
  `::test_a_strategy_cannot_loosen_an_account_limit`,
  `::test_a_bot_cannot_loosen_its_accounts_limits`)
- The AI seat's return type has **no field that could carry an increase**. It is
  not that a raise would be rejected; it is not expressible.
- Optimisation allocates within the approved budget. It has no path to the
  budget itself — changing a limit goes through `RiskService.set_limits`, which
  is an authenticated, audited, role-gated write.
- `LiveTradingGate` reads `CapitalLimits`. It has no setter.

## What WARNING means

Only two checks may be non-blocking, and both were chosen because neither can
make an order unsafe:

- `notifications_healthy` — L70 §29: notification delivery must never become a
  dependency for order safety. If Discord is down, trading safety still works.
- `release_stamped` — an unstamped image is a rollback problem, not an
  execution one.

**A mandatory check that returns WARNING blocks.** "Warning" on something that
can make an order unsafe is a FAIL wearing a softer word, and the softer word is
how it gets waved through. `soften_optional_failures()` raises if handed a
mandatory check.

## The demo fence, which sits under everything

```python
# tools/mt5_paper.py
if ai.trade_mode != 0:          # 0 DEMO, 1 CONTEST, 2 REAL
    raise RefuseToTrade(...)    # and UNKNOWN(n) refuses too
```

This is in code, not configuration. No setting overrides it, the `MT5Adapter`
calls it rather than reimplementing it, and an unrecognised `trade_mode` refuses
along with the recognised bad ones. It is the reason the platform cannot reach a
real account today regardless of what any environment variable says.

## Failure modes and the response

| Condition | Response |
|---|---|
| MT5 or broker disconnect | stop new orders, latch safe mode with the reason, alert, reconnect, reconcile, resume only if safe |
| Stale market data | no new entries; existing positions managed under the safest available policy |
| Reconciliation mismatch | stop new entries, latch, leave the finding for a person |
| Unexpected position at the venue | **flag only.** Never closed, never modified, never adopted |
| Unknown order state | park it; no retry ever; settled only by asking the venue |
| Risk engine unavailable | fail closed — no engine means no approval means no order |
| Database unavailable | fail closed — state must be durable before a venue is called |
| Notification delivery down | trading continues; the alert is recorded as failed |
| Kill switch engaged | new entries stop; position management continues per policy |

Emergency shutdown fails closed. Nothing here closes a position automatically:
the instinct on finding an unknown position is to flatten it, and that instinct
closes trades somebody opened by hand.
