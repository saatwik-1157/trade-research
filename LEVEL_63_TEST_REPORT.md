# LEVEL_63_TEST_REPORT.md

Continuous assurance, certification lifecycle and autonomy governance.
2026-09-06.

---

# Certification: CONDITIONALLY_CERTIFIED · Autonomy ceiling: BOUNDED (3)

Unchanged from L62. Ten of twelve gates PASS; GATE-01 and GATE-10 remain
WARNING because part of what they would check does not exist here.

---

## 1. The premise, stated once

L63 lists components the repository contains, including
`PortfolioDecisionEngine`, `PortfolioControlPolicy`, `PortfolioControlState`,
`PortfolioRiskOrchestrator`, `Autonomous Control Loop` and `Research
Orchestrator`. **Most do not exist.** This was established at L62 and is
unchanged; the evidence is in `AUTONOMOUS_CONTROL_VERIFICATION.md` and is not
re-argued here.

So L63's question — *is the autonomous control system still operating within
its certified boundaries?* — has a literal answer of "there is no autonomous
control system operating". That is a reason to build the half that must be
right **before** one runs, not a reason to skip the level.

## 2. The audit found three real gaps in L62's own work

This is what made the level worth doing.

| # | Gap | Fix |
|---|---|---|
| 1 | Certification could never go stale — `certify()` returns the same answer forever | `lifecycle.py`: two clocks, and a transition table where nothing reaches CERTIFIED except through REVALIDATING |
| 2 | Certification meant nothing at the moment of action | The engine carries an autonomy level, applied last, one-directional |
| 3 | **The governance machinery was not protected from itself** | `GOVERNANCE_TARGETS` |

## 3. The defect

**An action could ask for more autonomy through an approval path.**

L62 protected the safety envelope by putting every envelope field inside the
set L61 refuses outright. The certification machinery was not in that set,
because it did not exist yet:

| Target | Before L63 | After |
|---|---|---|
| `max_capital_movement` | REJECT | REJECT |
| `autonomy_level` | **REQUIRES_APPROVAL** | REJECT |
| `certification_state` | **REQUIRES_APPROVAL** | REJECT |
| `certification_expiry` | **REQUIRES_APPROVAL** | REJECT |
| `audit_logging` | **REQUIRES_APPROVAL** | REJECT |
| `monitoring` | **REQUIRES_APPROVAL** | REJECT |

Section 15 says never allow escalation above the certified level; section 23
says the system may not disable its own certification, audit log or monitoring.
**`REQUIRES_APPROVAL` satisfies neither, because it is a door rather than a
wall.**

Fixed using L62's own move — put the machinery inside the set L61 already
refuses — rather than a new rule that could be forgotten.

## 4. And a defect in the test for it

The first guard was parameterised over `GOVERNANCE_TARGETS` itself. Deleting
`autonomy_level` from the set **also deleted the case that would have caught
it**, and the suite stayed green under exactly the regression it existed to
prevent.

That is the self-referential guard shape this repository has now hit four
times: C-4, the L57 lifecycle grep, the L62 near-miss, and this. A test that
reads the thing it is checking proves only that the thing agrees with itself.

Fixed with `MUST_BE_PROTECTED`, an independent hand-written list, and
re-capability-checked: dropping `autonomy_level` now fails
`test_the_targets_that_must_never_be_droppable_are_protected[autonomy_level]`.

## 5. Section 35: the fifteen mandatory tests

| | Test | Result |
|---|---|---|
| 1 | SUSPENDED, action proposed → no auto-apply | pass |
| 2 | REVOKED → observe/recommend only | pass |
| 3 | Critical invariant fails → immediate response | pass |
| 4 | Certification expires → autonomy downgrades | pass |
| 5 | Policy changes → impact analysis runs | pass |
| 6 | RiskEngine changes → high-impact revalidation | pass |
| 7 | AI recommends policy change → proposal only | pass |
| 8 | AI recommends removing a risk restriction → REJECT | pass |
| 9 | Recovery after suspension → controlled, not immediate | pass |
| 10 | Repeated action anomaly → circuit breaker | pass |
| 11 | Unknown safety state → fail closed | pass |
| 12 | Frontend attempts autonomy escalation → backend rejects | pass, **found the defect** |
| 13 | One account degraded → others isolated | pass |
| 14 | Policy regression fails → cannot become certified | pass |
| 15 | Evidence cannot be reconstructed → revalidation required | pass |

All fifteen pass. Two are narrower than the brief's wording and say so in their
docstrings: TEST 12's HTTP/RBAC half already exists in `tests/test_security.py`
and is not re-tested weakly here, and TEST 13 asserts isolation through the
action fingerprint rather than through a degraded-account state machine that
does not exist.

## 6. Test results

| Suite | Tests | Result |
|---|---|---|
| `tests/test_autonomy_governance.py` | 48 | pass |
| `tests/test_safety_invariants.py` | 64 | pass |
| `tests/test_policy_verification.py` | 37 | pass |
| **Three safety suites together** | **149** | **pass** |

`ruff check` clean · `ruff format` clean · `mypy` clean over 314 source files.

Invariants: **27 — 25 ENFORCED, 2 NOT_APPLICABLE, 0 UNENFORCED.** Two added at
L63 (INV-26 autonomy ceiling, INV-27 no self-modification of governance), both
under GATE-03.

## 7. Architecture inspection (section 43)

Re-run after the L63 changes:

- `test_only_the_oms_reaches_a_venue_to_write` — **pass.** Still exactly two
  modules in the backend call a venue write: `app/oms/service.py` and one
  `self.modify_order` inside `app/brokers/mt5.py`.
- `test_the_safety_package_reaches_nothing_that_trades` — pass.
- `test_the_safety_package_imports_only_what_it_delegates_to` — pass.
- No risk bypass, no automatic live activation.

Live deployment: `trading_mode=paper`, `live_trading=False`,
`live_execution_allowed=False`.

## 8. What existed / reused / modified / added / rejected

**Existed and reused unchanged:** `app/portfolio/control.py` (L61 classifier),
`app/portfolio/decision.py` (L60 hierarchy), `app/safety/invariants.py`,
`certification.py`, `envelope.py`, `verification.py` (L62), `app/brokers/shadow.py`,
`app/datasets/leakage.py`, `ModelMonitoring`, `StrategyMonitoring`,
`NotificationService`, the recovery and security suites.

**Modified:** `app/safety/verification.py` (governance targets in `hard_limit()`,
autonomy ceiling, level on the record), `app/safety/invariants.py` (INV-26,
INV-27), `app/safety/certification.py` (GATE-03 extended).

**Added:** `app/safety/autonomy.py`, `app/safety/lifecycle.py`,
`app/safety/impact.py`, `tests/test_autonomy_governance.py`, eleven documents.

**Rejected:** assurance evidence engine and snapshots, drift detection
integration, behavioural assurance, trend analysis, assurance replay and
simulation harness, governance review queue, database migrations, APIs, admin
panel section, frontend dashboard. Every one needs a running control loop to
produce the data it would display or store. Section 33 says no fake data.

## 9. Remaining risks

1. **The architecture scan is syntactic.** A venue write through
   `getattr(adapter, name)()` would evade it. None exists.
2. **Every threshold is a configuration decision, not a measurement** — the
   30-day certificate TTL, the 1-day evidence TTL, the eleven envelope bounds,
   three-MEDIUMs-is-a-pattern. Nothing has ever been recertified here. All
   require approval.
3. **`CRITICAL_PATHS` is a hand-maintained list.** A new module as important as
   the OMS would not be on it until somebody added it. The unrecognised-path
   rule limits the damage — it returns FULL_REVALIDATION rather than NONE — but
   not to SUSPEND_AUTONOMY.
4. **Two gates remain CONDITIONAL.** GATE-01 and GATE-10. Both clear when the
   components exist; neither is a defect.
5. **Unchanged and still the top of the list:** the MT5 adapter has never
   connected and the backup has never been restored. L63 governs the
   certification of a component that has never run.

## 10. Unresolved

Nothing found during L63 was left unfixed. The two `NOT_APPLICABLE` invariants
and the two `WARNING` gates are not defects — they are the honest status of
properties whose subjects do not exist.
