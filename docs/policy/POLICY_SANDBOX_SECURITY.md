# POLICY_SANDBOX_SECURITY.md

L64 sections 9 and 33. Why there is no sandbox.

---

## The claim

**There is no sandbox because there is nothing to sandbox.**

Section 9 requires a sandbox preventing broker access, MT5 access, filesystem
abuse, secret access, arbitrary network access, production database mutation,
live trading, policy deployment and monitoring disablement.

A `Candidate` is `dict[str, Decimal]`. Every item on that list is unreachable
from a dictionary of decimals — not blocked, unreachable. A sandbox would be a
guard posted at a door that does not exist, and its presence would imply the
door does.

## What is asserted instead

`test_a_candidate_cannot_reach_a_venue_because_it_is_not_code` parses the
module and fails if it calls `eval`, `exec`, `compile`, `__import__`, `open`,
`globals`, `locals`, `system`, `popen`, `run` or `loads`, or any adapter write
method.

`test_research_being_unavailable_leaves_the_certified_policy_authoritative`
walks every module under `app/` and asserts none imports `policy_research`. The
research layer is not merely restricted from production — it is not connected
to it.

That test originally grepped the text and failed the moment the invariant
registry *named* the module in a string. It now walks the syntax tree: **match
the structure, never the characters**, the same lesson as C-4 and the L57
lifecycle guard.

## If a candidate ever becomes executable

It should not. If a future level needs expressions rather than numbers, the
sandbox question returns in full and this document stops applying. The cheapest
safety property in this module is that `changes` is typed `dict[str, Decimal]`,
and it should be defended rather than relaxed.

## Secrets, network, filesystem

Nothing in `app/safety/` reads a credential, opens a connection or touches the
filesystem. The package import guard (L62) limits it to
`app.portfolio.control`, `app.portfolio.decision` and its own modules.
