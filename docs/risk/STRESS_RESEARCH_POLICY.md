# STRESS_RESEARCH_POLICY.md

L67 sections 30 and 34. Gaps become research.

---

## The route

A resilience gap creates a research opportunity, which travels L64's pipeline:

    RESEARCH -> VALIDATION -> SHADOW -> GOVERNANCE -> CERTIFICATION

**Research remains non-executing.** `app/safety/policy_research.py` has no
importer anywhere under `app/`, asserted by a syntax-tree test, so research
cannot affect current trading because nothing reads it.

## What a stress result may propose

Section 34's example is the good one: *current policy causes excessive action
oscillation under volatility stress -- can cooldown or hysteresis be improved?*

Both `oscillation_limit` and `oscillation_window_hours` are on L64's
`RESEARCHABLE` allow-list, with bounds. So that hypothesis is expressible and
bounded today, which is the point of the allow-list existing before there was
anything to research.

What is **not** expressible: anything aimed at a risk ceiling, leverage,
certification or the autonomy level. Those are Category A -- not researchable,
not proposable, no approval path.

## Not built

Automatic opportunity generation from stress results. It needs stress results.
