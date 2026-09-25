# Order execution flow

Every path from an intention to a venue, and the refusals along each one.

## The one authorised path

```
  TradingView alert          manual instruction         strategy on bars
        |                          |                          |
        v                          v                          v
  WebhookGateway              /v1/orders                 PaperEngine
  app/webhooks/gateway.py     app/api/v1/orders.py       app/paper/engine.py
        |                          |                          |
        +-------> Signal row (status=new) <-------------------+
                          |
                          v
                  ExecutionWorker            app/execution/worker.py
                  claims the row in the same transaction that reads it
                          |
                          v
                  ExecutionPipeline          app/execution/pipeline.py
                    1. validate the signal      (untrusted external input)
                    2. validate the strategy    (exists, enabled, not paused)
                    3. AI seat                  (advisory; may only decline)
                    4. RISK                     (the only producer of Approval)
                    5. SIZING                   (the only producer of quantity)
                    6. RISK binds the sized order (request_hash)
                    7. OMS                      (the only path to a venue)
                          |
                          v
                  OrderManager.submit(approval, order)   app/oms/service.py
                    intent -> submitting (ON DISK) -> place_order
                          |
                          v
                  BrokerAdapter.place_order      app/brokers/{fake,mt5,shadow}.py
                          |
                          v
                       MetaTrader 5 -> broker
                          |
                          v
                  OrderResult: ACCEPTED | REJECTED | UNKNOWN
                          |
                          v
                  PositionManager -> Journal -> Portfolio -> Analytics
                          |
                          v
                  Monitoring / Recovery / Reconciliation
```

## Why each arrow cannot be short-circuited

| Claim | Mechanism |
|---|---|
| TradingView cannot reach MT5 | the gateway writes a row and publishes an event. It holds no adapter and imports none |
| The HTTP request cannot execute | the gateway's job ends at "recorded"; the worker's starts there. An alert whose execution ran in the request would be lost when the connection dropped |
| The browser cannot execute | `ExecutionWorker` is an `app.workers.Worker` — a supervised loop with a heartbeat |
| Strategy cannot reach the OMS directly | the pipeline holds no adapter; a test parses every module in the package to prove it |
| AI cannot approve | `AiVerdict` has no field expressing an approval, a quantity or a limit |
| Nothing reaches the venue without risk | `submit` takes an `Approval` positionally, and `Approval` is constructible only by `RiskEngine.approve` |
| An approval cannot be reused | it carries `request_hash` and an expiry, both re-checked in `submit` |
| One intent cannot become two orders | a repeat returns the existing order marked `duplicate` |
| A crash mid-send is recoverable | `submitting` is persisted **before** `place_order` is awaited |
| An uncertain result is never retried | `unknown` has no retry edge; `can_resend` is False for it and for `submitting` |

## Order states

```
  created -> validating -> risk_check -> approved -> intent
      -> submitting -> submitted -> partially_filled -> filled     (terminal)
                    -> rejected                                    (terminal)
                    -> cancel_requested -> cancelled               (terminal)
                    -> expired                                     (terminal)
                    -> failed          (never reached the venue -- safe to resend)
                    -> unknown         (exits ONLY via reconcile)
```

`failed` vs `rejected` vs `unknown` is the distinction that decides whether a
retry is safe, and collapsing them loses exactly that. `filled`, `rejected`,
`cancelled` and `expired` have no outgoing edges at all.

## Where the live gate sits

Above all of it, and outside the per-order path:

```
  LiveTradingGate  ->  may live execution be turned on?      (once, per session)
  RiskEngine       ->  may THIS order be sent?               (every order)
```

The gate is never consulted in the engine's place, and passing it does not
approve anything.

## Every code path that can reach MetaTrader 5

Audited across the whole repository:

| # | Path | Status |
|---|---|---|
| 1 | `app/oms/service.py` → `BrokerAdapter.place_order` → `app/brokers/mt5.py` → `tools/mt5_paper` | **the authorised path.** Demo-fenced; no live adapter exists |
| 2 | `tools/mt5_paper.place()` called from `tools/take_profit.py` / `run_overnight.py` | the standalone research harness. Separate process, demo-fenced in code, outside the platform entirely |
| 3 | `tools/mt5_account.py`, `track_record.py`, `rule_backtest.py` | **read-only.** They cannot write |

There is no fourth. The UI, the webhook gateway, the AI package, the research
package, the portfolio optimiser and the recovery manager reach none of them —
each is asserted by a test that parses the relevant package for forbidden
imports.

Path 2 is a documented duplicate execution path and the audit records it as
such (`PROJECT_TRADING_ACTIVATION_AUDIT.md` §7, Blocker 7). It is not removed,
because it is the only thing in this repository that has ever actually traded
and removing it would delete working functionality. The integration that folds
it into path 1 needs the platform to have a demo venue first.
