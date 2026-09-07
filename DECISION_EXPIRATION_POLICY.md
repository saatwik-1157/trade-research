# DECISION_EXPIRATION_POLICY.md

L65 section 29. Validity windows.

---

## The windows

| Horizon | Default validity |
|---|---|
| 0 | EMERGENCY | `HARD_SAFETY` (7) | 0:05:00 | yes |
| 1 | IMMEDIATE_RISK | `RISK` (6) | 0:15:00 | yes |
| 2 | SHORT_TERM | `STRATEGY` (4) | 4:00:00 | no |
| 3 | MEDIUM_TERM | `PORTFOLIO` (5) | 2 days, 0:00:00 | no |
| 4 | LONG_TERM | `OPTIMIZATION` (2) | 14 days, 0:00:00 | no |

Configuration decisions, not measurements. No multi-horizon decision has ever
been made here, so there is no observed rate at which these go stale. **They
require approval.**

## An undated recommendation is expired

`HorizonRecommendation.expired()` returns `True` when there is no timestamp.

Not eternal, not "assumed current". A recommendation with no timestamp cannot
be shown to be about now, and unknown is not fresh - the same rule
`decision.Input.freshness` applies to evidence, and the same rule L53's
`open_symbols` defect violated.

## Except an emergency

An emergency recommendation is **never dropped for being stale**. A stale
emergency is a reason to look at it, not a reason to discard it - so it stays
live and the staleness becomes something a person sees rather than a silent
removal.

This is the one asymmetry in the expiry rule and it is deliberate.

## Expired recommendations do not execute

They are separated from the live set before synthesis and reported in
`MultiHorizonDecision.expired`, so an expired recommendation is visible rather
than absent.
