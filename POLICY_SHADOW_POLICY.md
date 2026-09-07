# POLICY_SHADOW_POLICY.md

L64 section 17. Shadow evaluation.

---

## The requirement

Run a candidate alongside the current policy. The current policy stays
authoritative. Record current decision against candidate decision. **Shadow
policies must never execute real trades.**

## The pattern already exists

`app/brokers/shadow.py` is a non-executing adapter with its own test suite
(`tests/test_shadow.py`), built at L44. It is the right thing to extend, and it
is named here so it is not rebuilt.

`SHADOW` is a required stop in `PromotionState`: a candidate cannot reach
`PROMISING` without passing through it, and cannot reach `CERTIFIED` from it
directly.

## Not built

## Not built, and the reason is the same one

This needs **a governance policy that has actually run** to produce the data it
would evaluate. There is no policy deployment history, no autonomous action
history and no effectiveness record on this platform.

`CLAUDE.md` spends several hundred lines on what happens when a search is run
against insufficient data: 41 candidates, 128 combinations, 200 combinations,
five universes, and in every case the winner did not survive out-of-sample.
Running that machinery over an empty sample would not be a smaller version of
that mistake, it would be a purer one.

A shadow run needs two policies and a stream of decisions to run both over.
There is one policy, it is a pure function over module constants, and no
decisions have been made. The comparison would have an empty input and its
divergence rate would be a number computed from nothing.
