# FINAL_PRODUCTION_READINESS.md

Level 42, 2026-09-05.

**This document is the decision. `PRODUCTION_READINESS.md` (L41) is the
category-by-category scorecard and remains the detail; this is the verdict
drawn from it and from the final audit.**

---

## Two decisions, deliberately separate

```
APPLICATION DEPLOYMENT:   READY WITH CONDITIONS
LIVE FINANCIAL TRADING:   NOT READY
```

Conflating those would be the most dangerous sentence in this repository. The
application can be deployed, operated, monitored, recovered and rolled back
today. It must not be given real money.

## Deployment: READY WITH CONDITIONS

**Verified against a running stack, not asserted:**

* migrations applied to a real PostgreSQL database, **0021 -> 0025, with 252
  trades from 2026-08-24 to 2026-09-02 preserved** and verified by query
* WebSocket handshake **HTTP 101** through nginx (404 before the L41 fix)
* four health endpoints correct through the proxy, `/health/trading` -> 503
  which is the honest state with no adapter
* webhook end to end: accepted -> duplicate (same signal id) -> 401 -> 401,
  **with no order created**
* graceful shutdown: workers before the hub, 0 failures
* restart: recovery sequence ran and reported honestly
* production publishes **only nginx 80/443**, asserted by CI
* image stamped, non-root uid 10001, all 293 modules import inside it
* **2374 backend tests passed, 0 failed**; 247 frontend; integration suite
  **37/37 against PostgreSQL**

**The conditions, accepted knowingly** (2 of 3 closed at L43):

1. **A volume failure loses everything.** No backup has been taken and no
   restore tested. Until H-1 closes, the platform's durability is "the Docker
   volume has not failed yet".
2. ~~CI is red~~ — **CLOSED at L43.** `mypy` clean over 346 files, `ruff check`
   and `ruff format --check` clean.
3. ~~`test_realtime.py` is flaky~~ — **CLOSED at L43.** Root cause was a
   connection shared across two event loops; five consecutive clean runs after.

**One condition remains, and it is the first one.**

## Live trading: NOT READY

Not a matter of confidence. Three independent mechanisms would each have to
change:

| Gate | State |
|---|---|
| `LIVE_GATES` | **all 11 false** |
| `LIVE_TRADING` | **false**, and `TRADING_MODE=live` without it refuses to start |
| A live broker adapter | **does not exist** |

And beneath those, `tools/mt5_paper.assert_demo` refuses any terminal whose
`trade_mode` is not demo.

**The substantive blocker is not the flags.** It is that **nothing in this
platform has ever been told it is wrong by a real venue.** Every
reconciliation, disconnect, rejection, partial fill and unknown-order recovery
is exercised against a fake this repository wrote -- one that agrees with the
platform's model of a venue because it was written from that model.

No amount of further testing closes that. It closes when a demo account is
connected and the reconciliation logic meets a real retcode.

## Before live trading, in order

1. **Take a backup. Restore it into a scratch database. Compare row counts.**
   Not a script that exists -- a restore that has happened.
2. **Connect a demo account** and re-run the disconnect, reconnect,
   reconciliation and unknown-order scenarios against it.
3. **Fix CI.**
4. **Build MFA**, or accept in writing that an administrative session is a
   password and a cookie plus a same-password step-up.
5. **Run the concurrency tests against PostgreSQL.**
6. Only then revisit the `LIVE_GATES`, one at a time, each with the test that
   justifies flipping it.

## What would change this verdict

Closing H-1 alone moves deployment from READY WITH CONDITIONS to READY.

Closing H-1 and H-2 makes live trading a *decision* rather than a blocked
state -- still requiring the gates to be flipped deliberately, one at a time,
which is the design and should not be circumvented.
