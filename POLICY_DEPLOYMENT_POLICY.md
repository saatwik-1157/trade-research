# POLICY_DEPLOYMENT_POLICY.md

L64 section 29. Controlled deployment.

---

## The sequence

    APPROVED -> CERTIFY -> CANARY -> MONITOR -> FULL ACTIVE

Encoded in `PromotionState`. **Nothing reaches ACTIVE except from CANARY**, and
nothing reaches CANARY except from CERTIFIED. Asserted over the whole
transition table by `test_nothing_becomes_active_without_a_canary`, so a state
added later cannot quietly acquire the edge.

This is the same property L63 asserts for certification recovery, for the same
reason: the rule is broken not by a bad decision but by a table where the
shortcut exists and somebody takes it during an incident.

## Canary scope

Paper, shadow, a restricted strategy set, a limited account scope, a limited
allocation, a limited action scope. `app/brokers/shadow.py` is the existing
non-executing adapter and is where this belongs.

## What a canary never bypasses

RiskEngine, PortfolioRiskOrchestrator, OMS, BrokerAdapter, certification.

That holds without a canary-specific rule:
`test_only_the_oms_reaches_a_venue_to_write` (L62) covers a canary exactly as
it covers everything else, because it is a property of the whole codebase
rather than of any component.

## Not built

The deployment mechanism. There is no policy store to deploy into - the current
policy is a set of module constants, and `policy_version` is a hash of their
source. Deployment would mean editing code, which is a pull request, not a
runtime action. That is a limitation worth keeping rather than fixing: a policy
that can only change by code review is a policy no loop can change.
