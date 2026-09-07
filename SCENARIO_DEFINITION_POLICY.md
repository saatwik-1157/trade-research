# SCENARIO_DEFINITION_POLICY.md

L66 sections 6, 7 and 38. What a scenario is.

---

## Bounded inputs

Section 6: no arbitrary unsafe values.

| Input | Min | Max |
|---|---|---|
| `volatility_pct` | -50 | 500 |
| `correlation_pct` | -100 | 100 |
| `spread_pct` | 0 | 1000 |
| `liquidity_pct` | -95 | 100 |
| `drawdown_pct` | 0 | 100 |
| `margin_pct` | -95 | 100 |
| `slippage_pct` | 0 | 1000 |
| `latency_ms` | 0 | 60000 |

**An unrecognised input is refused**, not passed through. An input nobody
bounded is an input nobody thought about -- the same allow-list polarity L64
uses for researchable parameters.

The bounds are configuration decisions, not measurements. Nothing has ever been
stressed on this platform, so there is no observed distribution they were
fitted to. **They require approval.**

## A scenario is never defined against live

The check that would be easy to leave out. A scenario is a simulation, so
running one against `live` looks harmless -- and a simulation naming a live
account is one somebody will eventually read as a statement about live
behaviour.

## A historical scenario must name its data version

Without one the run cannot be reproduced and cannot be checked for leakage.
Section 37 asks for reproducibility; this is the part of it that can be
enforced at definition time rather than promised.

## Fingerprint

Covers kind, account, environment, every input, and **every version** -- data,
model, policy, seed.

Excludes the id and the name: two definitions differing only in what they are
called are not two scenarios. Includes the versions: the same inputs against
different data have not actually been run, so a version change makes it a new
question rather than a duplicate.
