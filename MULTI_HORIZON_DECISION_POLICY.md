# MULTI_HORIZON_DECISION_POLICY.md

L65 sections 9 to 12. The decision object.

---

## Determinism

`synthesise()` is a pure function of its inputs. Identical inputs produce an
identical decision and an identical explanation - section 11's requirement,
and it falls out of `decide()` being pure rather than needing a rule.

`test_synthesis_is_deterministic`.

## Confidence is per component

Section 12 forbids a single opaque score. `HorizonRecommendation.confidence` is
a mapping, and the synthesised decision namespaces them
(`short_term.data`, `short_term.model`).

**An absent confidence is absent, never high.** A horizon that reported no
confidence contributes no keys - it does not contribute a default. That is the
same rule as everywhere else in this platform: unknown is not healthy, missing
is not permission, absent is not zero.

## What the decision records

Decision id, timestamp, account, environment, the resolved `Decision` from
L60, every live recommendation, everything deferred with its reason and
condition, everything expired, the conflicts, and per-component confidence.

Plus an `authority` line on every serialisation saying it applied nothing.

## It decides nothing about orders

`MultiHorizonDecision` is a recommendation. The RiskEngine remains the final
veto and the OMS owns order state; nothing in the module reaches either, and a
test parses it to keep that true.
