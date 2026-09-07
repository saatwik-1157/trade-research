# LEVEL_62_TEST_REPORT.md

Autonomous control verification, policy assurance and safety certification.
2026-09-06.

---

# Certification: CONDITIONALLY_CERTIFIED

Ten of twelve gates PASS. Two are WARNING because part of what they would check
does not exist in this repository. **Not CERTIFIED, and the difference is the
whole report.**

---

## 1. The audit finding, first

**The brief describes an architecture this repository does not contain.**

Section 1 states that the previous level introduced an OBSERVATION → STATE
ESTIMATION → `PortfolioDecisionContext` → `PortfolioDecisionEngine` → Policy
Evaluation → Action Proposal → Safety Validation → Approval → 
`PortfolioRiskOrchestrator` → … → Outcome Measurement → Policy Effectiveness
loop, and asks L62 to verify that it respects its safety boundaries.

Searched by class name and by equivalent, **six of the seven portfolio-control
components it names are absent**: `PortfolioDecisionEngine`,
`PortfolioControlPolicy`, `PortfolioControlState`, `PortfolioStateEstimator`,
`PortfolioRiskOrchestrator`, `PortfolioDecisionContext`.

This is not a discrepancy to work around. L61 built the safety half of that loop
and explicitly declined the measurement half, and `MIGRATION_STATUS.md` records
why: no autonomous action has ever been applied here, so there were no outcomes
to measure. `app/portfolio/control.py` and `app/portfolio/decision.py` exist and
both are documented as reaching nothing.

**I did not build the missing engines.** The brief itself says the objective is
not to make the system more autonomous, and building them so that they could be
certified would have inverted the level.

## 2. What the audit actually found

Nearly every invariant L62 asks to verify **was already enforced and already
tested before this level**, across thirty-six test files:
`test_a_signal_cannot_reach_a_venue_without_passing_risk`,
`test_the_ai_cannot_overturn_a_risk_veto`,
`test_an_unknown_order_latches_safe_mode_and_is_never_retried`,
`test_paper_and_live_positions_are_never_pooled`, and dozens more.

**What did not exist was any way to ask whether they all still hold.** That is
the only thing L62 added that the platform did not already have.

Final tally: **23 ENFORCED, 2 NOT_APPLICABLE, 0 UNENFORCED.**

## 3. The strongest result

**There is no unauthorized execution path**, and it is verified as a property of
the whole codebase rather than of six named paths.

`test_only_the_oms_reaches_a_venue_to_write` walks the syntax tree of every
module under `app/` and finds every call to `place_order`, `close_position`,
`cancel_order` or `modify_order`.

| Module | Calls | Verdict |
|---|---|---|
| `app/oms/service.py` | all four | the single broker boundary since L19 |
| `app/brokers/mt5.py` | `self.modify_order` ×1 | the adapter attaching a bracket to a fill it just received |

**Nothing else in the backend can reach a venue to write.** TradingView → MT5,
AI → MT5, PortfolioDecisionEngine → MT5, optimizer → MT5, research → MT5,
policy → MT5 — every path section 34 forbids is a special case of that one
assertion, and none had to be enumerated for it to hold.

The MT5 allowance is pinned by its own test asserting the call is on `self` and
is the only one in the file, so the allow-list entry cannot silently license a
future write.

**Capability-checked:** planting `await adapter.place_order(request)` in
`app/execution/pipeline.py` fails the test with the file and line number.

## 4. The defect found and fixed

**Absent confidence was more permissive than zero confidence.**

An action stating `confidence=0.0` was refused for being under the bar, while
one stating `confidence=None` passed unchecked. An action with no confidence
behind it was treated as *more* trustworthy than one that honestly reported
having none.

That is the **L53 defect wearing different clothes**:
`PortfolioState.open_symbols` defaulted to an empty frozenset, so "nobody told
me what is open" was indistinguishable from "nothing is open", and the one
aggregate risk control enabled by default could not fire.

**How it was found matters.** Section 28's TEST J was written as a property over
the whole confidence range — *absent is never more permissive than present* —
rather than as one scenario. It failed on first run. A single-value test would
have passed.

Fixed in `verify()`; pinned by
`test_stating_no_confidence_is_not_better_than_stating_none`.

## 5. The registry cannot claim what it cannot show

Every invariant marked ENFORCED names the module that enforces it and the test
that proves it, and `test_every_enforced_invariant_names_a_test_that_exists`
resolves both.

**On first run it caught thirteen bad pointers of my own** — eleven naming real
tests in the wrong file, two naming nothing at all. They were written from
memory and greps, which is exactly how a registry becomes a list of confident
sentences.

Two invariants are `NOT_APPLICABLE`. It would have been easy to mark them
ENFORCED: the components they constrain do not exist, so nothing can violate
them and every test would pass. **A test that cannot fail is not evidence.**

## 6. Section 28: the fifteen mandatory tests

| | Test | Result |
|---|---|---|
| A | budget $10,000, proposal $15,000 → REJECT | pass |
| B | RiskEngine rejects, retry proposed → REJECT | pass |
| C | AI recommends leverage increase → REJECT | pass |
| D | SAFE_MODE, action proposed → no auto-apply | pass |
| E | unknown broker state, retry → reconcile first | pass |
| F | market data stale, entry proposed → defer | pass |
| G | strategy quarantined, allocation proposed → REJECT | pass |
| H | duplicate proposal → one execution at most | pass |
| I | same action repeats rapidly → cooldown | pass |
| J | AI unavailable → deterministic fallback | pass, **found the defect** |
| K | policy tries to change a hard risk limit → FORBIDDEN | pass |
| L | frontend unauthorized policy mutation → backend reject | **partial** |
| M | policy version changes mid-decision | **reduced** |
| N | replay attempts future data → fail closed | **reduced** |
| O | live trading flag enabled → controls prevent it | pass |

**L, M and N could not be written as section 28 describes**, and each says so in
its own docstring rather than being weakened into something that passes:

- **L** is an HTTP/RBAC assertion that already exists in `tests/test_security.py`.
  Re-testing it here would create a second, weaker version — and the weaker one
  is the one that gets maintained. The half that is this module's own *is*
  tested: an approval-required action never comes back auto-appliable.
- **M** asks what happens when the policy version changes mid-decision. **There
  is no stored policy to change.** What is asserted instead is the property a
  version exists to give: one verification carries exactly one version, derived
  from the rules actually applied, and different rules version differently.
- **N** asks about replay accessing future data. That control exists and
  predates this level (`app.datasets.leakage`, INV-22's evidence) and is not
  duplicated. What is tested here is the narrower reproducibility property this
  module owns: an action's identity includes the account and the rule set.

## 7. Test results

| Suite | Tests | Result |
|---|---|---|
| `tests/test_safety_invariants.py` | 60 | pass |
| `tests/test_policy_verification.py` | 38 | pass |
| `tests/test_portfolio_control.py` | 22 | pass |
| `tests/test_portfolio_decision.py` | 21 | pass |

`ruff check` clean · `ruff format` clean · `mypy` clean over 311 source files.

Backend suite prior to this level: 2,583 passed, 0 failed.

## 8. Completion against section 35

| Requirement | Status |
|---|---|
| Existing architecture audited | done — and the audit is the finding |
| No duplicate safety engines created | done, asserted by test |
| Safety invariants documented | done — 25, with statuses |
| Policy verification implemented | done — delegating, not duplicating |
| Safety envelope implemented | done — 11 bounds, all config |
| Policy hierarchy verified | done — including through the wrapper |
| State transitions verified | partial — quarantine and approval paths |
| Decision replay verified | **declined** — no decision history exists |
| Counterfactual testing | done as parameterised tests |
| Fault injection | pre-existing, bound as evidence |
| Runaway loop protection verified | done |
| Idempotency verified | done |
| Shadow policy comparison | **declined** — one policy, a pure function |
| Policy regression suite | done — the invariant resolver |
| Safety scorecard | done — `SAFETY_SCORECARD.md` |
| Certification gates | done — 12, derived not declared |
| Policy versioning verified | **NOT_APPLICABLE** — no policy object |
| Rollback verified | **NOT_APPLICABLE** — same |
| AI safety boundaries verified | done — enforced by type |
| Security verified | pre-existing, bound as evidence |
| Monitoring integrated | **not done** — nothing produces events yet |
| Database migrations | **declined** — tables for records nothing writes |
| APIs verified | **not done** — nothing to serve |
| Frontend verified | **declined** — see below |
| Critical safety tests pass | 12 of 15 as specified, 3 reduced and labelled |
| No unauthorized execution path | **verified, whole codebase** |
| Paper mode remains default | verified |
| Live trading remains disabled | verified |
| Documentation complete | 9 documents |
| `LEVEL_62_TEST_REPORT.md` | this file |

## 9. What was rejected, and why

- **A frontend AUTONOMOUS CONTROL SAFETY section.** It would render a
  certification state and a gate table that are already generated into
  `SAFETY_CERTIFICATION.md`, for a control loop that is not running. Section 26
  says never create fake metrics; a live-looking dashboard over a static
  registry is exactly that.
- **Migrations for `policy_verifications`, `decision_replays`,
  `counterfactual_runs`, `control_loop_health`.** Tables for records nothing
  produces. This platform's discipline is that a table arrives with its writer.
- **Read APIs for certification and scorecard.** Same reason. The data is four
  static tuples; serving it over HTTP would imply it changes.
- **Monitoring and alerting for policy violations.** No component emits one.
- **Shadow policy comparison, decision replay, policy versioning and
  rollback.** Each needs a running control loop. The shadow adapter pattern
  (`app/brokers/shadow.py`) is the right thing to extend when there is
  something to shadow, and is named so it is not rebuilt.

## 10. Remaining risks

1. **The architecture scan is syntactic.** It matches a call by method name, so
   a venue write through `getattr(adapter, name)()` would evade it. No such call
   exists today; the scan proves none is written directly, not that none can be.
2. **The envelope's eleven defaults are configuration decisions, not
   measurements**, and require approval. Nothing runs against them today.
3. **The idempotency cache is process-local.** A restart resets "have I verified
   this action". The durable backstop for orders remains `orders.intent_id`
   UNIQUE.
4. **Two gates are certified as CONDITIONAL, not passed.** GATE-01 and GATE-10.
   Both clear when the components exist; neither is a defect.
5. **Unchanged from previous levels, and still the top of the list:** the MT5
   adapter has never connected, and the backup has never been restored. L62
   certified the architecture around a component that has never run. That is
   worth having and it is not the same as the component having run.

## 11. Environment

`TRADING_MODE=paper` · `LIVE_TRADING=false` · `live_execution_allowed=false`
— verified on the running deployment, unchanged. No live trading, no production
capital, no real broker execution at any point in this level.
