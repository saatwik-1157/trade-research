# CONTINUOUS_ASSURANCE_ARCHITECTURE.md

Continuous assurance. L63, 2026-09-06.

---

## The premise, stated once

L63 opens by listing components the repository contains, including
`PortfolioDecisionEngine`, `PortfolioControlPolicy`, `PortfolioControlState`,
`PortfolioRiskOrchestrator`, `Autonomous Control Loop`, `Research
Orchestrator`, `Capital Allocation` and `Adaptive Strategy Governance`.

**Most of those do not exist.** This was established and documented at L62
(`AUTONOMOUS_CONTROL_VERIFICATION.md`) and is unchanged: L61 built the safety
half of the control loop and declined the measurement half, because no
autonomous action has ever been applied here.

So the question L63 poses — *is the current autonomous control system still
operating within its certified boundaries?* — has a literal answer of "there is
no autonomous control system operating". That is not a reason to skip the
level. It is a reason to build the half that has to be right **before** one
runs, and to decline the half that would be built against imagined data.

## What was genuinely missing, and is now built

L62 left three real gaps. Finding them was the value of the audit.

**1. Certification could not go stale.** `certify()` is a pure function of a
static registry, so it returned the same answer forever. `lifecycle.py` adds
two clocks — a 30-day certificate TTL and a 1-day evidence TTL — and a
transition table in which no state reaches `CERTIFIED` except through
`REVALIDATING`.

**2. Certification meant nothing at the moment of action.** A suspended
certificate appeared in a report and stopped nothing.
`PolicyVerificationEngine` now carries an autonomy level and refuses to mark
anything auto-appliable below BOUNDED.

**3. The governance machinery was not protected from itself.** The defect
below.

## The defect L63 found

**An action could ask for more autonomy through an approval path.**

L62 protected the safety *envelope*: every envelope field is a hard limit, so
an action aimed at one is an action that loosens a hard constraint, which L61
refuses outright with no approval path.

The certification machinery was not in that set, because it did not exist yet.
So before L63:

| Target | Classified as |
|---|---|
| `max_capital_movement` | REJECT (forbidden) — correct |
| `autonomy_level` | **REQUIRES_APPROVAL** |
| `certification_state` | **REQUIRES_APPROVAL** |
| `certification_expiry` | **REQUIRES_APPROVAL** |
| `audit_logging` | **REQUIRES_APPROVAL** |

Section 15 says never allow escalation above the certified level and section 23
says the system may not disable its own certification. **`REQUIRES_APPROVAL`
satisfies neither, because it is a door rather than a wall.**

Fixed with `GOVERNANCE_TARGETS`, using the same move L62 used for the envelope:
put the machinery inside the set L61 already refuses, rather than adding a new
rule that could be forgotten. All five now REJECT.

## And a defect in the test for it

The first version of the guard was parameterised over `GOVERNANCE_TARGETS`
itself — so deleting `autonomy_level` from the set also deleted the case that
would have caught it, and the suite stayed green under exactly the regression
it existed to prevent.

**That is the self-referential guard this repository has now hit four times.** A
test that reads the thing it is checking proves only that the thing agrees with
itself. Fixed with `MUST_BE_PROTECTED`, an independent list written out by
hand, and re-capability-checked.

## Where it is all enforced

    ACTION PROPOSAL
      -> safe mode                     (latched, above everything)
      -> unknown venue state           (reconcile first)
      -> RiskEngine veto               (carried, never re-derived)
      -> direction + loop protection   -> L61 control.permitted
      -> magnitude / frequency         -> L62 envelope.within_envelope
      -> stale evidence                -> L60 decision.decide
      -> quarantine                    (no new exposure; exits stay open)
      -> environment                   (nothing autonomous touches live)
      -> idempotency                   (fingerprint)
      -> AUTONOMY CEILING              -> L63, applied last, one-directional

The ceiling is applied **last** so it can only ever reduce the outcome, and at
the **action** rather than at the certificate.

## What was declined, and why

Everything that needs a running control loop to produce data:

- **Assurance snapshots and the evidence engine.** Aggregating evidence from
  systems that emit no autonomous-control events would produce a snapshot of
  nothing, with a hash to make it look rigorous.
- **Drift detection integration.** `ModelMonitoring` and `StrategyMonitoring`
  exist and are not duplicated; there is no autonomous behaviour to compare
  their output against.
- **Behavioural assurance** (expected vs actual action). No actions.
- **Trend analysis.** One data point is not a trend.
- **Governance review queue, migrations, APIs, admin panel, frontend
  dashboard.** Storage and views for records nothing produces. Section 33 says
  no fake data; a live-looking dashboard over four static tuples is that.
- **Assurance replay and simulation.** No decision history to replay. The
  leakage control that does exist (`app.datasets.leakage`) predates L62 and is
  bound as INV-22's evidence rather than reimplemented.

Each becomes worth building the day something autonomous runs.

## Certification status

**CONDITIONALLY_CERTIFIED**, unchanged from L62. 27 invariants: 25 ENFORCED,
2 NOT_APPLICABLE, 0 UNENFORCED. GATE-01 and GATE-10 remain WARNING.

**Autonomy ceiling: BOUNDED (3)**, which is what `CONDITIONALLY_CERTIFIED`
permits — and `PolicyVerificationEngine` defaults to it rather than to
CERTIFIED, so the engine cannot start out claiming more than the certification
behind it.
