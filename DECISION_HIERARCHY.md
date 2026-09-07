# DECISION_HIERARCHY.md

Which layer wins, and why it cannot be argued with. L60, 2026-09-06.

---

## The ordering

```
HARD SAFETY   safe mode, kill switches, an unhealthy execution path
     ↑
RISK          the RiskEngine's limits
     ↑
PORTFOLIO     exposure, concentration, drawdown, capital
     ↑
STRATEGY      lifecycle state, quarantine, health
     ↑
SIGNAL        models, AI, strategy signals
     ↑
OPTIMIZATION  allocators
     ↑
PREFERENCE    everything else
```

**A lower layer cannot relax a higher one**, and that is enforced by
construction rather than by a rule somebody has to remember. `decide()` returns
the most restrictive verdict present; it has no branch that could turn a RISK
`RESTRICT` back into `ALLOW` because a model was confident.

L60's own worked example, and it is a test:

```
AI:         ALLOW    "the model is confident this is a buy"
RiskEngine: REJECT   "portfolio drawdown limit breached"
---------------------------------------------------------
Decision:   REJECT, decided by RISK
```

`Layer` is an `IntEnum` so the comparison **is** the ordering, rather than a
lookup table that could drift from the diagram above. Every adjacent pair is
tested, so reordering the enum fails loudly.

## Missing is never safe

Every input carries its source, its timestamp and its version. Four states:

| | Meaning |
|---|---|
| `FRESH` | present, dated, within the max age |
| `STALE` | present and dated, but older than the decision can rely on |
| `MISSING` | **absent — including a `None` value with a good timestamp** |
| `INVALID` | undated, or dated in the future |

`require_fresh(...)` names the inputs a decision cannot be made without.
Anything named that is not `FRESH` produces a **HARD_SAFETY** finding, not a
warning — a warning that does not change the verdict is not "more
conservative".

### Why this rule is written down

This platform has already shipped the opposite. `PortfolioState.open_symbols`
was `frozenset[str]` defaulting to `frozenset()`, so "nobody told me what is
open" was indistinguishable from "nothing is open" — and
`one_position_per_symbol`, the only aggregate control enabled by default, could
not fire (L53). **An empty answer read as a positive claim.**

Two consequences of that lesson are encoded here:

* a `None` value is `MISSING` even with a perfect timestamp — *"we asked and
  got nothing"* is an absent fact, not a fresh one;
* a timestamp in the **future** is `INVALID`, not very fresh. It is a clock
  problem or a fabricated reading, and neither is evidence.

## The losing findings are kept

`Decision.findings` holds every finding, including the ones that did not win. A
record showing only the winner hides that risk and the optimizer disagreed,
which is the fact somebody needs after an incident.

Inputs that were degraded but not required are reported too: *"we decided while
the regime data was a day old"* is worth knowing even when it did not change
the answer.

## What this is not

**It decides nothing about orders.** It produces a recommendation and the
reasons for it. The RiskEngine remains the final veto, the OMS owns order
state, and the `authority` field says so on every serialised decision so the
claim travels with the data.

A test walks the syntax tree and fails if the module calls `place_order`,
`close_position`, `cancel_order`, `submit`, `approve`, `set_limits` or `create`,
or imports anything from `app.` outside its own package. A decision layer that
could submit an order would be the L45 C-2 defect at a higher altitude: a
component quietly assuming another's authority.

## What was not built

L60 asks for a `PortfolioDecisionEngine` that aggregates regime, correlation,
AI predictions, research findings and model health into a live decision.

**Those inputs do not exist yet.** There is no regime engine, correlation is
unavailable while `market_bars` covers one instrument, no AI provider has ever been called,
and the research layer produced its first module today. Wiring collectors to
them would be wiring to nothing.

What exists is the part that does not depend on them: **the hierarchy and the
freshness rule**. They are the properties that make an aggregator safe to build
later — and the ones that would be tempting to skip when the inputs finally
arrive and everything looks urgent.

`DEFAULT_MAX_AGE` is five minutes. That is an assumption, not a measurement:
nothing here has a measured staleness distribution, and it is recorded as a
configuration decision requiring approval.
