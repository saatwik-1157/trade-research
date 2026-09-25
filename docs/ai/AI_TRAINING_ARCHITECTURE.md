# AI_TRAINING_ARCHITECTURE.md

Level 25. The document §43 asks for: the architecture, the job lifecycle, the
data flow, versioning, artifacts, failure handling, security, the validation
handoff, and where promotion stops.

---

## 1. The one sentence

**Training produces a candidate.** A successful run ends at
`validation_pending` and writes a **draft** model version. It does not promote,
deploy, replace or enable anything, and there is no route, method or code path
in `app/training/` that could.

---

## 2. Architecture

```
   POST /v1/ai/training/jobs        validates, writes a row, returns immediately
        |
        v
   TrainingService.queue()          bounded: 2 concurrent, 3 per user
        |                           refuses a duplicate recipe already running
        v
   asyncio background task          the pattern BacktestService has used since L14
        |
        v  ── stage: loading_dataset
   loader(db) -> Dataset            L23 rebuilds the rows from market_bars
        |                           fingerprint LOCKED here
        v  ── stage: quality_gates
   gates.evaluate()                 READY? leakage passed? enough rows? ordered?
        |                           features present? both classes? -> refuse
        v  ── stage: splitting
   dataset.split                    chronological, from L23. Nothing re-splits.
        |
        v  ── stage: preprocessing
   Scaler.fit(rows, split)          reads split.train. No other spelling exists.
        |
        v  ── stage: fitting_baseline
   fit_majority(train targets)      the prior every candidate must beat
        |
        v  ── stage: fitting_candidate
   asyncio.to_thread(fit)           OFF the event loop; cancellable per iteration
        |                           early stopping reads VALIDATION only
        v  ── stage: evaluating
   metrics.classification()   +   metrics.economic()      never merged
   metrics.calibration()      (from app.monitoring.stats, L29)
   metrics.compare(candidate, baseline)
        |
        v                           dataset fingerprint RE-DERIVED and compared
        v  ── stage: recording
   ai_service.register_version(status="draft")
        |
        v
   status = validation_pending   ->   L26 validates   ->   L28 promotes
```

---

## 3. Job lifecycle

Seven statuses. Five existed on `training_runs` since L05; `cancelling` and
`validation_pending` were added.

| Status | Means |
|---|---|
| `queued` | recorded, waiting for a slot |
| `running` | a background task holds it |
| `cancelling` | we asked it to stop; it has not stopped yet |
| `cancelled` | it stopped at a boundary. **Never** a trained model |
| `failed` | it stopped for a reason recorded in `error`. No candidate |
| `finished` | pre-existing; nothing writes it |
| `validation_pending` | **success**: a candidate exists and awaits L26 |

**Three statuses §5 lists are deliberately absent.** `paused` — nothing here
can pause; these fits are seconds long and there is no checkpoint to resume
from, so the state would be unreachable. `validation_passed` and
`validation_failed` — level 26's to write, and they should arrive with the code
that writes them. Declaring a value nothing can enter makes the vocabulary a
wish list, which is the reasoning L22 used on bot states and L23 on dataset
ones.

**There is no `completed`.** §25 is the reason: "candidate model successfully
trained" is not "model approved for trading", and a status called `completed`
would be read as the second.

---

## 4. Versioning and the dataset lock

§6 asks a job to record the exact data it trained on. A version string records
what the data was *called*; L23's fingerprint covers the dataset configuration
**and a digest of the bars**. So the job:

1. builds the dataset and stores `dataset_fingerprint` before fitting;
2. re-derives it after the fit;
3. **fails** if it moved, discarding the candidate rather than recording one
   against provenance that is a guess.

That turns §6 from a rule into a check. `test_a_dataset_that_moves_under_a_running_job_fails_it`
is the proof.

`config_fingerprint` is a hash of the recipe *without* the data, and it is what
§38's duplicate check uses: two runs of the same recipe over the same named
dataset are the duplicate that section is about. It is indexed rather than
unique, because re-running a recipe after it finished — on new data — is
something somebody legitimately wants.

Every candidate carries `feature_version`, `label_version`, `dataset_version`,
`dataset_fingerprint`, `preprocessing_version` and `code_version` on its
`model_versions` row, and the database refuses to promote one that cannot state
them.

---

## 5. Leakage prevention

Nothing here re-implements it. The guarantees come from L23 and are *checked*
rather than restated:

- The dataset must be `READY`, which L23 grants only when six leakage checks
  pass and which no route can override.
- The split is L23's, chronological, and there is **no shuffle option** — not
  in `TrainingConfig`, not in `SplitConfig`, not in `app.datasets.splits`.
- `Scaler.fit()` takes rows **and a split** and reads `split.train`. There is
  no function that takes a whole dataset, so §10's mistake has no spelling.
- Early stopping reads the **validation** segment. The test segment is never
  consulted for any decision, which is §16.
- Nothing resamples. §15 permits controlled resampling inside training data;
  class weighting achieves the same thing without duplicating rows, and
  duplicating rows in a time series creates the same bar twice at one
  timestamp — which every causal guarantee downstream assumes cannot happen.

---

## 6. Preprocessing and artifacts

The scaler's parameters, the fitted model's parameters, the training
configuration, the environment and the feature schema are stored together in
`model_versions.params` as JSON.

§20 says not to put a large binary model in a database unless the architecture
intends to. These are not large — a handful of floats each — and keeping them
structured means a version can be inspected, diffed and queried rather than
only loaded. When a family arrives whose parameters are megabytes it will need
object storage, and that is a decision to take then rather than now.

`app.ai` reads exactly these parameters at inference, so the model that
predicts is the model that was fitted; there is no conversion step that could
disagree with the fit.

---

## 7. Metrics: two kinds, never merged

`metrics.classification()` returns accuracy, precision, recall, F1, log loss,
Brier and the confusion matrix — **always beside `majority_share`**, because a
labelled set that is 70% WIN gives 70% accuracy to a model that always says
WIN, and the comparison is the only thing that makes the first number mean
anything.

`metrics.economic()` returns expected value, win rate, profit factor and the
worst drawdown, from the label's own forward return. It says in its own output
that it is **not a backtest**: no sizing, no financing, no slippage beyond the
spread the label set charged, every trade weighted equally.

§22 forbids treating ML accuracy as trading profitability, and this repository
has measured exactly that shape — rules that separate from a coin flip on no
reading and still lose to the spread. A single merged "score" would hide it.

`metrics.compare()` calls a candidate **meaningfully better** only when it beats
the baseline on log loss *and* on accuracy *and* clears the majority share. One
metric moving is not a result: the project's record is a long list of candidates
that beat something on one reading and nothing out of sample. Its own output
says it is not the L26 gate — no permutation null, no correction for the number
of candidates tried, no walk-forward.

---

## 8. Failure handling

| Failure | What happens |
|---|---|
| dataset missing, corrupt, unordered, too small | a blocking gate refuses; job `failed`, no candidate |
| dataset not READY | refused at the API before a job is created |
| a required feature absent | gate refuses, naming it |
| one class absent | gate refuses |
| the dataset moves mid-run | the fingerprint check fails the job |
| the fit raises | `failed` with the exception recorded; no candidate |
| cancellation | `cancelled` at a boundary; no candidate |
| too many jobs | `TrainingBusy`, a refusal rather than an unbounded queue |
| a duplicate recipe already running | `DuplicateTrainingJob` |
| process shutdown | `TrainingService.shutdown()` stops and awaits every job |

**A failed or cancelled job never leaves a candidate behind**, and never
touches an existing model. §27: if a new candidate is invalid, the previous
validated model is untouched — which holds trivially here, because nothing in
this package can write to a promoted version at all.

---

## 9. Security

- Every training route requires `Permission.manage_ai_models` (TRADER and
  above). Reading jobs requires it too: a training job names datasets, feature
  sets and model parameters.
- The configuration is a typed body with bounded numeric fields. There is no
  free-form parameter blob, no model path, no dataset path and no code field —
  §35's "do not accept arbitrary Python code as a training strategy" is
  satisfied by there being nowhere to put it.
- An unknown `class_weight` or model family is **refused**, not ignored: a
  caller who asked for weighting should not silently get none.
- `ai_service.register_version` walks the stored parameters for
  credential-shaped keys and refuses. §31's logging rules hold: the service logs
  a job id, an error type and a stage, never a payload.

---

## 10. Where promotion stops

```
   training  ->  candidate (draft)  ->  L26 validation  ->  L28 registry  ->  promotion
      ^                                                                          ^
      |                                                                          |
   this level                                                          NOT this level
```

`app/training/` contains no path to `app.execution`, `app.risk`, `app.sizing`,
`app.oms`, `app.brokers`, `app.positions` or `app.strategies` — a test parses
every module. The string `"promoted"` does not appear in the package, which a
second test asserts. There is no PATCH, PUT or DELETE on `/v1/ai/training`.

---

## 11. What is not built, and why

- **Checkpointing (§17).** These fits complete in under a second; a checkpoint
  would be state to maintain for a resume nobody needs. When a family arrives
  whose fit takes hours, it will need one — and `TrainingStage` already gives
  the boundaries to write it at.
- **Hyperparameter search (§14).** Deliberately absent. This repository's
  central measured finding is that searching a grid produces winners that do
  not survive out of sample — a 36-cell bracket sweep whose best in-sample t was
  0.83 while the *random* rule scored 1.76 on the same grid. Adding a search
  before there is a validation harness that corrects for it would be building
  the exact machine the project's own results warn about. It belongs after L26.
- **Scheduled retraining (§32).** No scheduler exists to integrate with, and
  §32 warns against enabling aggressive retraining. Nothing here is on a timer.
- **GPU and disk limits (§19).** Concurrency and per-user limits are enforced;
  CPU and memory are not capped, because the fits are small and a cgroup is a
  deployment concern (L41).
- **A model trained on real data.** `market_bars` covers one instrument, so
  every test trains on a seeded synthetic series. §48 is explicit that a
  blocker should be reported rather than papered over: **the infrastructure is
  verified; no claim is made about any model's usefulness.**

---

## 12. The thing worth saying last

This level built the machine that trains a model correctly. It did not find a
model worth training.

The project's own measured position is unchanged: across five universes and
several hundred candidates, nothing has cleared its own permutation null out of
sample. What L23, L24 and L25 add is that a candidate which *appears* to clear
it can now be checked — versioned and leakage-tested data, a deterministic fit,
a baseline reported beside every candidate, and metrics that refuse to let ML
accuracy stand in for money.

That is a smaller claim than "the AI system is built", and it is the one the
evidence supports.
