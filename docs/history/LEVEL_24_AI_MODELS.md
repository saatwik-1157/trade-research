# LEVEL 24 — AI MODELS

Completed 2026-09-04. Adaptive build mode: audit → baseline → plan → implement
→ integrate → test → verify → document.

Three model families, one prediction shape, and no authority. The strongest
thing anything built here can do is decline.

---

## 1. What the audit found

**No AI model exists anywhere in this repository.** Not a stub, not a pickle,
not a notebook. `grep` for `sklearn`, `torch`, `tensorflow`, `xgboost`,
`lightgbm`, `catboost` and `keras` across `backend/`, `tools/` and `tests/`
returns three hits, all of them in tests asserting that a package does *not*
import them. There is no `ai/`, `ml/`, `training/`, `inference/`,
`experiments/` or `notebooks/` directory, and no `.ipynb` file.

So §4's preservation rules, §35's per-model decisions and §47's "do not delete
existing models" all have nothing to act on, and nothing was deleted, retrained
or replaced. That is the audit's main finding and it is worth stating plainly
rather than dressing up as a migration.

What *does* exist, and is reused:

| Component | State | Verdict |
|---|---|---|
| `models`, `model_versions`, `training_runs`, `model_predictions` | L05, empty | **KEEP + MODIFY** — the registry §34 says not to duplicate |
| `AiVerdict` / `AiFilter` in `app/execution/ai.py` | L16, moved at L22. The seat has no field by which a model could approve, size or raise anything | **KEEP** — §54.1–4 already held before this level |
| `app/datasets/` | L23: 22 causal dimensionless features, 5 labels, splits, scaler, leakage checks | **KEEP** — every model here reads it |
| `app/monitoring/` | L29: PSI, KS, Brier, reliability bins, expected calibration error, BH-FDR, `market_regime` | **KEEP** — §20's calibration and §39's drift preparation |
| `tools/rule_search.py` | L26 methodology: permutation null, Bonferroni, era blocks, walk-forward, date clustering | **KEEP** — the gate a model must clear |

---

## 2. The defect the audit found, which is not an AI defect

**The backend Docker image cannot run the strategy engine, the backtester, the
replay engine or L23's feature engine.**

`backend/Dockerfile` installs `backend/requirements.txt` and copies `app`,
`alembic`, `alembic.ini` and `pyproject.toml`. It does **not** install numpy,
and numpy was never declared there — yet `app/strategies/indicators.py`,
`app/strategies/rules.py`, `app/backtest/runner.py` and `app/replay/engine.py`
have all imported it since L12. It works on a developer machine only because
the research toolkit's own `requirements.txt` is installed globally.

Worse, `app/strategies/indicators.py::_toolkit` resolves `tools/` as a sibling
of the backend directory and imports `rule_backtest` and `rule_search` from it.
In the image that path is `/srv/tools`, and `tools/` is never copied.
`docker-compose.yml` mounts nothing to fix it.

Found by this level's dependency audit — §3 asks what the project already
supports, and the honest answer turned out to be "less than it appears to".

**What was done about it.** `numpy>=1.24` is now declared in
`backend/requirements.txt`, which fixes half the problem and is unambiguously
correct either way. The other half — the image carrying no `tools/` — is a
build-context change, which is L41's subject and not a decision to take
unilaterally in the middle of a model level. It is recorded here, in
`PROJECT_AUDIT.md` §21 and against L41 in `MIGRATION_STATUS.md`.

**And it shaped this level's design**: `app/ai/` uses only the standard
library, so the AI layer does not deepen a deployment gap it did not create.

---

## 3. No scikit-learn, and the reasoning

§17 asks for strong classical baselines and says to use what the project
already supports. It was tempting to add scikit-learn and be done. Four reasons
not to, and they compound:

1. **It is not already supported.** The backend does not even declare numpy
   (§2), so adding a second heavy numerical dependency would widen a gap this
   level had just discovered.
2. **The models this level needs are a few dozen lines each.** A quantile-cut
   regime classifier, a robust-z anomaly detector and a logistic regression are
   all standard-library arithmetic. Importing 30MB of framework to run them is
   not a baseline, it is a dependency.
3. **A logistic model's coefficients ARE the explanation §23 asks for.**
   `weight × value` is the exact contribution to the logit — not an
   approximation of it, not a surrogate, and not SHAP output that might not
   correspond to what the model did. §23 forbids claiming a feature influenced
   a model when it did not, and this is the one model class where that claim is
   provably true.
4. **Determinism is easier to guarantee** (§24). The fitter here is full-batch,
   fixed-iteration, zero-initialised, with no shuffling and no early stopping.
   Every one of those removes a source of run-to-run variation.

When L25 needs gradient boosting, it should be added — with a stated reason and
after the packaging gap is closed. That is a different decision from taking it
now because it is conventional.

---

## 4. The prediction contract

One shape for all three families (§11), because counters, drift checks and a
dashboard can only be built over predictions that share one.

**A refusal carries no value**, enforced in `Prediction.__post_init__` and again
by a database CHECK. A caller cannot read a number that was never computed —
the difference between "you must check the status" and "there is nothing to
read". Six statuses, and each is a refusal with a reason:

| Status | When |
|---|---|
| `OK` | a value was computed |
| `MODEL_UNAVAILABLE` | no fitted model to ask (§26) |
| `FEATURE_VERSION_MISMATCH` | fitted against a different feature set (§14, §51) |
| `MODEL_INPUT_ERROR` | a required feature was absent or null (§13) |
| `STALE_FEATURES` | older than the model accepts, or unstamped (§25) |
| `INSUFFICIENT_DATA` | valid inputs, too thin to answer |

**The gate lives in `BaseModel`, in a fixed order**, so a fourth family added
later cannot forget one. The order matters: a caller asked with the wrong
feature version usually also has missing features, and reporting the second
would send them looking for data rather than for a version.

**`probability` and `confidence` are separate fields.** A probability is an
estimate of a named outcome; a confidence is how sure the model is of its own
output. 0.51 is a confident-ish reading of a near-coin-flip, and collapsing the
two would lose that. The regime and anomaly models report no probability at all
rather than borrowing their confidence into the field.

**`calibrated` is false until measured** (§20). An uncalibrated probability is
a ranking, not a frequency, and the prediction says so in its own metadata.

---

## 5. The three models

### 5.1 Regime — fitted thresholds, not constants

§6 forbids hardcoded regime labels without a methodology, so the methodology is
written down and the four boundaries are **quantiles of the training segment**.
A 0.3% ATR is high volatility in EURUSD and quiet in BTC; any fixed number
would encode one instrument's habits as a universal fact — the same mistake as
a raw price level being a feature, one level up.

Volatility wins at the top of its range: a market moving violently in one
direction is more usefully described as violent than as trending, because what
a risk decision needs to know is that the range has widened. Below
`minimum_confidence` the answer is `UNKNOWN` (§7), which is a real answer.

`fit_cuts` **refuses** below 30 readings rather than falling back to constants:
a regime model with invented thresholds produces output indistinguishable from
a fitted one's.

### 5.2 Trade probability — P(a named label), and the name travels

§8's whole point. This is not "the probability the trade wins"; it is
P(`bracket_outcome == WIN`) under L23's label definition — a long entry at this
close, a stop at `stop_atr × ATR`, a target at `take_profit_atr × ATR`, within
`horizon` bars, net of a stated spread. `label_definition` is on every
prediction so the number cannot be quoted without it.

It refuses until it has coefficients. There is no default weight vector,
because a default one answers 0.5 for everything and 0.5 is a number a caller
will act on.

### 5.3 Anomaly — a statistical detector, and rare is not bad

Median and MAD rather than mean and standard deviation, because an outlier
moves a mean far more than a median — a detector fitted with means is partly
fitted to the events it exists to find.

The score is `min(1, max robust z / saturation_z)`, and the model says in its
own output that this is **not a probability and not a severity**. §22, and more
forcefully the project's own record: the largest single loss in the live log
came from a bar whose M1 range was 282 points, and that bar was real. This layer
labels; risk and the exit policies decide.

A constant feature is named and skipped rather than treated as infinitely
unusual — the same reasoning `Scaler` applies to a zero deviation.

---

## 6. Version resolution, and §50 verbatim

`ModelRegistry.get(key, version)` is the normal call; `latest(key)` is a
separate, differently-named one. There is no default that quietly follows the
newest thing registered, because that default is indistinguishable from correct
behaviour until the day it is not.

Registering the same `(key, version)` twice is **refused**. §32's "no silent
model replacement" expressed as an error rather than as a rule somebody has to
remember — a caller holding "regime v1.0" must be holding the same thing
tomorrow.

`test_a_bot_pinned_to_v1_keeps_v1_when_v2_is_installed` is §50 written out.

---

## 7. AI failure policy (§26, §49)

`AiPolicy` has two values and no third. "Carry on if it seems safe" is exactly
what an explicit policy exists to replace.

- **`AI_REQUIRED`** and the model gives no answer → `accept=False`. **No
  trade.** A model that could not answer is not a model that agreed.
- **`AI_OPTIONAL`** and no answer → the signal proceeds *to the risk engine*,
  which is unchanged and still authoritative.

`_no_answer` is the one place the policy is applied, so it cannot be applied
twice or differently in two branches. A feature build that *raises* is also "no
answer" — not an accept.

`ModelBackedFilter` lives in `app/execution/ai.py`, not in `app/ai/`. The
dependency runs one way — execution knows about ai, ai knows nothing about
execution — and that direction is the L22 import cycle not being repeated. A
test parses every module in `app/ai/` to keep it true.

---

## 8. Changes, classified

**ADD** — `app/ai/` (`contract.py`, `base.py`, `regime.py`, `probability.py`,
`anomaly.py`, `registry.py`, `service.py`), `app/api/v1/ai.py`,
`alembic/versions/0014_model_metadata.py`, `tests/test_ai.py` (56 tests), 11
API tests, `ModelTable.tsx` + 4 frontend tests.

**KEEP + MODIFY** — `app/execution/ai.py` (`AiPolicy`, `AiGate`,
`ModelBackedFilter` beside the existing seat), `app/models/ai.py` (provenance
columns, the promoted-provenance CHECK, `prediction_key` UNIQUE, the
refusal-carries-no-value CHECK), `app/models/base.py` (`NullableJSONType`),
`app/main.py` (an empty registry on app state),
`backend/requirements.txt` (numpy declared), `app/api/pending.py` and
`app/api/protected.py` (the `/ai/*` groups are built; no pre-v1 alias remains),
`tests/test_auth.py`, `frontend` nav and AI Lab page.

**REPLACE / REMOVE / RETRAIN** — nothing. There was no model to replace, no
model to retrain and no model deleted.

---

## 9. Safety invariants (§54)

| # | Invariant | How it holds |
|---|---|---|
| 1–4 | AI cannot execute, or bypass risk, sizing or the OMS | `app/ai/` imports none of them; a test parses every module. The only route out is `AiVerdict`, which has exactly four fields and none of them can approve |
| 5 | AI cannot enable live trading | Nothing in the package reads or writes a setting; all ten gates stay false |
| 6 | AI cannot modify broker positions | No adapter, no position manager, no order manager is importable |
| 7 | Predictions are versioned | `ModelIdentity` is on every prediction, and `prediction_key` is a hash of model, version, timestamp **and input** |
| 8, 9 | Feature and dataset versions tracked | On the identity, in the row, and in `model_versions` |
| 10 | Incompatible features block inference | `FEATURE_VERSION_MISMATCH`; compatibility must be declared, never assumed |
| 11 | Missing features do not become fake values | `MODEL_INPUT_ERROR` naming them; nothing imputes anywhere in `app/ai/` |
| 12 | Future data cannot influence a historical prediction | Tested end to end: append 100 bars at 5× the price, recompute, and the earlier prediction is identical, id included |
| 13 | Production models are not silently replaced | The registry refuses a duplicate `(key, version)`; the database refuses a promotion without provenance |
| 14 | AI failure follows an explicit policy | Two values, no implicit third, applied in one place |
| 15 | Probability is not presented as a guarantee | `label_definition` on every prediction; `calibrated` false until measured; the UI says so too |
| 16 | Existing valid models preserved | There are none, and nothing was touched |
| 17 | No secrets in model artifacts | `register_version` walks the params for credential-shaped keys and refuses |
| 18 | Live trading disabled by default | Asserted, as at every level |

---

## 10. Tests

| Suite | Before | After |
|---|---|---|
| Backend | 1269 passed, 15 failed, 3 skipped | **1335 passed, 15 failed, 3 skipped** |
| Frontend | 87 passed | **91 passed** |
| Research toolkit | 34 passed | 34 passed, untouched |

The 15 are the same 15 in both columns — `redis.exceptions`, because Docker is
not running here. Run in two chunks for the reason L23 records.

The five the brief writes out as scenarios:

- `test_a_bot_pinned_to_v1_keeps_v1_when_v2_is_installed` (§50)
- `test_a_feature_version_the_model_was_not_fitted_on_blocks_inference` (§51)
- `test_ai_required_and_no_model_means_no_trade` (§49)
- `test_future_data_cannot_change_a_prediction_for_an_earlier_timestamp` (§52)
- `test_the_ai_layer_cannot_reach_a_venue_or_the_risk_engine` (§54)

### 10.1 Three defects the tests found in this level's own code

**A refused prediction was stored as the JSON literal `null`, not SQL NULL.**
SQLAlchemy's `JSON` type serialises Python `None` that way by default, so the
`refusal_carries_no_value` CHECK — the constraint that exists to stop a refusal
carrying a value — failed on a refusal. Fixed with a `NullableJSONType` used
for that column, so the semantics live in the schema rather than in a call-site
discipline.

**The prediction id did not cover the input.** It hashed the model, version,
timestamp, symbol and timeframe — so two *different feature vectors* scored at
one nominal timestamp produced the same id and collapsed into one row.
`test_the_answer_rate_counts_the_refusals_too` recorded three predictions and
found one. §24's determinism is about the input, so the input is now part of
the identity, and `test_two_different_feature_vectors_are_two_predictions` is
the guard.

**A test that grepped source text for `app.execution` failed on a docstring
explaining that `app.ai` must not import it.** Rewritten to parse imports: a
text search cannot tell an explanation of a rule from a violation of it.

---

## 11. Remaining, and honest about it

- **No model is loaded, and none is fitted against real data.** The registry is
  empty at startup by design (§36), and every model in the tests is fitted on a
  seeded synthetic series because `market_bars` covers one instrument here. The interfaces
  are verified; no claim is made about a model's usefulness.
- **Nothing consults a model yet.** `ModelBackedFilter` exists and is tested,
  and the paper engine's AI seat still takes whatever filter it is handed —
  which is nothing. Wiring a model into a running pipeline means choosing a
  policy and a threshold per strategy, and that is a configuration decision
  L27 owns.
- **The probability model is uncalibrated.** `CalibrationReport` exists and is
  reported when supplied; measuring it needs a validation run, which is L25/L26.
  Until then the model says its probability is a ranking, not a frequency.
- **No expected-return, volatility, exit or position-management model** (§1's
  optional 4–7). Each would need a hypothesis and a label, and inventing either
  to fill a list is the speculative work the brief warns against.
- **No backtest or paper integration** (§40, §41). Both need a model somebody
  wants to run; the interfaces are shaped for walk-forward — a model carries its
  training period and refuses a feature version it was not fitted on — but
  nothing drives them yet.
- **The AI decision trace (§45) is half built.** A prediction records its model,
  version, feature version, input digest and outcome; joining that to the
  signal, the risk decision, the order and the trade needs the execution
  pipeline to pass a prediction id through, which is L27.
- **The deployment gap in §2 is only half fixed.** numpy is declared; the image
  still carries no `tools/`, so the indicator calls L23's features depend on
  would fail in a container. That is L41's, and it is now written down in three
  places instead of none.

---

## 12. The thing worth saying last

This level built the apparatus for a model, not a model that works. The
project's own measured position is unchanged and it is not encouraging: across
five universes and several hundred candidates, **nothing has cleared its own
permutation null out of sample**, and the one result that looked strongest
(`donchian_fade_55`, out-of-sample t = 2.79) dissolved under era blocks, date
clustering and a walk-forward.

A model faces the same three gates. What L23 and L24 add is that a model which
*appears* to clear them can now be checked — the dataset is versioned and
leakage-tested, the features are causal and dimensionless, the fit is
deterministic, and the prediction says what it is the probability of. That is
the useful outcome, and it is a smaller claim than "the AI layer is built".
