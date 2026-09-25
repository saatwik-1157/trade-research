# POLICY_CONFLICT_RESOLUTION.md

How conflicting policies resolve. L62 §7, 2026-09-06.

---

**This document is a pointer, not a second specification.** The hierarchy and
its reasoning are in `DECISION_HIERARCHY.md` (L60) and the implementation is
`backend/app/portfolio/decision.py`. L62 verified it rather than restating it,
because two documents describing one hierarchy is how the two come to disagree.

## The hierarchy

    HARD_SAFETY > RISK > PORTFOLIO > STRATEGY > SIGNAL > OPTIMIZATION > PREFERENCE

`Layer` is an `IntEnum`, so the comparison **is** the ordering rather than a
lookup table that could disagree with the documentation beside it.

## Why a lower layer cannot relax a higher one

`decide()` takes the most severe verdict present and records which layer
produced it. An `ALLOW` from OPTIMIZATION cannot displace a `RESTRICT` from
RISK, because **the function cannot express that outcome** — it is not a rule
somebody has to remember, it is the shape of the return value.

Ties break to the higher layer, so the reason names the authority a reader
should go to.

## What L62 added

One thing: the hierarchy had to survive being **wrapped**.

`PolicyVerificationEngine` takes an optional `DecisionContext` and runs
`decide()` over it. If the wrapper had read only the verdict and not the layer,
or had mapped a `RESTRICT` onto its own `PASS`, it would have reintroduced
exactly what `decide()` was built to make unrepresentable.

`test_a_layer_below_risk_cannot_relax_it_through_the_verifier` puts an
OPTIMIZATION `ALLOW` beside a RISK `RESTRICT` and asserts the verification does
not come out appliable, and that the recorded deciding layer is `RISK`.

## The deterministic conflict cases

Section 7 lists five. All five are tested, four of them against the real
classifiers rather than against a restatement:

| Case | Expected | Test |
|---|---|---|
| AI recommends more exposure, RiskEngine rejects | REJECT | `test_a_risk_veto_is_not_retried_around` |
| Optimizer wants more allocation than the budget allows | REJECT / bounded | `test_a_proposal_over_the_approved_budget_is_refused` |
| Strategy quarantined, optimizer proposes allocation | no new entry | `test_a_quarantined_strategy_gets_no_new_exposure` |
| SAFE_MODE, optimizer proposes adaptation | no auto-apply | `test_nothing_is_auto_applied_in_safe_mode` |
| Approval-required action proposed | wait for approval | `test_an_approval_required_action_is_never_auto_applied` |

The quarantine case has a second half that matters more than the first:
`test_quarantine_still_allows_reducing_what_is_already_open`. A restriction
that also blocked exits would be a way of being unable to get out.
