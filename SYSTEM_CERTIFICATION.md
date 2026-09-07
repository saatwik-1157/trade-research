# SYSTEM_CERTIFICATION.md

Level 42, 2026-09-05. Nothing below is marked PASS without evidence, and the
evidence is named.

*(Re-certified after L43 post-audit hardening, 2026-09-05. Four issues closed;
the two blockers are unchanged.)*

> **SUPERSEDED IN PART, 2026-09-06.** The L45 eligibility gate found **four
> CRITICAL trading-safety defects** that this certification did not.
> `TRADING SAFETY` went to **FAIL**. The certification below is left intact
> because the reasoning that produced it is worth reading against the reasoning
> that overturned it — 17/17 safety rules "verified" structurally, and an
> adversarial pass found four holes that structural verification could not.
>
> **UPDATED LATER THE SAME DAY: all four are fixed**, each with a regression
> test that fails on the old code. `TRADING SAFETY` returns to PASS *for the
> defects*, and the pilot gate remains **NOT ELIGIBLE** for reasons that were
> never about them — no venue has ever been connected and no backup has ever
> been restored.

```
PROJECT STATUS:        PARTIAL
PRODUCTION STATUS:     NOT READY   (was: READY WITH CONDITIONS)
LIVE TRADING STATUS:   NOT READY
PILOT STATUS:          NOT ELIGIBLE  (no venue; no restored backup)
SECURITY STATUS:       PASS
RECOVERY STATUS:       PASS        (C-1 fixed: the pipeline now writes the
                                    orders row that startup reconciliation and
                                    safe mode always read and never found)
INTEGRATION STATUS:    PASS
DEPLOYMENT STATUS:     PARTIAL
TRADING SAFETY:        PASS        (C-1, C-2, C-3, C-4 fixed and regressed;
                                    behaviourally verified, not structurally)
```

**Read that last line with the caveat it carries.** These verdicts are earned
against a test suite and a simulator. **No part of this platform has ever
spoken to a real venue**, so "PASS" here means "the defect is fixed and a test
holds it fixed", not "this was observed working in production".

### Why the earlier PASS was wrong

The 17 safety rules were verified **structurally** — by walking the import
graph and asserting that a package could not reach a broker. That method is
sound for what it measures and it missed all three defects, because:

* C-1 lives in **process lifetime**, not in the import graph. Every duplicate
  test ran inside one process.
* C-2 reaches the adapter through an object it was legitimately handed
  (`manager.adapter`), so no forbidden import appears anywhere.
* C-3 is a test that asserts the wrong property.
* C-4 was a check whose two sides derived from the same object, so it could
  never fail. Nothing structural distinguishes a check that always passes from
  a check that passes because the code is correct.

**Structural verification proves a package cannot reach something. It cannot
prove a package uses correctly what it was given.** That is the lesson worth
carrying forward.

### What the fixes changed about how this is verified

Each fix carries a **behavioural** regression, and two of them pin the pre-fix
behaviour as an explicit assertion — `test_the_restart_regression_is_capable_of_failing`
runs the C-1 scenario with no store and asserts the venue is asked twice. A
regression test that cannot fail is C-4 in test form.

Three of the four also gained a structural guard, aimed at the failure one
level up from the defect: that the mechanism gets built and never wired.
`test_the_deployed_pipeline_is_given_a_store` reads `app/main.py` rather than
trusting it, because `OrderManager.resume()` and `load_unresolved()` were both
complete, both tested, and both had zero callers.

**PARTIAL, not COMPLETE** — and the reason is two things that were never done
rather than anything done wrong: no backup has ever been restored, and no
broker adapter has ever been registered.

---

## Evidence

| Status | Evidence |
|---|---|
| **SECURITY — PASS** | 44 tests. Headers on every response incl. errors; CORS wildcard refused at startup; step-up on four dangerous actions (scoped, subject-bound, session-bound, single-use); per-user socket cap; allow-listed event payloads. Secret scan clean: no tracked `.env`/`.pem`/`.key`, no secret-shaped literal, no `.env` on disk. No `eval`/`exec`/`subprocess`/`pickle`/`shell=True`; no f-string SQL; no upload surface; non-root uid 10001. **Gap: no MFA, stated in every posture response.** |
| **RECOVERY — PASS** | 46 tests. 16-step startup sequence **observed running live** after restart (`clean=false, needs_attention=1, safe_mode=false`). Safe mode latches with a reason, blocks three server-side paths, is re-derived every boot. Graceful shutdown **observed**: workers before the hub, 0 failures. |
| **INTEGRATION — PASS** | 35 cross-subsystem tests. **7 real defects found and fixed at L40–L41**, six of them at a seam no single suite could reach. Full chain verified against the deployed stack. |
| **TRADING SAFETY — PASS** | **11/11 safety rules verified structurally** against the import graph, plus behavioural verification of risk veto, AI-cannot-override, sizing refusal, unknown-order non-retry and paper/live isolation. |
| **DEPLOYMENT — PARTIAL** | Verified live: migrations 0021→0025 on a real database with **252 trades preserved**; WebSocket **101** through nginx; four health endpoints correct; production publishes only nginx 80/443; image stamped and non-root. **Blocked on backup/restore.** |
| **PRODUCTION — READY WITH CONDITIONS** | Deployable and operable in paper mode today. Condition: a volume failure loses history until a backup exists. (CI was a second condition at L42; it was closed at L43.) |
| **LIVE TRADING — NOT READY** | All 11 `LIVE_GATES` false, `LIVE_TRADING=false`, **no live adapter exists**. Three independent mechanisms. |

## The 17 safety rules

| Rule | Result | How |
|---|---|---|
| 1 TradingView cannot place an MT5 order | **PASS** | `app/webhooks` imports no broker, calls no order verb |
| 2 AI cannot bypass RiskEngine | **PASS** | `app/ai` reaches neither broker nor OMS; approving AI cannot override a veto (test) |
| 3 Admin cannot bypass RiskEngine/OMS | **PASS** | `app/admin` imports no risk, OMS, broker, execution or sizing module |
| 4 Recovery cannot bypass RiskEngine/OMS | **PASS** | `app/recovery` calls no order verb; reconcilers report, never repair |
| 5 Unknown state reconciled before retry | **PASS** | `test_an_unknown_venue_answer_is_never_retried`; venue count unchanged on a second pass |
| 6 MT5 disconnect blocks new orders | **PASS** | verified against `FakeBroker.disconnect()` |
| 7 Reconnect requires reconciliation | **PASS** | startup sequence; no automatic reconnection by design |
| 8 Position mismatch prevents execution | **PASS** | latches safe mode |
| 9 Stale data creates no signal | **PASS** | freshness measured against the alert's own timestamp, not arrival |
| 10 Browser closure cannot stop workers | **PASS** | workers are backend services; separate container in production |
| 11 Paper is the default | **PASS** | asserted on every test run |
| 12 `LIVE_TRADING=false` default | **PASS** | asserted on every test run |
| 13 No deployment enables live trading | **PASS** | stated explicitly in the production overlay; all gates false |
| 14 Training does not deploy a model | **PASS** | verified structurally |
| 15 No duplicate event → duplicate execution | **PASS** | verified live: same alert twice → same signal id; pipeline race → one order |
| 16 No production database reset | **PASS** | zero dangerous destructive operations; migration tests now refuse a non-scratch database |
| 17 No secret exposed | **PASS** | scan clean across source, frontend, image, logs, API, health |

**17/17.**

## Audit score

Weighted by consequence, not by count. A trading platform that is beautifully
documented and cannot restore its database is not an 8.

| Dimension | Score | Basis |
|---|---|---|
| Architecture | 9.5 | one bus, one simulator, one audit log; every refusal documented |
| Functionality | 9.0 | 33 of 42 levels PASS, 0 FAIL |
| Trading safety | 9.5 | 17/17 rules; gates unbypassable |
| Risk | 9.0 | mandatory veto, verified unbypassable |
| Security | 8.5 | strong; **no MFA** |
| Reliability | 7.5 | recovery verified; **never tested against a real venue** |
| Recovery | 8.5 | sequence + latch verified live; **restore untested** |
| Testing | 9.5 | **2374 passing**; realtime flake fixed (5 clean runs), concurrency verified on PostgreSQL 37/37 |
| Observability | 9.0 | five-state health, hysteresis, verified running |
| Deployment | 7.5 | verified live; **backup/restore still blocked**; CI now green |
| Documentation | 9.5 | 54 documents, corrected when they went stale |

**Overall 8.6 / 10 at L42. After L43 hardening: 8.9 / 10** — still capped by
deployment and reliability, because both HIGH risks are unchanged. Four MEDIUM
and LOW issues closing moves the score a little; it cannot move the cap, and a
scoring method where it could would be the wrong method.

The score is deliberately not higher. Two HIGH risks are open, and both are
about the platform never having been tested against reality: a real venue, and
a real restore.

## Certification

**PROJECT NOT FULLY CERTIFIED — BLOCKERS REMAIN.**

The blockers are H-1 (no tested backup or restore) and H-2 (no registered
broker adapter). Both are open, both are documented, and neither can be closed
by writing more code — one needs a storage decision, the other needs a demo
account.

What *is* certified: the platform is safe to deploy and operate in paper mode,
its trading-safety rules hold, and it fails closed everywhere it was tested.
