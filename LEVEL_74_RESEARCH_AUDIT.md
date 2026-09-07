# Level 74 — research audit

Step 2 of the level: inspect before building, classify what exists, and do not
create a second research tool.

Everything below was read, not assumed. Where a claim is an inference rather
than something I confirmed in a file, it says so.

---

## The headline finding

**Most of what Level 74 asks for already exists in this repository. It was built
for trading rules, and it has never been pointed at company research.**

Level 74 reads as a request for fifteen new subsystems. It is mostly not. The
selection-bias gate, the leakage detector, the point-in-time clock, the
train/validation/out-of-sample splits, the clustered t-statistic, the
calibration measures, the model registry, the evidence-kind taxonomy and the
process-versus-outcome separation are all built, tested and running — against
`bracket_outcome == "WIN"` on FX bars, not against a company.

So the shape of this level is **not** "build an evidence graph". It is:

1. **Reuse** the trading side's methodology for equity research. Nine of the
   most valuable requirements are satisfied by modules that already exist.
2. **Extend** the three-kind evidence taxonomy to the eight kinds §4 names.
3. **Add**, genuinely, the fundamental layer: earnings model, guidance,
   consensus, peers, relative valuation, claims, thesis, counter-thesis,
   reasoning verification, prediction/outcome tracking.

The second-largest finding is a warning about this level's own premise, and it
is in [Where this level could go wrong](#where-this-level-could-go-wrong).

---

## What already exists, and what it satisfies

### The methodology layer — built for trading, reusable as-is

| Module | What it does (read, not assumed) | L74 section |
|---|---|---|
| `backend/app/research/selection.py` | How many candidates clear by chance; the "best result discovered ≠ robust result" gate | **§46** multiple testing |
| `backend/app/datasets/leakage.py` | Behavioural leakage detection — recompute with future bars appended and require no row changes | **§44** no look-ahead |
| `backend/app/datasets/splits.py` | Train / validation / out-of-sample splits | **§47** overfitting |
| `backend/app/replay/clock.py` | Replay against a controlled clock | **§45** point-in-time |
| `backend/app/validation/statistics.py` | Date-clustered t, Brier, reliability bins, ECE, Benjamini-Hochberg, Welch | **§46, §51** |
| `backend/app/monitoring/stats.py` | Calibration measures | **§51** confidence calibration |
| `backend/app/review/checks.py` | Deterministic assessors; **four of five cannot see the outcome** | **§74** decision quality |
| `backend/app/review/contract.py` | `OBSERVED / INTERPRETED / HYPOTHESIS` — a claim cannot be built without a kind | **§4** taxonomy |
| `backend/app/ai/decision.py`, `eligibility.py` | The AI layer's answer carries no authority | **§64** AI role |
| `backend/app/ai/registry.py`, `lifecycle.py` | Model versioning and lifecycle | **§63** versioning |
| `backend/app/datasets/quality.py` | Dataset quality gates | **§3** data quality |
| `backend/app/notifications/` | NotificationService | **§69** alerts |
| `backend/app/journal/` | Trade journal, timeline, quality | **§2** journal |

`review/checks.py` deserves particular note. Its docstring states that four of
its five assessors *take a `DecisionContext` and nothing else* — so "a losing
trade can have a high-quality entry" is not a guideline the code follows, it is
a property of what the code was given. **That is §74's four-quadrant
process/outcome separation, already enforced structurally.** Rebuilding it for
research would be the exact duplication this level forbids.

### The research pipeline — real, and much thinner

| Module | What it does | Verdict |
|---|---|---|
| `tools/edgar.py` | SEC XBRL annual facts, period-aligned, with accession numbers; `recent_filings`, `fundamentals` | **KEEP** — this is the PRIMARY source layer §3 wants |
| `tools/score.py` | Five components: technical, risk, quality, valuation, analyst | **KEEP + MODIFY** — see below |
| `tools/snapshot.py` | Builds the JSON contract the agents consume | **KEEP + MODIFY** — must carry provenance |
| `tools/verify.py` | Checks every number in a note against the snapshot | **KEEP + EXTEND** — §29's core work |
| `tools/backtest.py` | Point-in-time forward-return test of the composite | **KEEP** — already point-in-time |
| `tools/indicators.py`, `market.py`, `patterns.py` | Price-derived inputs | **KEEP**, untouched |
| `.claude/agents/` | technical, fundamental, sentiment, risk, thesis | **KEEP + MODIFY** — a thesis agent exists |

### Confirmed absent

Searched across `tools/` and `backend/app/`, zero matches in any
equity-research sense:

* **guidance** — 0 files. §16, §17 are genuine ADDs.
* **transcript** — 0 files. §3's "official transcripts" tier has no ingestion.
* **DCF** — 0 files. §24.
* **earnings_surprise** — 0 files. §18.
* **counter_thesis** — 0 files. §36.
* **peer** — 5 matches, every one a false positive (`TypeError, ValueError`
  tuples and a socket address in `tv_webhook.py`). There is **no peer set, no
  peer selection and no relative valuation**. §19–§21 are genuine ADDs.
* **consensus** — 3 matches, all in `tools/score.py::analyst_score`, which reads
  a vendor's `recommendationMean` and target price directly. There is no
  calibration of it. §25, §26 are genuine ADDs.

---

## Classification

### KEEP — do not touch

`tools/indicators.py`, `tools/market.py`, `tools/patterns.py`,
`tools/edgar.py`, `tools/backtest.py`, and the whole trading side of `backend/`
(`oms/`, `risk/`, `brokers/`, `execution/`, `positions/`, `sizing/`, `safety/`,
`live/`). Level 74 touches none of it, and §50 requires research never to reach
it except through the existing chain.

### KEEP + MODIFY

| Component | Modification | Why |
|---|---|---|
| `tools/score.py` — `valuation_score` | Take growth/ROIC/FCF as inputs, not just a `quality_hint` | §21: a high P/E is not automatically expensive. The hook already exists |
| `tools/score.py` — `analyst_score` | Keep the raw consensus; add a calibrated view beside it | §25: sell-side targets are not objective truth. **Never replace the raw figure** |
| `tools/snapshot.py` | Every material field carries source, publication date, effective date, retrieval time, units | §3 provenance |
| `tools/verify.py` | Add reasoning verification beside the numeric gate | §29, §56. **The numeric gate stays exactly as it is** |
| `backend/app/review/contract.py` | Extend three evidence kinds to eight | §4. Extend, because the three are correct and enforced |
| `.claude/agents/thesis` | Emit a structured Thesis, not prose | §28 |

### REFACTOR

| Component | Refactor | Why |
|---|---|---|
| `backend/app/research/selection.py` | Generalise from trading candidates to any hypothesis set | §46 applies identically to research |
| `backend/app/validation/statistics.py` | Already generic; expose it to the research path | It calls `tools/rule_search` helpers directly |

### MERGE

`backend/app/review/` and the research verifier should share one evidence
vocabulary rather than growing two. A trade review and a research note are
both "a claim, its kind, and what supports it".

### ADD — the genuine new work

Ordered by dependency, because several of these are prerequisites for the rest.

1. **Provenance record** (§3) — the substrate. Nothing else is trustworthy first.
2. **Evidence taxonomy, eight kinds** (§4) — extends `review/contract.py`.
3. **EvidenceGraph** (§5) — nodes and edges as specified.
4. **CompanyResearchProfile** (§6).
5. **Earnings model, driver-based, segment-aware, bear/base/bull** (§11–§13).
6. **Guidance intelligence** (§16–§17) and **surprise decomposition** (§18).
7. **Peer selection with recorded justification** (§20) and **relative
   valuation** (§19), **growth-adjusted** (§21), **value-trap** (§22).
8. **Claim engine** (§27), **Thesis** (§28), **CounterThesis** (§36),
   **falsifiability** (§37).
9. **Reasoning verifier** (§29–§34) — the level calls this the most important
   part, and it is.
10. **Prediction and outcome tracking** (§40–§42), **ResearchMemory** (§43).
11. **ResearchEdgeAssessment** (§48) and `RESEARCH_EDGE_SCORECARD.md` (§75).

### REMOVE

**Nothing.** No component was found that is dead, duplicated or wrong. §2 says
not to delete working functionality without evidence, and there is none.

---

## Where this level could go wrong

Three risks worth naming before any of it is built. Two are the level's own
warnings; the third is a disagreement with its premise, offered as a
disagreement.

**1. The level's own §16 warning contradicts §48 unless the edge is measured.**
This is a system that will produce a guidance-accuracy figure, a channel
composite, an internal forecast and a reasoning score. Every one of those is a
*number produced by us about our own process*, and §48 is explicit that
producing detailed output is not edge. The existing composite score is the
cautionary example, and it is in this repository: it was built, it looked
credible, and when backtested its information coefficient was
indistinguishable from zero across all three horizons. **The default status of
every capability added by this level must be `UNPROVEN`**, and
`RESEARCH_EDGE_SCORECARD.md` should be created early and start empty rather
than being written at the end.

**2. Channel intelligence is the highest-risk component in the level, and the
riskiest part is legal rather than technical.** §8 forbids fabricating channel
checks; §86 forbids scraping in violation of terms. Between those two
constraints, the set of channel sources this project can legitimately ingest
**today** may be close to empty — app-usage, traffic and shipment data are
mostly licensed products. The honest build is the schema, the quality scoring
and an ingestion interface with **zero sources wired**, reporting
`INSUFFICIENT_DATA`, rather than something that looks populated. An empty
channel engine that says so is worth more than a full one that inferred its
observations from news text.

**3. A disagreement, stated once.** §1 says the goal is better evidence,
forecasting, valuation, reasoning, falsification, outcome tracking and
calibration. Those are seven distinct systems, and this repository's entire
measured history says the binding constraint is **sample size, not
information**. The FX work needed ~6,600 trades to demonstrate an edge the size
of the observed scatter; equity theses resolve on quarters, so a
prediction-calibration loop (§40–§42, §51) will take *years* to produce a
statistically meaningful reading — one company-quarter at a time.

That is not an argument against building it. It is an argument for building the
**outcome-tracking and calibration spine first** rather than last, because it
is the only part whose value grows with wall-clock time and it is currently
accruing nothing. Every quarter it does not exist is a quarter of calibration
data permanently lost. §40–§43 should move ahead of §8 and §11.

---

## Proposed order of work

Following the dependency chain above and the reasoning in risk 3:

| Phase | Content | Why here |
|---|---|---|
| **A** | Provenance (§3), 8-kind taxonomy (§4), data conflict (§52), missing data (§53) | Substrate. Nothing downstream is trustworthy without it |
| **B** | Claim (§27), Thesis (§28), CounterThesis (§36), falsifiability (§37) | The objects the verifier and the tracker both need |
| **C** | Prediction (§40), Outcome (§41), post-mortem (§42), ResearchMemory (§43) | **Moved early.** It only accrues value with time |
| **D** | Reasoning verifier (§29–§34), extending `tools/verify.py` | The level's stated most-important part |
| **E** | Peers (§20), relative valuation (§19), growth-adjusted (§21), value trap (§22) | Fixes the compounder and value-trap defects named in §6, §7 |
| **F** | Earnings model (§11–§13), guidance (§16–§18), consensus calibration (§25–§26) | The heaviest build; depends on A |
| **G** | Channel intelligence (§8–§10) — schema and quality scoring, sources empty | Highest legal risk, lowest certain payoff |
| **H** | EvidenceGraph (§5) as the unifying layer, dashboard (§67), alerts (§69) | Best built once the nodes exist |
| **I** | ResearchEdgeAssessment (§48), scorecard (§75) | Cannot be honestly filled in before C has data |

Each phase gets its tests from §76 and the negative tests from §77, which are
the part of this level that will actually keep it honest.

---

## Status

    Audit complete. Nothing has been modified.

    The finding is that Level 74's methodology requirements are largely
    already implemented — for trading rules — and that the fundamental
    research layer they should be applied to is genuinely thin.

    The recommended first build is Phase A, and the recommended change to the
    level's own ordering is to move outcome tracking and calibration from
    §40-§43 to immediately after it.
