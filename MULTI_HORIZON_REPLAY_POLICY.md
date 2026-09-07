# MULTI_HORIZON_REPLAY_POLICY.md

L65 sections 14 and 17. Time consistency.

---

## The control that exists

`app.datasets.leakage` - it refuses a feature computed from information that
did not exist at its timestamp, including the case where a scaler fitted on the
whole series leaks the future into every row without any single feature reading
forward. It predates L62 and is bound as INV-22's evidence.

`app/replay/` provides a clock and engine for market replay.

## What this module contributes

`synthesise()` takes `now` as a parameter and every expiry check is relative to
it. There is no call to a wall clock anywhere in the module, so replaying a
decision at a historical timestamp uses that timestamp throughout - it is not
a mode, it is the only way the function works.

## Strategic memory must not leak backwards

Section 17: long-term decisions may reference prior strategy behaviour, prior
regime behaviour and prior policy effectiveness - but historical outcomes must
not leak into past simulations.

The module holds **no history**. `synthesise()` is a pure function of the
recommendations handed to it, so there is no store from which a later outcome
could leak into an earlier decision. When history is added, this is the
property to preserve, and `app.datasets.leakage` is the check that already
knows how to test it.

## Not built

Reconstructing a historical multi-horizon decision. There are none - no
multi-horizon decision has ever been made. Building a replay engine now would
mean building it against imagined records, and validating it would mean
generating those records.
