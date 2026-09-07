# POLICY_RESEARCH_ARCHITECTURE.md

Researching governance policy without being able to change it. L64, 2026-09-06.
Source: `backend/app/safety/policy_research.py`.

---

## The design decision the level rests on

**Research operates on an ALLOW-LIST. Everything else in the safety package
uses blocklists, and that difference is deliberate.**

L62's `ENVELOPE_TARGETS` and L63's `GOVERNANCE_TARGETS` name what an autonomous
*action* may not touch. That polarity is right there: an action's targets are
known when the action is written.

It is exactly wrong for research. A blocklist means *everything is researchable
unless forbidden* — so a safety parameter added next month would be
researchable by default, by nobody's decision. `categorise()` inverts it:

> **Anything unrecognised is `HARD_SAFETY`.**

`max_portfolio_risk`, `risk_engine_enabled` and `live_trading` are refused not
because somebody remembered to forbid them, but because nobody authorised them.
`test_anything_unrecognised_is_hard_safety` proves it with two parameter names
invented for the test.

Capability-checked: flipping the default to `GOVERNANCE` fails twelve tests.

## Three categories

| Category | May be researched | May be optimized |
|---|---|---|
| **A — HARD_SAFETY** | no | no |
| **B — GOVERNANCE** | yes, as a proposal | no, a person approves |
| **C — PREFERENCE** | yes | yes, inside bounds |

Category A has **no approval path**. It is not reviewable, not proposable and
not optimizable — stronger than requiring sign-off, and the same shape L61 used
for FORBIDDEN actions.

## The allow-list

| Parameter | Category | Bounds | Why it is a judgement call |
|---|---|---|---|
| `oscillation_limit` | GOVERNANCE | 2 – 6 | how many direction changes on one target count as thrashing. L61 set it at 3 by reasoning, not measurement, and it is the kind of number a record of real control-loop behaviour would settle |
| `oscillation_window_hours` | GOVERNANCE | 0.5 – 24 | the window direction changes are counted over |
| `evidence_ttl_hours` | GOVERNANCE | 1 – 72 | how old the evidence behind a certification may be. Lower is stricter |
| `certification_ttl_days` | GOVERNANCE | 1 – 90 | how long a certification stands before renewal |
| `medium_violations_before_downgrade` | GOVERNANCE | 2 – 10 | how many medium violations constitute a pattern rather than noise |
| `diversification_preference` | PREFERENCE | 0 – 1 | how much the allocator prefers spread over concentration |
| `stability_preference` | PREFERENCE | 0 – 1 | how much churn the allocator will accept for a better fit |
| `regime_preference` | PREFERENCE | 0 – 1 | how strongly regime classification weights allocation |

Deliberately short. **A generous allow-list is how a hard limit becomes
researchable**, so a test caps it at twelve entries and requires each to
explain itself. The bounds are configuration decisions, not measurements — no
governance policy has ever run here — and require approval.

The bounds are themselves Category A: a candidate proposing to change one is
proposing to change the rules it is judged by.

## A candidate is data, never code

Section 9 asks for a sandbox preventing broker access, filesystem abuse, secret
access and arbitrary network access from generated policy.

**The stronger answer is not a better sandbox.** A `Candidate` is
`dict[str, Decimal]`. There is no expression, no callable, no source string and
no interpreter for one, so section 8's "never execute generated policy code"
holds because there is nothing executable to run. Every item on section 9's
list is unreachable from a dictionary of decimals.

`test_a_candidate_cannot_reach_a_venue_because_it_is_not_code` asserts the
module calls no `eval`, `exec`, `compile`, `__import__`, `open`, `system`,
`popen`, `run` or `loads`.

## Safety dominates lexicographically, not by weight

Section 18: "a policy with better performance but worse safety must lose."

A weighted sum cannot deliver that. **A weight is a price** — with a big enough
gain elsewhere the sum tips. `Score.ordering()` compares safety first and only
then anything else, so the rule is true by construction rather than by tuning.
Same move L60 used to stop a lower layer relaxing a higher one.

A tie goes to the incumbent: a running policy has evidence a candidate does not.

## Nothing reads this module

`test_research_being_unavailable_leaves_the_certified_policy_authoritative`
walks the syntax tree of every module under `app/` and asserts none imports
`policy_research`. So section 40's TEST N — AI unavailable, existing certified
policy remains authoritative — holds in its strongest form: research stopping
changes nothing about what the platform does.

## What was reused, not rebuilt

**`app/research/selection.py`** — the L58/L59 multiple-testing gate — covers
sections 25 and 26. It was validated against this repository's own recorded
searches (`CLAUDE.md` reports 41 candidates clearing ~1 by chance, 128 clearing
3.2, 200 clearing 4.9, and the gate reproduces all three). Rebuilding it would
have been a second implementation of the one piece of research machinery this
project has already proven against real data.

**`app/brokers/shadow.py`** — the non-executing shadow adapter, named as the
thing to extend when there is something to shadow.

## What was declined

Candidate *generation*, historical replay of policies, counterfactual
simulation harness, walk-forward policy evaluation, shadow policy runs, policy
effectiveness measurement, the challenger registry, research memory, compute
limits, database tables, APIs, and the frontend research dashboard.

Every one needs **a governance policy that has actually run** to produce the
data it would evaluate. There is no policy deployment history, no action
history and no effectiveness record, because no autonomous action has ever been
applied on this platform.

Generating candidates and scoring them against nothing would produce a ranked
list with confidence intervals over an empty sample — which `CLAUDE.md` spends
several hundred lines explaining is exactly how this project's own strategy
searches manufactured winners that did not survive contact with out-of-sample
data.

What exists is the half that must be right **before** the first candidate is
generated: what may be researched at all, and what a candidate is allowed to
be.
