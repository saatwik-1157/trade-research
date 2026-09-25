# PORTFOLIO_RESILIENCE_POLICY.md

L67 sections 12, 13 and 14. What stress established about robustness.

---

## Insufficient coverage is UNKNOWN, not HEALTHY

**The branch the module exists for.**

A portfolio nobody has stressed is not a robust portfolio. It is an unmeasured
one, and reporting it as healthy would be the fabrication the coverage matrix
is built to prevent.

`evaluate()` returns `UNKNOWN` when coverage is below the minimum, and
`ResilienceStatus.UNKNOWN.blocks_autonomy` is `True` -- exactly as hard as
`CRITICAL`, on the rule this platform now applies everywhere: unknown is not
healthy, missing is not permission, absent is not zero.

## Hard failure outranks the score

Section 13. Checked before anything is weighed. A weighted score cannot express
"this outranks everything", only "this counts for a lot".

## The status values

`HEALTHY` `WATCH` `DEGRADED` `CRITICAL` `UNKNOWN` -- and two of the five block
autonomy.

## Recovery time

Section 14 asks for `TIME_TO_RECOVERY`, distinguishing **simulated** recovery
from **actual** recovery.

Not built. Simulated recovery needs a simulation that runs; actual recovery
needs a drawdown that happened and a portfolio that came back. This platform
has 252 closed trades totalling +3.44 USD on a demo account and one open
position.

The distinction is recorded here because it is the part that will be tempting
to blur: a simulated recovery time reported without its label reads as a
measurement.

## Not built

The resilience score's dimensions -- stress loss, exposure concentration,
margin pressure, liquidity sensitivity, strategy dependency. Each is a
cross-instrument or multi-strategy measurement, and this deployment has one
instrument and one position.
