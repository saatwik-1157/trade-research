# LIQUIDITY_STRESS_POLICY.md

L66 section 25. Spreads, slippage and depth.

---

## Bounded inputs exist

`spread_pct`, `liquidity_pct` and `slippage_pct` are bounded scenario inputs. A
value outside the bound is refused before anything runs.

## Simulation inputs only

Section 25's emphasis: results feed the OMS, the position sizer and the broker
adapter **only as simulation inputs**, never as configuration.

They cannot do otherwise. The module imports none of those components, so there
is no path by which a stressed spread becomes a real one.

## Do not invent liquidity data

Section 22 of L65 and section 25 here both say it. This platform has no depth
data and no recorded slippage distribution beyond what `CLAUDE.md` documents
from `tools/` -- and that documentation is emphatic about the trap:

> A single live quote can understate this broker by 3-8x, and bars recording 0
> are unrecorded rather than free -- averaging them in halves the apparent cost
> of trading.

A liquidity model fitted to that would inherit the error. The bounds are stated
as configuration requiring approval, not as measurements.

## Not built

The simulation itself. It needs a spread and depth series across instruments,
and `CLAUDE.md` records that the honest spread source is `--spread-source
median` over recorded per-bar spreads -- which exist in `tools/`, for the
research pipeline, not in the platform database.
