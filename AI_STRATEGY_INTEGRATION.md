# AI strategy integration (Level 27)

The AI layer now occupies the seat `app/execution/ai.py` has held since L16 and
which had taken nothing.

The one sentence that governs the level:

> **AI enhances a strategy decision. It never becomes the final authority over
> trading risk.**

Everything below is that sentence made structural rather than intended.

---

## 1. The pipeline, unchanged

```
TRADINGVIEW / STRATEGY
        ↓
SIGNAL ENGINE
        ↓
STRATEGY ENGINE          ← the deterministic signal. Still the only originator.
        ↓
AI STRATEGY FILTER       ← L27. It can decline. It cannot create.
        ↓
RISK ENGINE              ← the only producer of an Approval. Final authority.
        ↓
POSITION SIZING          ← the only producer of a quantity.
        ↓
OMS                      ← the only path to a venue.
        ↓
BROKER ADAPTER
        ↓
MT5
```

The AI seat was already at this position — L16 put it there and L20 wired the
orchestrator through it. L27 fills it; it does not move it.

**There is no `TradingView → AI → MT5`.** An externally-arriving alert still
goes through the webhook gateway, validation, idempotency, the signal engine,
the strategy check, the AI seat, risk, sizing, the OMS and the adapter. A test
asserts the AI stage precedes the risk stage in the orchestrator's own source.

---

## 2. What was already here

L27 is the level where the previous seven pay off. The audit found the seat, the
contract and the policy already built:

| Component | Where | Verdict |
| --- | --- | --- |
| `AiFilter`, `AiVerdict` — the seat and its return type | `app/execution/ai.py` (L16, moved at L22) | **KEEP** |
| `AiPolicy`, `AiGate`, `ModelBackedFilter` | `app/execution/ai.py` (L24) | **KEEP**; `AiPolicy` moved to a neutral module and re-exported |
| The AI stage, before risk, in both pipelines | `app/paper/engine.py`, `app/execution/pipeline.py` | **KEEP** — the position was already right |
| `Outcome.ai_rejected`, already in `NO_ORDER` | `app/execution/outcome.py` | **KEEP** |
| `Prediction`, `PredictionStatus`, `FeatureContract`, `ModelIdentity` | `app/ai/contract.py` (L24) | **KEEP** — one prediction contract, not a second |
| `ModelRegistry` | `app/ai/registry.py` (L24) | **KEEP** — `get(key, version)` is the normal call |
| `compute_features` | `app/datasets/features.py` (L23) | **KEEP** — one feature engine for training and inference |
| `model_predictions`, idempotent on `prediction_key` | L24 | **KEEP** — the journal references it rather than restating it |
| `validation_runs` | L26 | **KEEP** — it is what makes a model eligible |
| `simulate()`, `build_signal_vector` | L14 | **KEEP** — the AI filter is applied to the vector, not a second backtest |
| Modes, thresholds, the decision type, the journal, eligibility | *(absent)* | **ADD** |

Nothing was replaced and nothing was removed. `ModelBackedFilter` — L24's
single-model seat — is kept and still tested, because it works and several tests
describe it; `IntegrationFilter` is the richer seat and occupies the same
position with the same limits.

### The one move, and why

`AiPolicy` lived in `app/execution/ai.py` and L27's integration service needs
it. Having `app.ai` import `app.execution` would reverse the one dependency
direction this codebase keeps — and that reversal is exactly the cycle L22 had
to break when `AiVerdict` was defined inside the paper engine.

So the shared vocabulary moved to `app/ai/decision.py` and
`app/execution/ai.py` re-exports it. Every existing caller and test is
unchanged. Same extraction, same reason, third time.

---

## 3. The four modes

Section 6, and there is deliberately no fifth. **No mode lets the AI layer
originate a trade**: every one takes a signal the strategy already produced.

### AI_DISABLED — the baseline

Nothing is loaded, nothing is computed, and `evaluate()` returns before it does
anything. The strategy behaves **exactly** as the deterministic strategy would.

This is not a degraded mode. It is:

* the baseline every AI-vs-no-AI comparison needs;
* the fallback when the model registry is empty;
* what a strategy with no configuration gets, which is the only default in the
  level.

In a paper bot, AI_DISABLED means the engine's `ai` seat is `None` — there is no
AI object at all, rather than one that accepts everything. The difference is not
cosmetic: a filter would run, journal a row and appear in the counters, so a
reader could not tell a disabled deployment from one whose model agrees with
everything.

### AI_ADVISORY

Inference runs, the reading is recorded, and the signal is unchanged. The
decision is `NEUTRAL` — never `ACCEPT` — because somebody will later count how
often the layer agreed, and an advisory reading is not agreement.

### AI_FILTER

Inference runs and the signal may be **rejected**. A probability below the
configured minimum stops the pass with `Outcome.ai_rejected`, which is already
in `NO_ORDER`.

### AI_SCORING

The strategy's own score and the AI probability combine by a **named formula**:

| Method | Formula | Note |
| --- | --- | --- |
| `minimum` | `min(strategy, ai)` | the default |
| `weighted` | `(1-w)·strategy + w·ai` | |
| `product` | `strategy · ai` | strictest |

`minimum` is the default because it is the only one of the three that cannot let
a confident AI rescue a weak strategy signal.

Formulas are named rather than supplied. §41 forbids accepting arbitrary code,
and an arbitrary formula is arbitrary code with extra steps. Every branch is
monotone non-decreasing in both inputs — asserted by a test — which is what
makes a threshold on the result mean anything.

A strategy with **no** score of its own is refused in this mode rather than
scored on the AI alone, which would silently be AI_FILTER at a different
threshold.

---

## 4. AI_REQUIRED and AI_OPTIONAL

Sections 11 and 28. Two values, and the absence of a third is the point:
"carry on if it feels safe" is what an explicit policy exists to replace.

The policy is applied in **one function**, `_failed()`, so it cannot be applied
twice and a new failure mode inherits the configured behaviour instead of
picking its own. Every failure goes through it:

| Status | Cause |
| --- | --- |
| `MODEL_UNAVAILABLE` | not registered, or nothing resolved |
| `INCOMPATIBLE` | unfitted, or a feature-set version mismatch |
| `FEATURES_UNAVAILABLE` | too little history for the warm-up |
| `INVALID_OUTPUT` | a probability outside [0,1], or NaN |
| `LATENCY_EXCEEDED` | over the configured budget |
| `NO_PROBABILITY` | a deciding mode with only a regime model resolved |
| `LOOKAHEAD_REFUSED` | the supplied window reached past the signal |
| `PIPELINE_ERROR` | anything else, recorded rather than raised |
| `CONTEXT_ERROR` | the caller's context builder failed |

Under **AI_REQUIRED** each is a rejection: *"A model that could not answer is not
a model that agreed."* Under **AI_OPTIONAL** each proceeds to the risk engine,
and the reason says so. Neither is ever chosen implicitly, and neither skips a
downstream gate.

---

## 5. Model selection — validated only

Section 12, and it is where L26 pays off.

`app/ai/eligibility.py` is a **boundary, not a registry**. It holds no state,
stores nothing, and answers one question by reading rows two other levels own:

```
model_versions.status   ← L24 writes `draft`; L28 will write the rest
validation_runs         ← L26's verdict
```

Today: a version is eligible when its most recent **completed** validation run
returned PASS or CONDITIONAL. When L28 lands and begins writing
`model_versions.status`, `USABLE_STATUSES` becomes the primary gate — one
function changes, not a search.

Refusals name themselves:

| Situation | Answer |
| --- | --- |
| never validated | not eligible — *"nothing has been established about it"* |
| verdict BLOCKED | not eligible — *"a check could not be evaluated"* |
| verdict FAIL | not eligible |
| retired / rejected | not eligible |
| unknown key or version | refused, never resolved to the nearest one |

**A version is named exactly.** There is no `latest` in a `ModelRequirement`,
and the dataclass refuses an empty version string: a strategy whose behaviour
changes when somebody registers a new version is a strategy nobody can
reproduce.

**A configuration naming an ineligible model is refused, not filtered.** A
configuration that silently dropped a model would run a different pipeline from
the one it describes, and the operator who wrote it would have no way to notice.
Starting a bot whose configuration cannot be honoured refuses too, rather than
starting with the AI quietly off — §28's implicit fallback, avoided.

---

## 6. Compatibility, checked before inference

Section 13, and every check runs **before** any feature is computed:

* the model is fitted (an unfitted one answers `MODEL_UNAVAILABLE`, not a
  default);
* `contract.accepts_version(feature_version)` — a model fitted against a
  different feature set is asked about a different quantity;
* every feature it requires is one this platform's engine computes.

Then features are prepared once, from the **union** of what the resolved models
declare, through `app.datasets.features.compute_features` — the same function
that built the training data. §15 asks that training and inference use
compatible features; one implementation with no second path in is the cheapest
way to guarantee it.

A feature that cannot be computed is a refusal, never a substitution: *"a
feature filled with a default is a number the model will treat as a reading."*

---

## 7. No future information

Section 16, and it is structural in three places.

1. **The AI layer fetches nothing.** No module in `app/ai/` reads market data,
   a database or a cache of bars. It is handed a window and cannot reach for a
   different one — *a seat that could fetch a bar could fetch tomorrow's.*
2. **The window is the strategy's own.** `PaperEngine.process` builds
   `Candles.of(...)` once, hands it to the strategy, and hands the identical
   `bars` tuple to the AI seat. They cannot diverge because there is one object.
3. **A window reaching past the signal is refused, not trimmed.** Trimming would
   turn a caller's bug into a silently different evaluation, and the result
   would look fine. `LOOKAHEAD_REFUSED` says what happened.

In the **backtest**, `apply_ai_filter` runs bar by bar and passes
`bars[:i+1]` — the identical prefix `build_signal_vector` gave the strategy.
That is why it is a loop and not a vectorised pass: *computing once over the
whole array is how look-ahead is introduced, so the cost is the point.* A test
records the window length at each consulted bar and asserts it is exactly
`i+1`.

In **market replay** (§32), the replay engine *is* the paper engine, so the
guarantee is the same one rather than a second implementation of it.

**No label reaches inference.** `SignalContext` has no label field, and a test
asserts the serialised context contains no `bracket_outcome`.

---

## 8. The AI cannot bypass anything

Section 20's rule is absolute, and here is how each half is enforced.

### It cannot approve

`AiVerdict` has exactly four fields: `accept`, `confidence`, `reason`, `model`.
There is no field naming a limit, a switch, an approval, a quantity or an
account. An AI that wanted to overturn a risk veto has **no vocabulary for it**.

`AiDecision` — the richer type — has no such field either. A test enumerates
`quantity`, `volume`, `lots`, `risk_percent`, `risk_amount`, `account`, `order`,
`approve` and `leverage` and asserts none appears in the payload.

### It cannot size

Section 21 and 22. `app/ai/` imports no sizing module, and a test asserts the
strings `SizingRequest`, `lot_for_risk` and `calculate(` appear nowhere in the
package. **AI confidence is not allowed risk**: a 99% probability produces the
same `AiVerdict` shape as a 51% one, and nothing downstream reads `confidence`
as a size.

### It cannot execute

A test parses every module in `app/ai/` with `ast` and fails on an import of
`app.execution`, `app.oms`, `app.risk`, `app.sizing`, `app.brokers`,
`app.orders`, `app.positions`, `app.paper` or `app.bots`. `LIVE_TRADING` and
`live_trading` appear nowhere in the package.

The router adds nothing either: no path under `/v1/ai` contains `order`,
`execute`, `place`, `trade`, `position` or `live`, and there is no `PUT` or
`DELETE`.

### It cannot originate

In the backtest, a bar the strategy left flat is **never offered** to the AI
layer — there is no branch that turns a `0` into a `±1`. A test runs an
accept-everything model over an all-zero signal vector and asserts the output is
identical and the layer was asked zero times.

---

## 9. Latency

Section 26. Three figures are measured and recorded on every decision:
feature preparation, inference, and total.

Exceeding the configured budget is a **failure of the AI layer**, handled by the
policy: a rejection under AI_REQUIRED, a fallback under AI_OPTIONAL. It is never
a reason to skip a gate — whatever proceeds still runs through risk, sizing and
the OMS.

A budget of zero is refused at construction as a configuration mistake rather
than accepted as a policy that rejects everything.

Models are **resolved from the in-process registry**, not loaded from disk per
signal (§44). The registry holds fitted objects; a resolution is a dictionary
lookup.

---

## 10. The journal

Section 35, and it is the audit trail L29's monitoring, the trade journal and
the AI trade review all read.

`ai_decisions` records, for every signal the AI layer saw: the strategy and its
version, the symbol, timeframe and **bar time** (never "now" — two runs over the
same data produce the same row), the side, the mode and policy, the decision and
the **status**, the model and its version, the feature version, the probability,
predicted class, regime, anomaly score, confidence, both scores, all three
latencies — and then what happened *afterwards*: `final_outcome`,
`risk_verdict`, `order_id`, `execution_id`.

That last group is why "the AI accepted and risk vetoed" is **one row** rather
than a correlation somebody reconstructs from two tables and a timestamp.

**Every decision leaves a row, including the ones that changed nothing.** An
advisory NEUTRAL, a disabled run, a failure — all of them. A decision that
leaves no trace is indistinguishable from an AI nobody asked, and the two have
opposite meanings when somebody counts how often the layer answered.

**The inference itself is not copied.** `model_predictions` (L24) already
records every prediction including the refusals, is idempotent on
`prediction_key`, and carries the input digest. `ai_decisions.prediction_id`
references it. The single denormalised `probability` exists so a dashboard can
aggregate without a join, and a CHECK keeps it in range.

Two constraints carry §47 into the schema:

```sql
decision <> 'ERROR' OR probability IS NULL
mode <> 'AI_DISABLED' OR (probability IS NULL AND model_version_id IS NULL)
```

A failed inference that reports a number is exactly the shape a fabricated
result takes. A disabled run computed nothing, so it cannot have produced a
reading.

Collection follows the `pending_verdicts` pattern L16 already uses: the filter
collects `(decision, context)` pairs and the paper service — which has the
session — drains and writes them. One list holding both, because the pairing is
what a journal row needs and two lists is one opportunity too many to mismatch
them.

---

## 11. Backtest integration

Section 31. `BacktestResult` gains an `ai` field which is `None` for a run with
no AI service — and a run with no AI service is **byte-identical** to every
backtest run before this level, because `signals` is not touched.

With a service, `apply_ai_filter` reports what §31 asks for: signals offered,
accepted, rejected, neutral, errors, the acceptance and rejection rates, the
statuses seen, the probability distribution, and up to 200 individual decisions.

The report carries its own reading instructions:

> fewer trades is the expected effect of a filter, and fewer trades is not by
> itself an improvement — read the expectancy per trade beside the total.

That caveat is not decoration. This repository's measured position is that
**frequency is the one lever with a proven sign, and it points down**: tight
brackets trade often and pay the spread every time. A filter that removes trades
will improve total P&L on many of the project's own datasets *for that reason
alone*, and reading it as an AI edge would be the same error the `time_120` exit
and the H4 bracket sweep both produced.

---

## 12. Paper trading

Section 33. An AI-integrated strategy runs in paper first — and in this platform
it cannot do otherwise: `TRADING_MODE=paper` and `LIVE_TRADING=false` are
unchanged by L27, and there is no code path here that touches either.

The paper service builds the seat when a bot starts, from the strategy's stored
configuration, and refuses to start a bot whose configuration names an
ineligible model.

---

## 13. Configuration

Section 38, explicit throughout. `ai_strategy_configs` is keyed by
`(strategy_key, account_id)` so one strategy can run advisory on a demo account
and disabled elsewhere without two strategy rows. A NULL account is the default
for that strategy; an account-scoped row wins.

Everything a caller can set is a threshold, a mode, a policy or a named model.
There is deliberately **no field** for a model path, a formula, an expression or
a code fragment, and `scoring_method` is an enum rather than a string.

The stored model list is data a user supplied, so `_requirements()` refuses
anything that is not a `{key: str, version: str}` object rather than coercing
it — a test feeds it `{"key": "../../etc/passwd", "version": {"exec": "..."}}`
and asserts the refusal.

Every threshold is range-checked twice: by the request schema, and by a CHECK
constraint on the table.

| Threshold | Default |
| --- | --- |
| `minimum_probability` | 0.5 |
| `maximum_anomaly_score` | none (not applied) |
| `allowed_regimes` | none (not applied) |
| `minimum_expected_return` | none |
| `maximum_latency_ms` | 2000 |
| `scoring_method` | `minimum` |
| `ai_weight` | 0.5 |
| `minimum_combined_score` | 0.5 |

`None` means "not applied", which is different from a permissive value: a
`maximum_anomaly_score` of 1.0 says *any anomaly is fine* and `None` says *this
deployment has no anomaly model*, and a report should be able to tell them
apart.

---

## 14. API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v1/ai/integration` | the pipeline, the modes, and what the layer cannot do |
| GET | `/v1/ai/integration/models` | versions a strategy may legitimately name, with the reason either way |
| GET | `/v1/ai/integration/strategies` | every configured strategy |
| GET | `/v1/ai/integration/strategies/{key}` | one configuration (AI_DISABLED when none) |
| POST | `/v1/ai/integration/strategies/{key}` | set it. Refuses an ineligible model. |
| GET | `/v1/ai/integration/decisions` | the journal, counted by decision and by outcome |

No `PUT`, no `DELETE`. Every route requires `manage_ai_models`.

---

## 15. Frontend

The AI Lab gains three panels: the pipeline (rendered from the backend's own
list, so the page cannot drift from the order the code enforces), the decision
journal, and the strategy configuration table.

**It is informational.** Configuring a strategy's AI is a permissioned POST and
is deliberately not offered on the page: a surface that shows what the AI
decided and a surface that changes what it will decide are different things, and
mixing them is how a threshold gets moved while reading a losing streak.

**ACCEPT is rendered `accent`, never `good`** — the same choice `PASS` gets on
the validation table and `validation_pending` on the training table. An ACCEPT
means the AI layer did not object; what happened next is the *Then* column,
where `risk_vetoed` is both common and correct. Colouring ACCEPT as success
would teach the reader that the AI approves trades, which is the one thing the
whole architecture is arranged to prevent.

---

## 16. What Level 27 does not do

* **No model has been wired to a strategy on real data.** `market_bars` covers one instrument
  here; MT5 is not connected. Every test runs against seeded synthetic series,
  and no claim is made about any model's usefulness.
* **No AI-assisted sizing.** §21's default is kept and there is no path to
  anything else.
* **No autonomous strategy discovery.** §30's Pine parser and LLM
  interpretation do not exist yet (`PROJECT_STATE.json` records
  `pine_parser: NOT_BUILT`); when they land, the boundary they must respect is
  the one this level built — the compiled strategy produces the signal and the
  AI layer may only filter it.
* **No drift monitoring.** L29's, and building a second one here is the
  duplication §40 warns against. What L27 provides is the data it will read:
  predictions, feature versions, model versions, decisions and outcomes, all in
  one table.
* **No live trading.** `TRADING_MODE=paper` and `LIVE_TRADING=false` are
  untouched. Even with strategy PASS, AI ACCEPT and risk PASS, the platform
  stays in paper mode.
