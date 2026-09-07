# AUTONOMOUS_CONTROL_VERIFICATION.md

What was audited at L62, what was verified, and what could not be. 2026-09-06.

---

## The audit finding that shapes this level

**The L62 brief opens by describing an architecture this repository does not
contain.** It states that the previous level introduced:

> OBSERVATION → STATE ESTIMATION → PortfolioDecisionContext →
> PortfolioDecisionEngine → Policy Evaluation → Action Proposal → Safety
> Validation → Approval / Bounded Auto-Apply → PortfolioRiskOrchestrator → …
> → Outcome Measurement → Policy Effectiveness → Research Opportunity

Searched by class name and by equivalent:

| Named by the brief | In the repository |
|---|---|
| `PortfolioDecisionEngine` | **absent** |
| `PortfolioControlPolicy` | **absent** |
| `PortfolioControlState` | **absent** |
| `PortfolioStateEstimator` | **absent** |
| `PortfolioRiskOrchestrator` | **absent** |
| `PortfolioDecisionContext` | **absent** |
| `PolicyVerificationEngine` | **absent** (added at L62) |
| `RiskEngine` | present, `app/risk/engine.py` |
| `OrderManager` | present, `app/oms/service.py` |
| `BrokerAdapter` | present, `app/brokers/base.py` |
| `ModelRegistry` | present, `app/ai/registry.py` |
| `NotificationService` | present, `app/notifications/service.py` |

This is not a discrepancy to work around. L61 built the safety half of that
loop and **declined the measurement half**, and `MIGRATION_STATUS.md` records
why: no autonomous action has ever been applied on this platform, so there were
no outcomes to measure and no policy whose effectiveness could be assessed.
`app/portfolio/control.py` and `app/portfolio/decision.py` are the two modules
that exist, and both are documented as reaching nothing.

**So most of L62's subject matter is an empty room.** The level was still worth
doing, for a reason the audit made obvious.

## What the audit actually found

Nearly every invariant L62 asks to verify **was already enforced and already
tested**. The platform has thirty-six test files containing guards like
`test_a_signal_cannot_reach_a_venue_without_passing_risk`,
`test_the_ai_cannot_overturn_a_risk_veto`,
`test_an_unknown_order_latches_safe_mode_and_is_never_retried`,
`test_paper_and_live_positions_are_never_pooled`.

What did not exist was any way to **ask whether they all still hold**. That is
what L62 added, and it is the only thing it added that the platform did not
already have.

## The strongest result: there is no unauthorized execution path

Section 34 lists paths that must not exist — TradingView → MT5, AI → MT5,
PortfolioDecisionEngine → MT5, optimizer → MT5, research → MT5, policy → MT5.

Rather than checking six named paths, `test_only_the_oms_reaches_a_venue_to_write`
walks the syntax tree of **every module under `app/`** and finds every call to
`place_order`, `close_position`, `cancel_order` or `modify_order`.

**Two modules in the entire backend call one:**

| Module | Calls | Verdict |
|---|---|---|
| `app/oms/service.py` | all four | the single broker boundary since L19 |
| `app/brokers/mt5.py` | `self.modify_order`, once | the adapter attaching a bracket to a fill it just received |

The MT5 entry is pinned by its own test asserting the call is on `self` and is
the only one in the file, so the allow-list entry cannot silently license a
future write.

Every path the brief names is a special case of this one assertion, and none
had to be enumerated. **A path added later under a name nobody thought of fails
the same test.** Capability-checked: planting `await adapter.place_order(...)`
in `app/execution/pipeline.py` fails it with the file and line.

## What was built

| Component | Why it is not a duplicate |
|---|---|
| `app/safety/invariants.py` | A registry. Enforces nothing; names what does. |
| `app/safety/envelope.py` | Magnitude, frequency, freshness — the half L61's direction-only classifier does not cover. |
| `app/safety/verification.py` | **Delegates** to `control.permitted` and `decision.decide`. Adds only magnitude, identity and the record. |
| `app/safety/certification.py` | Derives gate status from the registry. No enforcement. |

L62 says not to build a second policy engine. That rule is never broken by a
file called `policy_engine_2.py`; it is broken by a verifier that starts
re-deriving a classification the existing one already makes, because calling
out felt awkward. `test_the_verifier_delegates_rather_than_re_deciding` parses
the module and asserts `permitted`, `decide` and `within_envelope` are actually
called, and that no `def classify(`, `def decide(` or `class RiskEngine`
appears in it.

## The defect L62 found

**Absent confidence was more permissive than zero confidence.**

`within_envelope` checks only what it is given, so an action stating
`confidence=0.0` was refused for being under the bar while one stating
`confidence=None` sailed past unchecked. An action with no confidence behind it
was treated as *more* trustworthy than one that honestly reported having none.

That is the L53 defect wearing different clothes: `PortfolioState.open_symbols`
defaulted to an empty frozenset, so "nobody told me what is open" was
indistinguishable from "nothing is open", and the one aggregate risk control
enabled by default could not fire.

**How it was found is the part worth keeping.** TEST J was written as a
property over the whole confidence range — *absent is never more permissive
than present* — rather than as a single scenario. A single-scenario test would
have picked one value and passed.

Fixed in `verify()`: an action that states no confidence has not cleared the
confidence bar. Leaving a field out is not a way to skip a check. Pinned by
`test_stating_no_confidence_is_not_better_than_stating_none`.

## What was NOT built, and why

The brief asks for eleven documents, a policy verification engine, a safety
envelope, decision replay, counterfactual simulation, fault injection, shadow
policy comparison, policy regression suites, a scorecard, migrations, APIs and
a frontend section. Several of those have nothing to attach to:

- **Decision replay and counterfactual simulation of autonomous decisions.**
  No autonomous decision has ever been made here. There is no decision history
  to replay. The reproducibility control that does exist — `app.datasets.leakage`,
  which refuses a feature computed from the future — predates this level and is
  bound as INV-22's evidence rather than reimplemented.
- **Shadow policy comparison (current vs challenger).** There is one policy and
  it is a pure function. A challenger comparison needs two policies and a
  stream of decisions to run both over.
- **Policy versioning and rollback.** No stored policy object exists to version.
  `policy_version` in a verification result is a **hash of the rules actually
  applied** — the envelope's values plus the source of both classifier modules —
  which is honest and serves the purpose a version serves (replaying a decision
  against the rules that made it). A version id invented to fill a field would
  be the fabricated evidence this level exists to prevent.
- **Database migrations for `policy_verifications`, `decision_replays` etc.**
  Tables for records nothing produces. The platform's migration discipline is
  that a table arrives with its writer.
- **A frontend safety section.** It would render a certification state and a
  gate table that are already generated into `SAFETY_CERTIFICATION.md`, for a
  control loop that is not running. Building a dashboard for it now would be
  the "fake metrics" section 26 forbids.

Each of these becomes worth building the day something autonomous actually
runs. What exists now is the half that must be right **before** the first
action is applied.

## Limitations

- The architecture scan is **syntactic**. It finds a call by method name, so a
  venue write reached through `getattr(adapter, name)()` would evade it. No
  such call exists today; the scan does not prove one cannot be written.
- `PolicyVerificationEngine.seen` is process-local. A restart resets "have I
  verified this action". The durable backstop for orders remains
  `orders.intent_id` UNIQUE, which is a database constraint.
- Every envelope default is a **configuration decision, not a measurement**.
  See `AUTONOMOUS_ACTION_SAFETY_ENVELOPE.md`.
