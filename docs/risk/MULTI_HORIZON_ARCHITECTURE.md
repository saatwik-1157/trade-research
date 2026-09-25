# MULTI_HORIZON_ARCHITECTURE.md

Decisions that reason over different time scales. L65, 2026-09-06.
Source: `backend/app/portfolio/horizon.py`.

---

## Horizon is not a second copy of Layer

The audit's first question was whether L60's `Layer` already does this. It does
not, and the distinction is the design:

- **`Layer` answers *who says so*** - HARD_SAFETY, RISK, PORTFOLIO, STRATEGY,
  SIGNAL, OPTIMIZATION, PREFERENCE.
- **`Horizon` answers *over what period*.**

They are orthogonal. A long-horizon view from RISK and a long-horizon view from
OPTIMIZATION carry different authority despite sharing a time scale.

Because they are orthogonal they must **compose, not compete**. Two independent
orderings resolving the same conflict is the failure this repository keeps
naming: the one that disagreed silently would be the one nobody read.

So `synthesise()` maps each horizon onto a `Layer` and **delegates the entire
resolution to L60's `decide()`**. There is no second resolver, and
`test_horizon_does_not_resolve_anything_itself` parses the module to assert
`decide` is the function actually called and that no `SEVERITY`, `decide()` or
risk engine has grown inside it.

## The mapping

| # | Horizon | Enters `decide()` as | Default validity | Safety? |
|---|---|---|---|---|
| 0 | EMERGENCY | `HARD_SAFETY` (7) | 0:05:00 | yes |
| 1 | IMMEDIATE_RISK | `RISK` (6) | 0:15:00 | yes |
| 2 | SHORT_TERM | `STRATEGY` (4) | 4:00:00 | no |
| 3 | MEDIUM_TERM | `PORTFOLIO` (5) | 2 days, 0:00:00 | no |
| 4 | LONG_TERM | `OPTIMIZATION` (2) | 14 days, 0:00:00 | no |

`Horizon` is an `IntEnum` where **lower wins**, deliberately inverted relative
to `Layer` where higher wins. They are different questions, and sharing a
direction would invite reading one as the other.

## What actually decides an outcome

**The verdict, not the layer.** `decide()` takes the most restrictive verdict
present and uses layer only to break a tie.

This matters, and the first version of a test here got it wrong. The obvious
assertion is that `LAYER_OF` decreases monotonically with horizon. It fails:
MEDIUM_TERM maps to PORTFOLIO(5) while SHORT_TERM maps to STRATEGY(4), because
medium-term concerns -- allocation, concentration, correlation -- genuinely are
portfolio concerns, and `Layer` names the *kind* of concern rather than its
urgency.

It does not matter, because a medium-term RESTRICT beats a short-term ALLOW on
**severity**, which is exactly what section 44's TEST 4 asks for. The layer
ordering only chooses which reason gets named when two horizons say the same
thing.

The property that must hold is narrower: **the two safety horizons own the two
safety layers, and nothing else may borrow one.**

## What horizon adds that a single-shot resolver cannot express

**Deferral.** `decide()` picks a winner. Section 8 requires that a long-term
recommendation losing to a short-term constraint is *kept with its reason*,
because the thing that displaced it is a condition, and conditions pass.

**Expiration.** A recommendation is about the moment it was made. **An undated
recommendation is expired, not eternal** - it cannot be shown to be current,
and unknown is not fresh.

**Revalidation.** Five gates re-asked before a deferred recommendation applies:
expiry, the deferral condition, certification, risk, data freshness. A
recommendation is never applied as written against a world that has moved.

**Hysteresis.** A minimum change threshold - **and never applied to a safety
horizon.** A smoothing rule that also smoothed the emergency stop would be the
L61 lesson repeated: a guard that suppresses the response to the thing it
guards against.

## Two things a safety horizon never does

It is **never parked in the deferred queue** - a deferral is a wait, and safety
does not wait. And it is **never dropped for being stale** - a stale emergency
is a reason to look, not a reason to discard.

## What is not built

**The contexts themselves.** Sections 4, 5 and 6 ask for short-, medium- and
long-term context objects aggregating positions, margin, liquidity, strategy
health, correlations, regime persistence, capital efficiency and research
priorities.

Those read a running portfolio. This platform has **one open paper position**,
no connected broker and seven bots that are test artefacts with no strategy
version. (An earlier draft of this paragraph said zero open positions; that was
asserted rather than measured, and was wrong.) Context objects built over that would be full of `UNKNOWN` -- and by
this module's own rules (section 12: UNKNOWN is not high confidence; section
13: missing critical data blocks a new entry) the correct output would be "no
autonomous adaptation", which is what the platform already does.

Building them would produce a decision object whose every field said "not
known", wrapped in machinery that made it look measured.

