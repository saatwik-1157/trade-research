# Level 75 — research verification audit

Phase 1 of Level 75's own implementation order: audit before building.

Everything below was read or measured in this repository. Nothing has been
modified.

---

## The blocking finding

**Level 75 is a verification layer for a research layer that does not exist
yet.**

Level 75 §1 lists components to inspect. Ten of them are things Level 74 was
supposed to create. Searched across `backend/app/` and `tools/`:

| Component L75 expects to audit | Files containing it |
|---|---|
| `EvidenceGraph` | **0** |
| `CompanyResearchProfile` | **0** |
| `ThesisEngine` | **0** |
| `ReasoningVerifier` | **0** |
| `CounterThesis` | **0** |
| `ResearchMemory` | **0** |
| `PredictionRecord` | **0** |
| `GuidanceIntelligence` | **0** |
| `ChannelIntelligence` | **0** |
| `RelativeValuation` | **0** |

Level 74 was audited (`LEVEL_74_RESEARCH_AUDIT.md`) and **not built**. Its own
§2 mandates auditing first, which is as far as it got.

This matters concretely rather than procedurally. Level 75's acceptance
criteria (§72) require forecast validation, guidance validation, channel
validation, valuation validation, catalyst validation and management
validation. **There are no forecasts, no guidance records, no channel
observations, no research valuations, no catalysts and no management track
records in this system to validate.** Every one of those validators, built
today, would run against an empty table and return `INSUFFICIENT_DATA` —
forever, and correctly.

That outcome is specifically what both levels forbid. L75's closing principle
is *"More reports ≠ edge"*; L74 §48 is *"Do NOT label a strategy or research
process 'edge' merely because it produces detailed reports"*. A validation
suite with nothing to validate is the purest form of that error: maximum
apparent sophistication, zero information.

**So the honest sequence is L74's build first, then L75's validation of it.**
The merged plan below does that, and it is cheaper than either level alone
because the two share a spine.

---

## What Level 75 can reuse today — verified

The statistical and economic machinery L75 asks for largely exists. It was
built for trading models, and almost all of it is domain-neutral.

| L75 requirement | Existing module | Status |
|---|---|---|
| §26 walk-forward, train/validate/OOS | `app/datasets/splits.py` | **exists** |
| §28 look-ahead detection | `app/datasets/leakage.py` | **exists**, behavioural not structural |
| §29 leakage invalidates the test | `app/datasets/leakage.py` + `quality.py` gates | **exists** — a failing dataset cannot become READY |
| §38 multiple-testing control | `app/research/selection.py` | **exists** |
| §37 significance, bootstrap, CI | `app/validation/statistics.py`, `app/monitoring/stats.py` | **exists** — clustered t, BH, Welch, Brier, ECE |
| §13 forecast calibration | `app/monitoring/stats.py` reliability bins + ECE | **exists** |
| §30 profitability metrics | `app/analytics/metrics.py` | **exists**, and is the *single* implementation |
| §30/§33 one simulator, not two | `app/validation/economics.py` | **exists**, and explicitly forbids a second |
| §39 data quality monitoring | `app/datasets/quality.py`, `app/monitoring/` | **exists** |
| §47 process vs outcome | `app/review/checks.py` — four of five assessors cannot see the outcome | **exists**, enforced structurally |
| §59 AI restrictions | `app/ai/decision.py`, `eligibility.py` | **exists** — the AI answer carries no authority |
| §61 database, migrations only | 27 Alembic migrations | **exists** |
| §63 workers/scheduler | `app/workers/` | **exists** — do not add a second |
| §50 alerts | `app/notifications/` | **exists** |
| §65 observability | `app/observability/`, `app/monitoring/` | **exists** |
| §71 live safety | `LIVE_GATES` ×10 all False, `assert_demo` | **exists**, verified |

`app/analytics/metrics.py` is worth quoting, because it is the precedent for
how this level should be built. Its docstring records that the audit *"found
**three** implementations of win rate, profit factor and maximum drawdown
already in the repository"* and centralised them. L75 §70 says the same thing
in advance. The lesson already cost this project once.

`app/validation/economics.py` is the second precedent: it routes a model's
predictions through `tools/rule_backtest.simulate` — the same simulator that
produced every figure in `CLAUDE.md` — precisely so results remain comparable.
**L75's profitability engine (§30) must do the same and must not gain its own
simulator.**

---

## What Level 75 genuinely adds

Confirmed absent, and not implied by anything above:

* **Ablation testing** (§33, §56) — 0 files. The single most valuable idea in
  this level, and see below.
* **Survivorship-bias control** (§27) — 0 files. There is no delisted-company
  universe and no universe-construction record.
* **Benchmark comparison** (§32) — the only `benchmark` match is an unrelated
  identifier in `app/strategies/rules.py`. There is no market/sector/factor
  baseline to beat.
* **Multi-source fact verification** (§4) and **source independence** — the
  reposting problem (filing → news → website counted as three confirmations) is
  unaddressed because there is only one source, EDGAR.
* **Source reliability** (§5), **conflict detection** (§6), **research snapshots**
  (§8), **historical replay** (§7, §9), **error classification** (§10),
  **luck-vs-skill** (§46), **decision quality score** (§47), **analogues** (§43),
  **pre-mortem** (§44), **post-mortem** (§45), **edge regression monitoring**
  (§57).

### Ablation is the part to build first, and it is buildable now

§33 says: test whether each component adds value, and *"if removing a component
does not reduce out-of-sample performance, that component has no demonstrated
incremental edge."*

This is the only requirement in either level that can be applied **today, to
something real** — the existing trading system. `app/validation/economics.py`
already routes predictions through the one simulator, `app/datasets/splits.py`
already provides out-of-sample folds, and `app/research/selection.py` already
counts how many candidates clear by chance. An ablation harness over those
three is a small amount of new code and would immediately answer a question
this repository has never asked: **does the AI seat, the regime model or the
anomaly detector improve out-of-sample results at all?**

Given `CLAUDE.md`'s record — six searches, six universes, nothing clearing its
null — the prior on the answer is not favourable. That is exactly why it is
worth measuring, and why it should not wait behind ten new subsystems.

---

## Classification

### KEEP — untouched by this level

The whole execution chain: `oms/`, `risk/`, `sizing/`, `brokers/`,
`execution/`, `positions/`, `safety/`, `live/`. §2 and §71 require research
never to reach it except through the existing path, and §53 items 24–30 are
tests of that separation rather than changes to it.

### KEEP + REUSE — the verification spine

`app/datasets/{splits,leakage,quality}.py`, `app/research/selection.py`,
`app/validation/{statistics,economics}.py`, `app/monitoring/stats.py`,
`app/analytics/metrics.py`, `app/review/`, `app/ai/{decision,eligibility,registry}.py`,
`app/workers/`, `app/notifications/`, `app/observability/`.

**No new scheduler, no second simulator, no second metrics implementation.**

### KEEP + MODIFY

| Component | Modification |
|---|---|
| `app/datasets/leakage.py` | Generalise from bar features to research facts; add restatement detection (§28) |
| `app/research/selection.py` | Accept any hypothesis set, not just trading candidates |
| `app/analytics/metrics.py` | Add benchmark-relative figures (§32); keep one implementation |
| `app/review/contract.py` | The eight-kind taxonomy from L74 §4 |
| `tools/verify.py` | Reasoning verification beside the numeric gate; **the numeric gate is unchanged** |

### ADD

Everything in "What Level 75 genuinely adds", plus the L74 objects it validates.

### REMOVE

**Nothing.** No dead or duplicated component was found in either audit.

---

## Honest read of the acceptance criteria

Level 75 §72 has 33 checkboxes. Assessed against what could truthfully be
ticked if the level were built today, in isolation:

* **Tickable now** (~8): audit done, components reused, no duplicate engines,
  look-ahead detection, multiple-testing control, AI restrictions,
  research/execution separation, paper/live safety.
* **Buildable and meaningful now** (~4): ablation over the existing trading
  models, benchmark comparison, survivorship controls, luck-vs-skill.
* **Buildable but empty** (~15): forecast, guidance, channel, valuation,
  catalyst and management validation; historical replay; post-mortems; edge
  validation; the dashboard. Each depends on L74 objects that do not exist.
* **Not honestly tickable for years** (~6): anything requiring calibration
  evidence. Equity theses resolve on quarters. A calibration curve needs tens of
  resolved predictions; at four quarters a year, per company, that is a
  multi-year measurement.

**I will not tick a box for a validator that has nothing to validate**, and
`LEVEL_75_FINAL_REPORT.md` should not be written until there is something in it
other than `INSUFFICIENT_DATA`. §73 already says the acceptable answer may be
`NO_DEMONSTRATED_EDGE`; the acceptable answer here is `NOT_YET_MEASURABLE`,
which is a different and more accurate statement.

---

## Merged plan for Levels 74 and 75

The two levels share a spine. Built together, in this order, each phase
produces something that works rather than something that waits:

| Phase | Content | From | Value on completion |
|---|---|---|---|
| **0** | **Ablation harness** over the existing AI seat, regime and anomaly models | L75 §33 | Answers a real question about the system that exists today |
| **1** | Provenance, 8-kind taxonomy, data conflict, source independence | L74 §3–4, L75 §4–6 | Substrate; makes every later claim traceable |
| **2** | Prediction / Outcome / post-mortem / ResearchMemory | L74 §40–43, L75 §12, §45 | **Starts accruing calibration data immediately** |
| **3** | ResearchSnapshot + historical replay + leakage extension | L75 §7–9, §28 | Makes past decisions reproducible |
| **4** | Claim, Thesis, CounterThesis, falsifiability | L74 §27–28, §36–37 | The objects the verifier needs |
| **5** | Reasoning verifier extending `tools/verify.py` | L74 §29–34, L75 §3 | The stated most-important part |
| **6** | Peers, relative valuation, growth-adjusted, value trap | L74 §19–22, L75 §18–20 | Fixes the compounder/value-trap defects |
| **7** | Earnings model, guidance, consensus calibration | L74 §11–18, L75 §14–15 | Heaviest build |
| **8** | Benchmark comparison, survivorship, profitability | L75 §27, §30, §32 | Makes "edge" answerable |
| **9** | Channel intelligence — schema only, sources empty | L74 §8–10, L75 §16 | Highest legal risk, least certain payoff |
| **10** | Edge assessment, scorecards, dashboard, alerts | both | Only honest once phase 2 has data |

Phase 0 is deliberately first. It is small, it uses only what exists, and it
tests the premise both levels rest on — that adding components adds value —
against the one system where the answer is already measurable.

Phase 2 is deliberately early for the reason given in the L74 audit: it is the
only component whose value grows with wall-clock time, and every quarter it
does not exist is calibration data permanently lost.

---

## Status

    Audit complete. Nothing modified.

    Level 75 cannot be honestly completed before Level 74 is built: ten of the
    components it exists to verify are absent, and its validators would return
    INSUFFICIENT_DATA by construction.

    Its verification spine, however, largely already exists — built for trading
    models — and one part of it, ablation testing, can be applied to the
    current system immediately and would answer a question this repository has
    never asked.

    Recommended start: Phase 0, then Phase 1.
