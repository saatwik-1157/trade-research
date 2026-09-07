# Level 81 — execution readiness audit

Step 1 of the level's own order: audit before modifying. Nothing was modified.

This is the sixth audit in the 74→81 chain and the first with a genuinely good
answer, because Level 81 is about things this platform actually has.

---

## The finding

**Level 81's safety layer is largely built and tested. Its research inputs are
not.**

Levels 74–80 asked for a research system that does not exist, so their audits
kept returning the same blocking answer. Level 81 is different: it is about
capital reservations, approvals, broker state, autonomy, safe mode and the
RiskEngine boundary — **all of which are real, running and covered by tests**.

What it cannot have is the *research* half of its `ExecutionReadinessContext`.

---

## Already implemented, with the tests that prove it

### §11 capital reservation readiness — BUILT

`app/risk/reservations.py`, and its docstring states the invariant in the
level's own terms:

> `conserves_budget` is the invariant an allocator must satisfy: **the parts may
> never exceed the whole.** L55 states it as a mandatory safety test — an
> approved budget of 10,000 with a recommendation of 15,000 must be REJECTED.

`tests/test_capital.py` carries **18 tests**, and they line up with Level 81's
mandatory list almost one for one:

| L81 requirement | Existing test |
|---|---|
| §38 #8 — budget 10,000, proposal 15,000 → REJECT | `test_an_allocation_over_the_approved_budget_is_rejected` |
| §11 — never double reserve | `test_reserving_the_same_intent_twice_reserves_once` |
| §38 #25 — two concurrent decisions, same capital | `test_two_callers_that_both_saw_no_reservation_still_make_one`, `test_the_unique_index_is_what_prevents_the_double_claim` |
| §11 — reservation states incl. EXPIRED, RELEASED | `test_releasing_frees_the_budget_and_records_why`, `test_an_expired_reservation_stops_holding_without_a_sweep` |
| §31 — fail closed on uncertain state | `test_a_reservation_outlives_the_process_that_made_it` |

That last one is worth reading. The module exists *because* the process-local
dict was empty after a restart — "an approval that reserved budget and had not
filled when the process died releases nothing, and the next process believes the
whole budget is free." Level 81 §12 asks for exactly that reconciliation.

### §21, §22, §45 autonomy and approval — BUILT

`app/safety/autonomy.py` — `AutonomyLevel(IntEnum)`: `OBSERVE(0) < RECOMMEND(1)
< APPROVAL(2) < BOUNDED(3) < CERTIFIED(4)`, with `may_auto_apply` true only at
BOUNDED and above. An `IntEnum` so the comparison *is* the ordering — the same
reasoning `portfolio.decision.Layer` uses, and its docstring says so.

Approvals appear in **48** modules; safe mode in **20**; reconciliation in
**81**.

### §45 safety invariants — BUILT AND TESTED

| Invariant | Existing test |
|---|---|
| #1 RiskEngine is the final veto | `test_the_oms_cannot_be_reached_without_an_approval` (`test_risk.py`) |
| #2–#5 no AI/research/portfolio → MT5 | `tests/test_integration.py:632`, plus containment tests that parse for `MetaTrader5` imports |
| #6 no capital expansion | `conserves_budget` + `test_capital.py` |
| #9 no live enablement | `test_the_ai_layer_cannot_enable_live...` (`test_ai_integration.py`) |
| #10 unknown broker state → reconcile | the OMS parks `unknown` and never retries |
| #16–17 AI cannot modify safety or self-approve | `app/ai/decision.py`, `eligibility.py` |
| #25 paper/live defaults | `LIVE_GATES` ×10 all False, `assert_demo` |

**Rewriting any of these would be the duplication §1 forbids.**

### §6 decision freshness — PARTIALLY BUILT

`app/portfolio/decision.py::Freshness` now carries six states — `FRESH`,
`AGING`, `STALE`, `CONFLICTED`, `MISSING`, `INVALID` — with `AGING` and
`CONFLICTED` added under Level 76.

**The gap §6 names precisely: one universal threshold.** `DEFAULT_MAX_AGE` is a
single five-minute value for every input, and §6 is explicit that market price
needs seconds while quarterly fundamentals stay valid for weeks. `freshness()`
already takes `max_age` per call, so the mechanism is there and the *policy* is
not. **This is the one genuinely buildable gap in the level.**

---

## Confirmed absent

Everything in §3's `ExecutionReadinessContext` that comes from research:
`thesis_state`, `thesis_health`, `evidence_quality`, `earnings_state`,
`guidance_state`, `valuation_state`, `channel_state`, `expectation_state`,
`catalyst_state`, `certification_state`, `waiting_value`, `margin_of_safety`,
`opportunity_competition`, `invalidation_conditions`.

Also absent: `WaitingValueEngine` (§9), `OpportunityCostEngine` (§10),
`ThesisDriftEngine` (§7), `PortfolioControlEngine`, capital-preservation
postures (§19 — `CAPITAL_PRESERVATION` and `DEFENSIVE` match nothing).

So gates **B, C, D, E** (research, earnings, valuation, edge) have no inputs.
Gates **A, F, G, H, I, J** (data, portfolio, capital, risk, execution,
governance) are built or nearly so.

---

## Classification

**KEEP + REUSE, do not duplicate:** `app/risk/` (including `reservations.py`),
`app/safety/`, `app/oms/`, `app/sizing/`, `app/brokers/`, `app/portfolio/`,
`app/ai/decision.py`, `app/ai/eligibility.py`, and every test named above.

**KEEP + MODIFY — buildable now:**

| Component | Change |
|---|---|
| `app/portfolio/decision.py` | Per-input-kind freshness policy (§6). The mechanism exists; the policy does not |
| `app/safety/` | Capital-preservation postures (§19), if the portfolio posture is to be explicit |

**ADD — blocked on Levels 74–80:** readiness gates B/C/D/E, thesis drift,
waiting value, opportunity competition, the readiness scorecard's research
dimensions, decision replay of research snapshots.

**REMOVE:** nothing, in any of the six audits.

---

## Honest status

Per §46, using the level's own vocabulary:

    LEVEL_81_BLOCKED

Not because the safety work failed — it largely predates this level and passes
— but because §46 forbids claiming completion when critical inputs are absent,
and Level 81's own §38 mandates tests 3, 4, 5, 6, 15, 16, 17, 18, 19, 20 and 31
(evidence conflict, thesis invalidation, guidance reduction, valuation extremes,
AI-fabricated evidence, compounder and value-trap handling, waiting outcomes,
fragility) against objects that do not exist.

**What can honestly be said:**

* Safety invariants #1–#17 and #25: **verified against existing tests.**
* §38 tests 8, 25, 26, 37–40 (capital, concurrency, account isolation, the
  bypass attempts): **already covered.**
* §38 tests 9–14 (broker unknown, MT5 disconnected, RiskEngine rejects/
  unavailable, expired approval): **mechanisms exist**; a readiness-layer
  wrapper for them does not.
* The rest: **blocked**, and building empty gates would produce a scorecard of
  `INSUFFICIENT_DATA` rows — the failure every level from 74 onward forbids.

**Recommended next build, unchanged and now supported by six independent
passes:** the per-input freshness policy (§6) is small and real; everything
else waits on Level 74's research objects, and the ablation harness remains the
one measurement that can be taken today.
