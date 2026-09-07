# AUTONOMY_TEST_MATRIX.md

Level 51, 2026-09-06. **Every `PASS` names the test or the observation that
produced it.** `BLOCKED` means no venue and no amount of further work on this
machine closes it — it is not `FAIL` and it is not `NOT APPLICABLE`.

| # | Scenario | Detection | Automatic action | Safety gate | Verification | Escalation | Expected | Actual | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | API worker tick raises | `Worker.run` counts the failure | none — loop continues | n/a | `status.failures` increments | log | survives, does not spin | as expected | **PASS** — `tests/test_workers` |
| 2 | Redis down | breaker opens after 3 consecutive failures | degrade to no fan-out | breaker | `/health/ready` 503 | log | webhook still accepts and dedups | **observed live at L44** | **PASS** |
| 3 | Redis restored | breaker closes | resume fan-out | n/a | ready 200 | — | no restart needed | **observed live** | **PASS** |
| 4 | Postgres down | health check | none | n/a | ready 503, webhook 500 | log | refuse rather than accept-and-lose | **observed live** | **PASS** |
| 5 | Postgres restored | health check | none | n/a | dedup intact | — | no restart needed | **observed live** | **PASS** |
| 6 | Bot heartbeat stops | supervisor sweep | mark `crashed` | — | `bot_events` | log | marked, **not** restarted | as expected | **PASS** |
| 7 | Crashed bot, gates agree | sweep | restart | `recovery_gate` (5 conditions) | runner returns bool | `recovery_failed` event | restart once | as expected | **PASS** |
| 8 | Crashed bot, safe mode on | sweep | **refuse** | safe-mode gate | — | `recovery_refused` | never restarted | as expected | **PASS** |
| 9 | Crashed bot, kill switch on | sweep | **refuse** | kill-switch gate | — | `recovery_refused` | never restarted | as expected | **PASS** |
| 10 | Crashed bot, unresolved order on its account | sweep | **refuse** | reconciliation gate | — | `recovery_refused` | never restarted | as expected | **PASS** |
| 11 | **Bot crashes on start, repeatedly** | sweep | restart, then **stop** | **recovery budget (new)** | attempts counted per bot from `bot_events` | `recovery_budget_exhausted` | bounded | **was UNBOUNDED; now ≤3 in 12 sweeps** | **PASS (defect fixed)** |
| 12 | The budget itself could be inert | — | — | — | same loop with a generous budget | — | runs away | >3 attempts, loop reproduces | **PASS (capability check)** |
| 13 | Unattended code changes risk limits | AST audit of the import graph from every `Worker` | — | — | — | — | no such path | none found | **PASS** |
| 14 | Unattended code promotes a model | same | — | — | — | — | no such path | none found | **PASS** |
| 15 | The Phase 30 matcher is inert | — | — | — | run against planted code | — | fires | fires | **PASS (capability check)** |
| 16 | Trading mode changed at runtime | AST scan outside `__init__` | — | — | — | — | no such assignment | none found | **PASS** |
| 17 | Unknown order state after restart | startup reconciliation reads `orders` | latch safe mode | — | `unresolved_orders` | operator | never retried | **fixed at L45 C-1**; before it, the table was empty and safe mode never latched | **PASS** |
| 18 | Duplicate order across a restart | durable intent guard | refuse | `orders.intent_id` UNIQUE | 27 tests | — | refused | as expected | **PASS** |
| 19 | MT5 disconnect | — | — | — | — | — | stop new orders, reconcile | **no terminal has ever been connected** | **BLOCKED** |
| 20 | Broker disconnect / timeout | — | — | — | — | — | stop, reconcile, resume only if safe | **no venue** | **BLOCKED** |
| 21 | Reconciliation failure | — | — | — | — | — | escalate, do not resume | **no venue** | **BLOCKED** |
| 22 | Market-data disconnect | freshness check exists | — | — | — | — | stop new decisions | `market_bars` covers one instrument; reports `UNKNOWN` honestly | **BLOCKED** |
| 23 | AI provider failure | — | — | — | — | — | strategy's approved fallback | **every AI test drives a stub; no provider has been called** | **BLOCKED** |
| 24 | Simultaneous failures (Phase 24) | — | — | — | — | — | no unsafe interaction | requires 19–23 | **BLOCKED** |
| 25 | Worker auto-restart | — | **none exists** | — | — | — | — | `WorkerRegistry` counts failures and never acts | **NOT APPLICABLE** |
| 26 | Notification failure isolated from trading | — | — | — | a raising bus does not stop an order | — | isolated | as expected | **PASS** |

---

## Tally

**PASS 18 · BLOCKED 6 · NOT APPLICABLE 1 · FAIL 0**

## The six blocked rows reduce to two facts

**No venue has ever been connected**, and **no market-data feed has ever been
connected**. Rows 19–24 are the chaos scenarios L51 Phase 23 and Phase 24 ask
for, and every one of them needs a broker or a feed to disconnect *from*.
Writing them up as passing would be fabricated recovery evidence, which rule 24
forbids.

## The one defect this matrix found

Row 11. It was found by asking Phase 19's question — "is this bounded?" — of a
recovery path whose safety gates were all good and all about something else.
