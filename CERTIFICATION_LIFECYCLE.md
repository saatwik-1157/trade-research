# CERTIFICATION_LIFECYCLE.md

Certification as something that can go stale. L63 §3, §4 and §9, 2026-09-06.
Source: `backend/app/safety/lifecycle.py`.

---

## Why this exists

L62's `certify()` is a pure function of the invariant registry, so it returns
the same answer forever. That is correct as far as it goes and it is not
enough: **a certification with no expiry is a claim about a moment presented as
a claim about now.**

## Two clocks, not one

| | Default | What it means when it runs out |
|---|---|---|
| Certification TTL | 30 days | `CERTIFICATION_EXPIRED` — the certificate is old |
| Evidence TTL | 1 day | `DEGRADED` — what it rests on is old |

Deliberately different, and the shorter one is the evidence. The certificate
may be a month old; the facts under it may not. They are different failures and
they carry different severities.

Both are **configuration decisions, not measurements** — nothing has ever been
recertified here, so there is no observed drift rate to fit to. Recorded as
assumptions requiring approval.

## States

`NOT_CERTIFIED` · `CONDITIONALLY_CERTIFIED` · `CERTIFIED` · `MONITORED` ·
`DEGRADED` · `SUSPENDED` · `CERTIFICATION_EXPIRED` · `CERTIFICATION_REVOKED` ·
`REVALIDATION_REQUIRED` · `REVALIDATING`

L62's five are reused verbatim. L63 adds the five that only mean something once
certification can decay.

`MONITORED` rather than `CERTIFIED` is the healthy steady state, deliberately:
a certification that is being watched is a different claim from one that was
issued and filed.

## The one edge that matters

**No state reaches `CERTIFIED` except from `REVALIDATING`.**

Asserted over the whole transition table, not at the states somebody thought to
check. `CERTIFICATION_REVOKED` goes to `REVALIDATION_REQUIRED` and nowhere
else, so a revoked certification cannot be quietly resumed.

## Order of assessment, worst first

1. **A critical invariant failed** → `CERTIFICATION_REVOKED`, and nothing else
   is consulted. §8 says hard safety failures override numerical scores, and
   the way that rule gets broken is a weighted average where CRITICAL is worth
   −3. There is no score here for a failure to be the largest term in.
2. Never certified → the base state.
3. Certificate expired → `CERTIFICATION_EXPIRED`.
4. Evidence stale or missing → `DEGRADED`.
5. A dimension `CRITICAL` **or `UNKNOWN`** → `SUSPENDED`.
6. A dimension `DEGRADED` → `DEGRADED`.
7. Otherwise → `MONITORED`.

## UNKNOWN is not HEALTHY

`AssuranceStatus.UNKNOWN.blocks_autonomy` is `True`, exactly like `CRITICAL`.

§7 says so, and this platform has been bitten by the opposite:
`PortfolioState.open_symbols` defaulted to an empty frozenset, so "nobody told
me what is open" and "nothing is open" were the same fact, and the one
aggregate risk control enabled by default could not fire.

A dimension nobody could measure is not a dimension that is fine.
