# MULTI_HORIZON_AI_POLICY.md

L65 section 33. What AI contributes to a horizon.

---

## AI may

Interpret a horizon, analyse scenarios, classify regime, suggest research,
offer strategic recommendations.

## AI may not

Override horizon priority, override risk, execute trades, change policy, change
certification, or increase risk.

## Why none of those needs an AI-specific rule

**AI has no privileged path into this module.** An AI-sourced recommendation is
a `HorizonRecommendation` like any other: it carries a horizon, it maps to a
layer, and it is resolved by `decide()`.

An AI long-term view enters as `OPTIMIZATION` - the second-weakest of seven
layers. It cannot outrank a portfolio constraint by being confident, because
confidence is not an input to the resolution at all.

`test_an_ai_recommendation_to_raise_risk_cannot_win_on_being_long_term` asserts
it, and the mechanism is the same one that stops any other over-eager input:
the most restrictive verdict wins.

## Provenance

Section 33 requires model id, version, timestamp, input version, confidence and
explanation. `HorizonRecommendation` carries `at`, per-component `confidence`
and a `reason` string. Model id and input version belong on the AI verdict that
produces the recommendation - `app/ai/decision.py` owns that, and duplicating
those fields here would create a second record of one fact.

## The type-level guarantee that predates this level

From `app/execution/ai.py`:

> The seat can only subtract. `AiVerdict` has no field by which a model could
> raise a limit, approve an order, size a position or disengage a kill switch.

A type that cannot express an approval is stronger than a check that rejects
one, because no code path forgets to run it.
