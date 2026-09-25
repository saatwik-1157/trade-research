# AI validation architecture (Level 26)

The question this engine answers is narrow, and the narrowness is the design:

> **not "is this model good", not "should we deploy it", but "does the evidence
> support a claim about this candidate at all, and if so, what claim".**

A validation run reads a trained candidate, measures it on data the training job
never saw, and writes one row to `validation_runs`. It changes no model's
status, reaches no venue, and nothing downstream acts on its result
automatically.

---

## 1. What was already here

L26 is the level with the most prior art in this repository, because the
methodology predates the platform. `MIGRATION_STATUS.md` has recorded L26 as
"COMPLETE (methodology)" since the first audit for exactly that reason. The
audit found:

| Component | Where | Verdict |
| --- | --- | --- |
| Permutation null, era blocks, walk-forward, date/symbol clustering, Bonferroni | `tools/rule_search.py` | **KEEP** — called, never copied |
| The backtest engine every measured figure came from | `tools/rule_backtest.py` (`simulate`, `stats`, `atr_series`) | **KEEP** — the only simulator |
| Brier, reliability bins, ECE, Benjamini-Hochberg, Welch's t, KS, PSI | `app/monitoring/stats.py` (L29 prep) | **KEEP** — imported |
| Six leakage checks, splits, scaler | `app/datasets/` (L23) | **KEEP** — its verdict is read, not recomputed |
| ML metrics, whose `compare()` says in its own docstring "this is NOT the L26 gate" | `app/training/metrics.py` (L25) | **KEEP** — read as the training record |
| Background job: queue / semaphore / cooperative cancel | `BacktestService`, `ReplayService`, `TrainingService` | **KEEP** — this is the fourth instance of one shape |
| Reading a stored artifact back into a model | *(absent)* | **ADD** — `app/ai/loader.py` |
| The checks, the report, the verdict rule, the job | *(absent)* | **ADD** — `app/validation/` |
| `(bars, dataset)` from one read of the raw layer | *(absent)* | **ADD** — `datasets/service.build_validation_loader` |

Nothing was replaced and nothing was removed.

`app/ai/loader.py` is worth calling out. L24 wrote `artifact_of()` — a fitted
model as JSON — and **nothing ever read it back**, because training holds its
model in memory and registers it in the same call. Validation is the first
consumer that starts from a database row, so the round trip had never been
exercised. It lives in `app.ai` rather than in `app.validation` because
deserialising an L24 model is an `app.ai` concern, and L28's promotion will want
the same function.

---

## 2. The pipeline

```
POST /v1/ai/validation/runs        (names a model version AND a dataset; no "latest")
        │
        ▼  writes a `queued` row, starts a background task, returns
ValidationService._run
        │
        ├─ loading         model version + (bars, dataset) from ONE read
        ├─ data_integrity  L23's own verdict, not a second opinion
        ├─ artifact        model_from_version(); feature set; version locking
        ├─ leakage         L23's six checks, read
        ├─ temporal        train < validation < test, proved from the timestamps
        │
        │  ── everything below is measured on the TEST segment only ──
        │
        ├─ baseline        candidate vs the majority baseline the run recorded
        ├─ calibration     ECE and reliability bins
        ├─ economic        tools/rule_backtest.simulate, spread charged
        ├─ walk_forward    AUC per sequential block
        ├─ robustness      threshold ±, cost ×
        ├─ regime          terciles of atr_pct_14
        ├─ significance    permutation null, Bonferroni-corrected
        └─ reporting       re-derive the dataset; block if it moved
        │
        ▼
`validation_runs` row: verdict + full report
        │
        ▼
A HUMAN reads it. (Promotion is L28's.)
```

---

## 3. The verdict is not a score

Section 27's requirement, and the single most important structural decision in
the level:

```
Data Integrity      PASS
Leakage Check       PASS
Temporal Validation PASS
Baseline Comparison PASS
Calibration         WARNING
Economic            PASS
Robustness          WARNING
Sample Size         PASS
──────────────────────────
Overall             CONDITIONAL
```

not

```
Score: 83/100
```

`verdict_from()` is precedence, not arithmetic:

```
BLOCKED  >  FAIL  >  CONDITIONAL  >  PASS
```

One failing check makes the verdict FAIL however many pass. There is no
weighting, because any weighting scheme can be tuned until it hides the check
that mattered — and a composite is exactly the shape a person reaches for when
they want a disappointing result to look acceptable.

`tests/test_validation.py::test_the_verdict_is_not_a_score` asserts the payload
has no key ending in `_score` and no `score` key at all, so a composite cannot
be added later without a test failing.

### BLOCKED is not FAIL

The distinction §26 draws, and the one most easily lost. **FAIL** says the
candidate is not good enough. **BLOCKED** says we could not tell.

A calibration check on 40 predictions has not measured calibration. Reporting
PASS *or* FAIL from it would be a claim the sample cannot support, so it reports
BLOCKED and names what was missing. BLOCKED outranks FAIL for the same reason: a
report containing an unevaluable check cannot honestly say the candidate failed.

Every check in `checks.py` can return BLOCKED, and several do so in the ordinary
course of events:

| Situation | Verdict | Not |
| --- | --- | --- |
| dataset is CLEAN rather than READY | BLOCKED | FAIL |
| 30 evaluation rows against a 200 minimum | BLOCKED | FAIL |
| no baseline recorded by the training job | BLOCKED | PASS |
| the artifact will not load | BLOCKED | FAIL |
| model fitted on a different dataset fingerprint | BLOCKED | FAIL |
| no trades generated at the decision threshold | BLOCKED | FAIL |
| fewer than 3 walk-forward windows | BLOCKED | PASS |
| no regime has a supportable sample | BLOCKED | FAIL |
| the leakage report is absent | BLOCKED | PASS |

That last one is the general rule: **a missing check is not a passing one.**

---

## 4. What a PASS authorises

The words are in the API payload, not only in a docstring, because the payload
is what a frontend renders and what an operator reads at 2am:

> the candidate satisfies the validation requirements that were checked, under
> the thresholds recorded in this report. It does **NOT** mean: deploy to live
> trading, allocate capital, or that the model will be profitable. It means the
> candidate may now be **CONSIDERED** by the model registry.

Three structural facts back that up rather than merely asserting it:

1. **`app/validation/` writes no model status.** `model_versions.status` is
   never touched. A test parses every module for an assignment to it.
2. **Migration 0018 deliberately adds no `validated` status to
   `model_versions`.** The obvious-looking addition would be a state nothing can
   enter — L25 declined to add `validation_passed` for the same reason. Whatever
   states promotion needs are L28's to add, when something can set them.
3. **The run status is `completed`, never `passed`.** A run succeeded or it did
   not; what it *found* is the `verdict` column. Collapsing the two is how "the
   validation passed" comes to mean "the model passed".

The frontend follows: a PASS badge is rendered in the `accent` tone, the same as
`validation_pending` on the training table — never `good`. BLOCKED is `warning`,
never `critical`, because rendering it in the same red as FAIL would teach the
exact confusion §26 exists to prevent.

---

## 5. Nothing here can trade

Section 30 and section 50, enforced rather than intended.

* No module in `app/validation/` imports `app.execution`, `app.oms`, `app.risk`,
  `app.sizing`, `app.brokers`, `app.orders`, `app.strategies`, `app.bots` or
  `app.paper`. `test_validation_cannot_reach_a_venue_or_an_order` parses every
  module with `ast` — not `grep`, which matched the docstring explaining the
  rule when L24 first tried it.
* The router has no `PATCH`, `PUT` or `DELETE`, and no path containing
  `promote`, `activate`, `deploy` or `approve`.
* `LIVE_TRADING` appears nowhere in the package.
* The economic evaluation is a **simulation over historical bars**. It calls
  `tools/rule_backtest.simulate`, which has no broker connection of any kind.

---

## 6. Where every figure comes from

**No figure in a report was invented.** Section 41: if the data cannot support a
check, the check reports BLOCKED and says why.

### Economic — one engine, not a second one

`app/validation/economics.py` turns the model's probabilities into the signal
array `simulate()` already takes, and hands it the same spread, the same
ATR-scaled bracket and the same next-bar-open entry rule. Sections 18 and 37
forbid a second simulator; the project has a stronger reason than the brief:

> every measured figure in `CLAUDE.md` — the −1.50 points per trade for
> `rsi_reversion`, the 50.5–52.7% breakeven win rates, the bracket sweeps, the
> exit searches — came out of `tools/rule_backtest.simulate`. A model evaluated
> by a different simulator could not be compared with any of them, and the
> comparison is the point.

The friction is inherited and real: entry at the **next** bar's open from a
signal read on closed bars, the spread charged on every trade, and the **loss**
booked when one bar's range covers both the stop and the target.

A prediction below the decision threshold is a `0` in the signal array — flat —
so `simulate()` skips the bar. That is what makes the threshold-sensitivity
probe meaningful: moving the threshold changes how many trades exist, not just
how they are scored.

### Significance — the model against its own shuffled self

Sections 13 and 23 ask for a baseline and a significance test. This project's
measured position is sharper than either:

> its searches produced a 36-cell sweep in which the *random* rule scored an
> in-sample t of 1.76 against the best real candidate's 0.83.

Beating a baseline is not evidence. `permutation_test()` shuffles the **outcomes**
against the model's own scores, which preserves the model's distribution of
confidence and the class balance, so the null pays the same costs and takes the
same exposure — and differs only in whether the predictions line up.

The seed is fixed: a validation result that changes between runs is not a
validation result. The p-value uses the Phipson–Smyth +1 on both sides, because
a p of exactly 0 claims more than 200 shuffles can support.

**Bonferroni over the candidates actually tried.** A model selected from twenty
training runs and quoted at p < 0.05 has been selected, not tested. The
correction is applied and the uncorrected figure is reported beside it, because
hiding either is how a search launders itself into a finding. Clearing α but not
the corrected α is a WARNING that says so in those words.

### Clustering — the toolkit's own implementation

`statistics.clustered()` calls `rule_search.clustered_by_date` and
`clustered_t`. Not reimplemented: this is the correction the project's own
results turned on, and a second copy would eventually disagree with the one
every recorded figure was measured under.

> every pair on the book has USD on one side, so one dollar move opens
> correlated trades in several at once. Pooling treats those as independent and
> inflates t by roughly the square root of how many fire together.

The date-clustered figure is the one to quote; the per-symbol one is a
consistency check and moves the other way. The pooled t is reported and never
quoted alone.

### Walk-forward — because aggregate performance is not the judgement

Section 11, and the check carries the project's own lesson in its evidence
payload:

> this repository's strongest-looking result held on a single split and
> dissolved under a walk-forward: re-ranked on only the eras before each test
> era, it was profitable in 1 fold of 4.

Blocks are sequential and never shuffled — the whole reason to look is that
performance concentrated in one era is an era rather than an edge, and shuffling
would average exactly that away.

### Overfitting — measured, not borrowed

L25 records an early-stopping figure taken from the **validation** segment,
which is not an in-sample one. So `_score_segment` is called twice — train and
test — and the gap is AUC on one against AUC on the other. Reading the stored
number would have compared two things that are not the same quantity.

---

## 7. No leakage, structurally

* **Every measured figure is on the final test segment**, and the report says so
  in `context.scored` with the exact index bounds.
* **The preprocessing applied is the one the model was fitted with**, rebuilt
  from the stored artifact. Refitting a scaler on the holdout would standardise
  it by its own statistics, which is the leak `Scaler.fit(rows, split)` is
  shaped to make unspellable.
* **Rows are aligned to bars by `bar_time`, never by position.** The builder
  drops a warm-up prefix and a horizon suffix, so dataset index *N* is not bar
  index *N*; treating it as one would shift every simulated entry by the warm-up
  length — silently, and in a direction that looks like a result.
* **The dataset is locked by fingerprint and re-derived at the end.** If the
  data moved under a running job, the report is BLOCKED rather than published
  against provenance that is a guess. Exactly what L25 does.
* **L23's six leakage checks are read, not recomputed.** A second
  implementation of the thing that has to be right is a second answer.

---

## 8. Thresholds

Section 25: every threshold is configurable and none is universal. A minimum
profit factor right for EURUSD H1 is wrong for a daily index, and a hardcoded
one would be a number nobody chose applied to instruments nobody checked.

| Threshold | Default | Why that number |
| --- | --- | --- |
| `minimum_samples` | 200 | below this, a figure's interval is wider than the differences read from it |
| `minimum_trades` | 30 | the economic figures need their own floor; the ML ones can hold when this does not |
| `minimum_walk_forward_windows` | 3 | fewer cannot show consistency |
| `minimum_auc` | 0.5 | deliberately low: the interesting failure is not a weak model but one that looks strong and does not replicate |
| `maximum_calibration_error` | 0.10 | a WARNING bar, not a FAIL bar |
| `minimum_profit_factor` | 1.0 | at SL=TP=1.5×ATR this broker needs a 50.5–52.7% win rate to break even, so barely over 1.0 is inside the noise the spread creates |
| `maximum_drawdown_ratio` | 0.5 | of gross profit, so it is comparable across symbols |
| `threshold_probe` | 0.05 | an edge that vanishes when the threshold moves this much is fitted to the threshold |
| `cost_probe_multiplier` | 1.5 | cost drag is the one effect this repository has measured to significance |
| `minimum_stable_share` | 0.6 | of probes and of walk-forward windows |
| `permutations` | 200 | precision against time; the floor is 20 and is enforced |
| `significance_alpha` | 0.05 | with Bonferroni applied over `candidates_tried` |
| `candidates_tried` | 1 | "no correction", which is only honest when one was tried |
| `maximum_train_test_gap` | 0.10 | AUC points |

The report stores what it used, so a report read a year later is not read
against today's defaults. Two runs at different thresholds have different
`config_fingerprint`s and are two runs, not a duplicate.

---

## 9. Calibration is a WARNING, not a FAIL

Deliberate, and worth the paragraph. A miscalibrated model can still rank
correctly, and ranking is what the AI seat uses it for. So a calibration failure
constrains **how the number may be read** rather than whether the model is
usable:

> the ranking may still be useful, but the probability must not be read as a
> frequency: 0.80 does not mean 80%.

Failing the candidate for it would reject a usable filter for a property it is
not being asked to have. This is the same reasoning L24 applied when it made
`calibrated` false until calibration has been measured.

---

## 10. The database

One table, `validation_runs`, created by migration `0018`. No existing table is
altered and no data is destroyed.

A new table rather than columns on `training_runs`, because a candidate can be
validated more than once — new data, changed thresholds, a re-check after a
disputed result — and a column holds one answer. Keeping runs separate makes
"validate again with a stricter profit-factor floor and compare" a query rather
than an argument.

Two CHECK constraints carry the level's rules into the schema:

```sql
status <> 'completed' OR (verdict IS NOT NULL AND report IS NOT NULL)
verdict IS NULL OR verdict IN ('PASS','FAIL','CONDITIONAL','BLOCKED')
```

A run claiming a conclusion with nothing behind it is a claim nobody can check,
and it will be read as a result anyway.

`status` is `String(16)` against a longest value of `cancelling` (10) and
`verdict` is `String(16)` against `CONDITIONAL` (11) — sized against the
vocabulary rather than by habit, because migration 0017 exists to fix a
`VARCHAR(8)` that was not.

---

## 11. API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v1/ai/validation/engine` | guarantees, what it will not do, default thresholds |
| POST | `/v1/ai/validation/runs` | queue a run; names a model version **and** a dataset |
| GET | `/v1/ai/validation/runs` | newest first, counted by verdict |
| GET | `/v1/ai/validation/runs/{id}` | one run and its config |
| GET | `/v1/ai/validation/runs/{id}/report` | every check and its evidence |
| POST | `/v1/ai/validation/runs/{id}/cancel` | cooperative |

No `PATCH`, no `PUT`, no `DELETE`. Every route requires `manage_ai_models`.

Every threshold is settable on the request and every one is range-checked by the
schema, so a caller cannot ask for `permutations=2` — a p-value from two
shuffles is not a p-value.

---

## 12. Tests

`tests/test_validation.py` — 75 tests. `tests/test_api_v1.py` — 11 more.

The ones that carry the level:

| Test | Rule |
| --- | --- |
| `test_a_blocked_check_is_not_a_failing_one` | §26 |
| `test_one_failure_cannot_be_averaged_away` | §27 |
| `test_the_verdict_is_not_a_score` | §27, by construction |
| `test_every_verdict_says_what_it_does_not_authorise` | §34 |
| `test_a_passing_report_promotes_nothing` | §34 |
| `test_the_run_status_never_says_passed` | §26 |
| `test_nothing_is_measured_on_data_the_model_was_fitted_on` | §12 |
| `test_the_in_sample_figure_is_measured_not_borrowed` | §19 |
| `test_a_dataset_that_moves_under_a_running_job_blocks_the_report` | §6 |
| `test_validation_cannot_reach_a_venue_or_an_order` | §30, §50, parsed |
| `test_validation_never_writes_a_model_version_status` | §34, parsed |
| `test_the_economic_figures_come_from_the_projects_own_engine` | §18 |
| `test_charging_more_spread_never_improves_the_result` | §17 |
| `test_a_p_value_of_exactly_zero_is_never_claimed` | §23 |
| `test_bonferroni_is_over_the_candidates_actually_tried` | §23 |
| `test_two_identical_runs_reach_the_same_verdict` | §24 |
| `test_a_report_carries_no_credential` | §46 |

### What the end-to-end run actually concluded

On 1,200 synthetic H1 bars — a random walk with a sine drift — a real training
run produced a candidate and validation returned **FAIL on significance**:

```
PASS     data_integrity       READY, 1138 rows, quality 1.000
PASS     sample_size          211 rows and 11 trades
PASS     model_artifact       trade_probability v1.0 loads, declares 4 features
PASS     version_locking      fingerprint matches the one the model was fitted on
PASS     leakage              all 6 checks pass
PASS     temporal             train 682 → validation 227 → test 229
PASS     baseline_comparison  log loss improves by 0.006599
PASS     discrimination       AUC 0.5641 over 211 rows
PASS     calibration          expected calibration error 0.0854
PASS     overfitting          in-sample 0.5910 against out-of-sample 0.5641
PASS     economic             profit factor 3.462 over 11 trades
FAIL     significance         p = 0.0784; the null's best shuffle scored 0.6093
                              against the model's 0.5641
WARNING  walk_forward         above chance in 2 of 4 windows
PASS     robustness           3 of 3 probes remain profitable
PASS     regime               profitable in all 1 regimes with a supportable sample
```

That is the correct answer and it is worth reading closely. The data is noise,
so a model fitted to it **should not** beat its own shuffled self — and the
engine says so even though twelve checks passed and the profit factor was 3.46.
An engine that rubber-stamped this would have looked identical on every other
check.

---

## 13. What Level 26 does not do

* It does not promote, activate or deploy anything — L28.
* It does not run on a schedule or re-validate a live model — L29's monitoring.
* It does not validate a model family with no probability output. The regime and
  anomaly models have no ranking to score, so the probability-dependent checks
  BLOCK on an empty list rather than substituting 0.5.
* It does not compare two candidates against each other. `compare()` in
  `app/training/metrics.py` compares a candidate to its baseline; ranking two
  candidates is a selection procedure and would need its own correction.
