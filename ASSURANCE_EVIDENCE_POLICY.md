# ASSURANCE_EVIDENCE_POLICY.md

What counts as evidence. L63 sections 5, 6 and 19, 2026-09-06.

---

## The rule

**Evidence is a pointer to a source of truth, never a copy of it.**

The invariant registry (L62) already works this way: it names the module that
enforces each invariant and the test that proves it, and a test resolves both.
It stores no results. That is what makes it a registry rather than a second
system that can disagree with the first.

## Freshness

Evidence has a 1-day TTL against the certificate's 30-day one. The certificate
may be a month old; what it rests on may not.

Missing evidence is `DEGRADED`, not `HEALTHY`. No evidence timestamp means the
certification cannot be shown to rest on anything current, and unknown is not
healthy.

## The twelve dimensions

Risk · Capital · Execution · Data · Strategy · Model · Portfolio · Policy ·
Security · Operations · Recovery · Control Loop.

Statuses: `HEALTHY` `WATCH` `DEGRADED` `CRITICAL` `UNKNOWN`.

**`UNKNOWN` blocks autonomy exactly as hard as `CRITICAL`.** Any softer
treatment would make not-measuring the cheapest way to stay certified.

## What is NOT built

**The evidence engine that would populate those dimensions.**

Section 5 asks for collection from the RiskEngine, OMS, BrokerAdapter, MT5,
StrategyEngine, Model Registry, monitoring, recovery and the control-loop logs.
Those systems exist and emit real data — but none of them emits
*autonomous-control* evidence, because there is no autonomous control loop.

A snapshot assembled anyway would be a document with an `evidence_hash` and an
`assurance_score` computed from nothing, which is the most convincing possible
form of the fabrication this project forbids.

`assess()` takes dimensions as a **parameter** for exactly this reason: a caller
with real evidence supplies it, and there is no path by which the module invents
one.

## Trends

Section 19 asks for improving / stable / deteriorating / volatile / unknown.
Not built: one data point is not a trend, and the platform has one.

Section 19 also says trends are advisory and must never override hard safety
rules. That property holds trivially today, and is worth recording for when
they exist.
