# Level 76 — decision intelligence audit

Phase 1 of Level 76's own order. Nothing modified.

This is the third audit in the 74→75→76 chain. It is shorter than the first two
because the pattern is now established and the interesting findings are
specific.

---

## 1. The premise correction

Level 76 opens: *"Level 75 established the Research Verification, Historical
Intelligence, Forecast Validation, Profitability Validation, Ablation Testing,
and Edge Assessment layer."*

**It did not.** Level 75 was audited and not built, as was Level 74. Verified
again for this audit — every one still returns zero files:

```
EvidenceGraph 0   ThesisEngine 0        ReasoningVerifier 0   CounterThesis 0
ResearchMemory 0  PredictionRecord 0    GuidanceIntelligence 0
ChannelIntelligence 0                   RelativeValuation 0
AblationEngine 0  survivorship 0        BenchmarkEngine 0
```

So §1's instruction to *"Read and audit: Level 74 implementation, Level 75
implementation, ResearchVerificationEngine, HistoricalResearchReplayEngine,
ResearchMemory, EvidenceGraph, ThesisEngine, ReasoningVerifier,
ForecastCalibrationEngine, ResearchProfitabilityEngine, BenchmarkEngine,
AblationEngine, EdgeAssessmentEngine"* has **no subject for twelve of its
thirteen research items**.

`InvestmentDecisionContext` (§4) lists 28 required inputs. Guidance, Consensus,
Internal Forecast, Forecast Delta, Estimate Revisions, Channel Signals,
Management Track Record, Relative Valuation, Historical Valuation, Thesis
Quality, Counter-Thesis, Catalysts, Scenario Distribution, Historical
Analogues, Research Confidence and Forecast Calibration — **sixteen of the
twenty-eight — have no source in this system.**

---

## 2. The finding that matters: much of Level 76's core already exists

And it comes from an unexpected place. **`app/portfolio/decision.py`, built at
Level 60, is most of Level 76's decision layer.**

| L76 requirement | Already in `app/portfolio/decision.py` |
|---|---|
| §3 decision states | `Verdict`: ALLOW, REVIEW, REDUCE, RESTRICT, DEFER, PAUSE, REJECT |
| §5 freshness | `Freshness`: FRESH, STALE, MISSING, INVALID |
| §31 confidence must fall with missing/conflicting data | Its stated first rule |
| — (L76 has no equivalent) | `Layer`: PREFERENCE < OPTIMIZATION < SIGNAL < STRATEGY < PORTFOLIO < RISK < HARD_SAFETY |

Its docstring states the rule directly: *"**Missing is never safe.** Every input
carries where it came from, when, and whether that is still true. An input that
is `MISSING`, `STALE` or `INVALID` makes the decision MORE conservative — never
less."* That is §5 and §31, written eleven levels earlier.

The `Layer` enum is worth noting because **Level 76 has nothing like it and
should**. It ranks *which layer decided*, from a preference up to hard safety,
so a permissive signal can never overrule a restrictive risk verdict. §2's
requirement that decision intelligence "must NOT answer *what trade should I
place*" is enforced by that ordering already — a research verdict enters at
`SIGNAL` (3) and cannot outrank `RISK` (6) or `HARD_SAFETY` (7).

Other existing coverage:

| L76 § | Requirement | Exists as |
|---|---|---|
| §22, §23 | Portfolio fit, exposure, concentration, marginal contribution | `app/portfolio/exposure.py` — `ExposureReport`, buckets, `open_risk`, `correlation_note` |
| §40 | Market regime intelligence | `app/ai/regime.py` |
| §41 | Execution-aware research | `app/marketdata/`, `app/brokers/` |
| §43 | Deterministic calculations | `app/analytics/metrics.py` — the single metric implementation |
| §37 | Overfitting / multiple testing | `app/research/selection.py`, `app/datasets/splits.py` |
| §48 | Database, migrations only | 27 Alembic migrations |
| §55 | Safety defaults | `LIVE_GATES` ×10 all False, `assert_demo` |

### §50's safety tests are already written

Four of the most important, located by name:

| §50 test | Existing test |
|---|---|
| #11 research cannot create an order | `test_the_oms_cannot_be_reached_without_an_approval` (`tests/test_risk.py`) |
| #12 cannot bypass RiskEngine | same, plus `tests/test_integration.py:632` — *"the AI must not be able to bypass the RiskEngine"* |
| #13 AI cannot modify hard safety | `test_the_ai_layer_cannot_enable_live...` (`tests/test_ai_integration.py:889`) |
| #14 $10,000 cannot become $15,000 | `test_tampering_with_the_approved_volume_breaks_the_binding` (`tests/test_oms.py:168`) |

**Writing these again would create the duplication §1 forbids.** They should be
referenced, not reimplemented.

---

## 3. Classification

**KEEP + REUSE, do not duplicate:** `app/portfolio/` (all 11 modules),
`app/ai/regime.py`, `app/analytics/metrics.py`, `app/research/selection.py`,
`app/datasets/`, `app/validation/`, `app/risk/`, `app/sizing/`, `app/oms/`,
`app/strategies/`, and the four safety tests above.

**KEEP + MODIFY — small, precise, buildable now:**

| Component | Change | Size |
|---|---|---|
| `app/portfolio/decision.py::Freshness` | Add `AGING` and `CONFLICTED` (§5 names six; four exist) | ~2 lines + tests |
| `app/portfolio/decision.py::Verdict` | Map L76 §3's research states onto the existing verdicts rather than adding a parallel enum | design decision |

**ADD — but blocked on L74/L75:** `HistoricalPatternEngine` (§7–9),
`EarningsDeltaEngine` (§10), `FundamentalPriceDivergenceEngine` (§12),
`MarketExpectationEngine` (§13), `RequiredSuccessConditions` (§14),
`FailureConditionEngine` (§15), `DecisionReadinessEngine` (§18),
`MarginOfSafetyEngine` (§20), `ValueOfInformationEngine` (§26),
`HistoricalAnalogueEngine` (§28–29), decision journal (§32), outcome tracking
(§33), pattern validation (§36–37).

Each of these consumes L74 objects that do not exist. Built now, each returns
`INSUFFICIENT_DATA` permanently.

**REMOVE:** nothing, in any of the three audits.

---

## 4. What is buildable today, and why it is the same answer as last time

**Ablation testing.** L75 §33 and §52, L76 §52. It is:

* **Absent** — `ablation` matches zero files.
* **Unblocked** — it needs only `app/validation/economics.py` (which routes
  predictions through `tools/rule_backtest.simulate`, the one simulator every
  figure in `CLAUDE.md` came from), `app/datasets/splits.py` (out-of-sample
  folds) and `app/research/selection.py` (how many candidates clear by chance).
* **Answering a real question** — does the AI seat, the regime model or the
  anomaly detector improve out-of-sample results at all? This repository has
  never asked.

L76 §52 lists eight components to ablate, all of which are unbuilt. But the
*existing* AI layer has three that are built and running, and the technique is
identical. Applying it there first tests the premise all three levels rest on —
that adding components adds value — against the only system where the answer is
currently measurable.

Given `CLAUDE.md`'s record — six searches, six universes, nothing clearing its
null, and a random rule beating real candidates in a 36-cell sweep — the prior
is not favourable. **That is the argument for measuring it, not against.** If
the AI layer shows no incremental out-of-sample value, that is a finding worth
more than any of the twelve engines above, because it would apply to all of
them.

---

## 5. Recommendation

Unchanged from the Level 75 audit, and now supported by a third independent
pass:

1. **Build the ablation harness** (Phase 0). Small, unblocked, and it tests the
   premise.
2. **Then L74 Phase 1–2** — provenance, the eight-kind taxonomy, and
   prediction/outcome tracking, which is the only component whose value grows
   with wall-clock time.
3. **Extend `Freshness` with `AGING` and `CONFLICTED`** — a two-line change that
   is genuinely part of L76 and costs nothing.

What should **not** happen is building twelve engines whose inputs do not
exist. §56's completion checklist has 23 boxes; roughly four could be ticked
honestly today, and the rest describe machinery with nothing to process.

---

## Status

    Audited. Nothing modified.

    Level 76's decision layer substantially exists already, as
    `app/portfolio/decision.py` and `app/portfolio/exposure.py`, built at L60 —
    including a Layer ordering that Level 76 does not specify and should adopt.
    Its safety tests exist and are named above.

    Its research inputs do not exist: 16 of the 28 fields in
    InvestmentDecisionContext have no source, because Levels 74 and 75 were
    audited and not built.

    Recommended: ablation first, then L74 provenance and outcome tracking.
