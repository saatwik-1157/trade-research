# OPPORTUNITY_DISCOVERY.md — L83

`backend/app/research/opportunity.py` · 27 tests in `backend/tests/test_opportunity.py`

## What this is

A research-attention engine. It answers *"what deserves research now, and
why"*, and it is built so the answer is allowed to be **nothing**.

It has **no execution authority**. `test_the_module_cannot_reach_a_venue`
parses the module's import graph with `ast` and fails if `app.oms`,
`app.brokers`, `app.execution`, `app.positions` or `MetaTrader5` appears —
parsed rather than grepped, so a comment naming `app.oms` does not fail it and
an import hidden inside a function does not slip past it. §32 is enforced, not
asserted.

## The audit that shaped it

All eighteen engines named in L83 §2 were searched for and **none exists**:
`InvestmentOpportunityLifecycleEngine`, `EvidenceGraph`, `ThesisEngine`,
`ExpectationGapEngine`, `ChannelIntelligenceEngine`, `GuidanceIntelligenceEngine`,
`CompanyEarningsModel`, `ResearchMemory`, `AdaptiveInvestmentEdgeEngine`,
`HistoricalPatternEngine`, `RelativeValuationEngine`, `DecisionAttribution`,
and the rest. L83 is greenfield, contrary to the brief's expectation.

What *does* exist and is **reused**:

| Existing | Reused for |
|---|---|
| `app/research/selection.py` (263 lines) | §10 edge assessment — how many candidates clear by chance |
| `tools/edgar.py` (368) | fundamentals with accession-number provenance and period alignment |
| `tools/score.py` (256) | the cautionary precedent, not the mechanism |
| `tools/verify.py` | the sourcing rule `Evidence` enforces |

No duplicate engine was created.

## The two refusals that define the design

**1. Below 50% component coverage there is no score.** `priority_score` and
`confidence` are `None`, `INSUFFICIENT_DATA` is a blocking factor, and the
opportunity cannot be promoted. This follows `tools/score.py`'s existing rule —
a dropped component is reported through `coverage`, never defaulted to 50 —
because a neutral score is a claim, not the absence of one.

**2. `no_edge` is the default.** Edge rises only when
`app.research.selection` says more candidates cleared than a coin flip would
clear over the same grid, and even then only to `potential_edge`. Beating a
null is the weakest of the three gates. `CLAUDE.md` records six searches where
reporting only the winner would have looked like a discovery — a 36-cell sweep
where the **random** rule scored 1.76 against the best real candidate's 0.83, a
128-cell D1 grid that came in *below* chance, and a shape search whose best
in-sample t was −0.04 against a shuffled null averaging 0.37.

## What is deliberately not computed

L83 asks for expectation gaps, channel intelligence, guidance credibility and
historical base rates. **No data source exists for any of them** — no consensus
estimates, no channel panel, no guidance history, and no point-in-time
fundamental panel (`PROJECT_STATE.json`: 705 market bars across 2 symbols,
1 dataset).

These return `Verdict.insufficient_data` with the missing input named in
`data_gaps`, rather than a stub returning neutral. A base rate computed from a
panel that does not exist is the metals-points error in new clothes:
arithmetic on quantities that are not what they claim to be.

## What is computed

| §  | Capability | State |
|---|---|---|
| 5 | Novelty / dedupe | keyed on the **event**, not the detection run. Same filing twice = one opportunity. Different economic thesis on the same event = `thesis_changed`, not a duplicate |
| 6 | Materiality | earnings impact; under 2% is `no` and does not become a research task |
| 8 | Fundamental–price divergence | 6 cases. **Agreement is explicitly not an opportunity** — both rising is the ordinary state of a working business, and reporting it is how a screen fills with noise |
| 14 | Counter-thesis | never empty; always leads with "the market may already price this" |
| 18 | Freshness | measured from the **oldest** evidence, not the newest. A fresh price beside a two-year-old filing is not a fresh opportunity |
| 23 | False opportunity | 4 of the brief's 13 are computable: weak cash conversion, margin-up-on-falling-revenue, one-off-driven, cheap-and-deteriorating. **No flags with no inputs returns `unknown`, not `low`** — a check that could not run is not a clean bill of health |
| 10 | Edge | `no_edge` unless `selection` says otherwise |

## Provenance

`Evidence` refuses construction without a source, and refuses a naive
`observed_at`. The second is a look-ahead guard: two naive stamps from
different zones compare as though they shared a clock, which is how leakage
enters a base-rate calculation unnoticed.

## Not yet built

Persistence (§30 — 9 tables, migrations), the dashboard (§29), champion /
challenger (§26), research memory (§22), replay (§27), opportunity competition
(§17). `docs/OPPORTUNITY_REPLAY.md`, `THESIS_HEALTH.md`, `RESEARCH_VALUE.md`
and `EDGE_ATTRIBUTION.md` are deferred with them — a document describing a
mechanism that does not exist is the drift this repository already has 177
files of.
