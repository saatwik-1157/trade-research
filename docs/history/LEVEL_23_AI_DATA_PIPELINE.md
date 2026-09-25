# LEVEL 23 — AI DATA PIPELINE

Completed 2026-09-04. Adaptive build mode: audit → baseline → plan → implement
→ integrate → test → verify → document.

The first level of the run 18–23 that is genuinely a fresh start. The execution
spine is closed; this begins the data side, and it trains nothing.

---

## 1. What already existed

Far more than the status table suggested, and the audit's main result is how
little of this level needed building.

| Component | State before | Verdict |
|---|---|---|
| `app/marketdata/` — `Bar`, `Quote`, `Series`, `Availability`, 8 timeframes | L08. Normalised, with per-field availability | **KEEP** — this *is* the canonical representation §5 asks for |
| `app/marketdata/validation.py` | `check_bar`, `find_duplicates`, `find_out_of_order`, `find_gaps`, `open_equals_close_rate`, `inspect_series`, `deduplicate` — all of which **flag and never repair** | **KEEP** — §7, §8 and §9 were already implemented |
| `market_bars` table | L08, keyed `(provider, provider_symbol, timeframe, bar_time)` with DB CHECKs for OHLC consistency and positive prices | **KEEP** — the raw layer §10 requires, already idempotent |
| `app/strategies/indicators.py` | SMA, EMA, RSI, ATR with declared `Unit`, bounded parameters and warm-up, calling `tools/rule_backtest.py` and `tools/rule_search.py` | **KEEP** — §14's "do not create a second indicator engine" |
| `app/backtest/runner.build_signal_vector` | The prefix walk: at index i the strategy sees `bars[:i+1]` and nothing more | **KEEP** — the causality discipline this level extends |
| `app/monitoring/` | L29: PSI, KS, calibration, Brier, BH-FDR, `market_regime` | **KEEP** — §39's drift preparation is already there |
| `models`, `model_versions`, `training_runs`, `model_predictions` | L05 | **KEEP** — §47's "do not delete existing models"; there are none to delete |
| `trades` (252 imported + paper), `signals`, `backtests` | L05/L09/L14 | **KEEP** — the sources for §22 and §23 labels |

**Nothing was duplicated.** No second `MarketDataService`, `DataPipeline`,
`IndicatorEngine`, `DataLoader` or storage system was created, and a test parses
every module in `app/datasets/` to prove the package imports no broker, no OMS,
no risk engine, no sizing calculator and no ML framework.

---

## 2. The gaps, and only these

Six, and each is a real absence rather than a rewrite:

1. **No feature engine.** Indicators existed; nothing turned bars into rows.
2. **No label engine.** Nothing computed what happened *after* a bar.
3. **No dataset identity.** No manifest, no version, no fingerprint.
4. **No leakage detection.** The backtester was causal *by construction*, and
   nothing checked that a new consumer was.
5. **No chronological split or walk-forward folds** outside `tools/rule_search.py`,
   which does it for rule candidates rather than for datasets.
6. **No OHLCV resampling.** `market_bars` stores whatever a provider serves.

---

## 3. The decision that shapes every feature

**Every feature is dimensionless. There is no raw price level in the
catalogue.**

This is the project's own hardest-won lesson applied to machine learning. From
`CLAUDE.md`: pooling "points" across symbols whose median H1 ATR runs from 160
(silver) to 9,386 (palladium) produced a +4,236 out-of-sample headline that was
**an arithmetic error, not a finding** — and `rule_search.py` now raises a data
gap whenever that spread exceeds 5x.

A model trained on `sma_20` as a price level learns the price of the
instrument. So a moving average appears only as `sma_distance_20`
(`close / SMA - 1`), volatility only as `atr_pct_14` (`ATR / close`), and the
raw ATR — the number every bracket in this repository is quoted in — is
deliberately **not offered as a feature at all**.

22 features across price shape, trend, momentum, volatility, volume, market
structure and time. The last of those includes `is_rollover_hour`, which is
measured rather than guessed: `cost_profile.py` puts server hour 00 at 4x the
normal spread, and 14.3% of M1 bars in its first minute exceed a 1.5×ATR
bracket.

---

## 4. Causality, and how it is proved

`no_future_influence` is §56 run literally: compute features over the first two
thirds of a series, append the rest, recompute, and require **exact** equality
on every overlapping row. Not a tolerance — a causal calculation over identical
input produces identical output, and treating a 1e-15 difference as noise is how
a real leak survives its own test.

The test that matters more is `test_the_leakage_check_reports_a_feature_that_reads_forward`:
a deliberately non-causal feature (`next_close`) is fed to the same check and
must FAIL. Without it, a passing check would only prove that the check runs.

Six checks in total, and a failure blocks the dataset:

| Check | What it catches |
|---|---|
| `no_future_influence` | a rolling window that reaches forward, a scaler over the whole array, a centred average |
| `labels_are_not_features` | the answer used as an input |
| `splits_are_ordered` | a holdout that is not held out |
| `scaler_fitted_on_train_only` | parameters that saw the test period |
| `labels_have_a_future` | a labelled row within `horizon` bars of the end |
| `feature_timestamps_are_causal` | a row paired with the wrong bar |

---

## 5. Normalisation: the mistake has no spelling

`Scaler.fit()` takes rows **and a split**, and reads only `split.train`. There is
no function in `app/datasets/scaler.py` that takes a whole dataset and returns a
scaler, so §17's mistake cannot be expressed. A caller who genuinely wants one
over the whole series has to construct a split that says so — which then shows
up in `fitted_rows` and is caught by the leakage check.

`refit_changes_nothing` re-fits and compares rather than trusting the claim, and
a feature whose training values never varied is named in `constant_features` and
passed through unchanged: standardising by a zero deviation is a division by
zero, and quietly substituting 1.0 turns a constant into a signal.

---

## 6. Labels, and the two refusals in them

`AMBIGUOUS` is a label outcome. When one bar's range contains both the stop and
the target, bar data cannot say which traded first, and picking one is inventing
the half of the record that is missing. The project has a live example of what
guessing costs: position 10200315596, a 282-point M1 range that put both exits
on the wrong side of the fill.

`LabelConfig` has **no zero-cost default**. `spread_points` is required, matching
`BacktestConfig` and for the same measured reason: cost drag is the only effect
in this repository large enough to reach significance, and a `WIN` computed
without the spread is a label for a market nobody trades in.

The tail is dropped, never filled. The last `horizon` bars have no future;
filling them with anything teaches a model that the end of a dataset is a
particular kind of market.

---

## 7. A dataset is a recipe, not a pile of rows

No table stores a training row. §30 asks for reproducibility, and if
reproducibility holds then storing the rows is optional — while storing them
would be a second copy of `market_bars` that can drift from it. The backtester
already works this way.

So the fingerprint covers the config **and a digest of the raw bars**: without
the second half it would say "same recipe" and be read as "same dataset".
`test_the_same_bars_and_configuration_rebuild_the_same_dataset` and
`test_different_bars_under_the_same_recipe_are_a_different_dataset` are the pair
that pins it.

Migration `0013` adds four tables: `feature_sets` and `label_sets` (immutable,
unique on `(key, version)`), `datasets`, and `dataset_checks`. The constraint
worth reading is `ready_is_reproducible` — a row may only claim READY while it
carries a fingerprint and a positive row count, because a dataset that says it
is ready to train on without the identity that reproduces it is a claim nobody
can check.

Three statuses, not six. §35 also names LOCKED, TRAINING and ARCHIVED; they
belong to levels 25 and 28, and declaring a value nothing can write makes the
vocabulary a wish list — the same reasoning that kept `created` out of the bot
run states at L22.

---

## 8. What the pipeline refuses

- **A corrupt series blocks at RAW.** A bar with `high < low` is not a market
  event, it is a corrupt row.
- **Out-of-order bars are refused, not sorted.** Every causal guarantee assumes
  an ordered series; sorting here would hide an upstream ordering problem.
- **A gap is a warning, not a block.** A closed market is a fact about the
  series. Nothing is filled.
- **A real market anomaly is kept.** §40: a crash is data, and deleting it is
  how a dataset stops describing the market it came from.
- **A source whose OHLC is untrustworthy loses its candle-shape features, not
  its series.** `SeriesQuality.ohlc_trustworthy` already encodes the thresholds
  `tools/market.validate_ohlcv` measured — an unvalidated Yahoo FX series
  produced a t-statistic of 28 from bars whose close sat outside the day's
  range. Close-based features are unaffected.
- **Mixed providers cannot be aggregated.** Two providers' bars are different
  measurements of different books, which is why `provider` is in `market_bars`'
  identity.
- **A dataset of fewer than 10 usable rows is blocked** rather than padded.
- **A single split is labelled as a single observation.** Its own output carries
  the warning, because the project has a worked example: `donchian_fade_55` was
  called `holds_out_of_sample` by splits at 0.5, 0.7 and 0.85 — one recent era
  counted three times, and the walk-forward is what dissolved it.

---

## 9. Changes, classified

**ADD** — `app/datasets/` (`features.py`, `labels.py`, `resampling.py`,
`quality.py`, `splits.py`, `scaler.py`, `leakage.py`, `builder.py`,
`service.py`), `app/models/datasets.py`, `app/api/v1/datasets.py`,
`alembic/versions/0013_datasets.py`, `tests/test_datasets.py` (68 tests), 14 API
tests, `DatasetTable.tsx` + `FeatureRegistry.tsx` + 3 frontend tests.

**KEEP + MODIFY** — `app/models/__init__.py` (four tables registered),
`app/api/v1/__init__.py` (the router mounted), `frontend/src/lib/services.ts`
(`datasetService`), `frontend/src/lib/nav.ts` (`/ai-lab` is L23 and partial),
`tests/test_models.py` (41 → 45 expected tables).

**REFACTOR** — `PlannedPage.tsx` gains `PlannedSections`, so `/ai-lab` can have
real panels above its planned ones without a second copy of the "not built"
grid.

**REPLACE / REMOVE** — nothing. No existing model, table, column or function was
deleted or rewritten.

---

## 10. Safety invariants (§61)

| # | Invariant | How it holds |
|---|---|---|
| 1 | Raw data is never silently overwritten | The package has no write path to `market_bars`; a test greps for one, and another reads the table back after a build and compares |
| 2 | Future information cannot become a feature | `no_future_influence`, and a negative control proving the check discriminates |
| 3 | Labels are separated from features | `labels_are_not_features`; the two engines take disjoint bar ranges |
| 4 | Training normalisation cannot use validation or test | `fit()` takes a split and reads only `train`; the check re-fits |
| 5 | Test data is chronologically later than training | `splits_are_ordered`; nothing shuffles, and unordered input is refused |
| 6 | Dataset versions are reproducible | The fingerprint covers config + bars; two builds compared row for row |
| 7, 8 | Feature and label versions tracked | On every row, in the manifest, and in two immutable tables |
| 9 | An invalid dataset cannot be READY | The builder decides; there is no PATCH, PUT or DELETE on `/v1/datasets`, asserted by walking the route table |
| 10 | Leakage failure blocks readiness | Status stops at CLEAN with the failure attached |
| 11 | Missing data is not fabricated | Rows are dropped and counted; no imputation exists |
| 12 | Real anomalies are not deleted | Only corrupt rows block; gaps and outliers are reported |
| 13 | Existing history is preserved | Migration 0013 is four `CREATE TABLE`s and nothing else |
| 14 | Existing models are not replaced | None exist, and nothing here writes to `models` or `model_versions` |
| 15 | L23 does not enable AI trading | The package imports nothing that trades; `LIVE_TRADING` is false and all ten gates are false |
| 16 | No secrets in datasets | A dataset holds bars, ratios and outcomes; no credential is reachable |
| 17 | TradingView data through legitimate access only | Unchanged: L09's gateway is the only ingress |
| 18 | Broker data normalised through existing infrastructure | Bars come from `market_bars`, written by `app.marketdata` |

---

## 11. Tests

| Suite | Before | After |
|---|---|---|
| Backend | 1187 passed, 15 failed, 3 skipped | **1269 passed, 15 failed, 3 skipped** |
| Frontend | 84 passed | **87 passed** |
| Research toolkit | 34 passed | 34 passed, untouched |

The 15 backend failures are the same 15 in both columns: `redis.exceptions`,
because Docker is not running on this machine. Environmental, and named as such
rather than counted as a pass.

`tests/test_datasets.py` is **68 tests** where there were none.
`tests/test_api_v1.py` gains **14**. The frontend gains **3**.

Lint, format and type checks are clean on both stacks: `ruff check`,
`ruff format --check` and `mypy` over 215 backend files; `eslint`,
`tsc --noEmit` and a production build on the frontend.

**The suite was run in two chunks, and that is worth recording.** A single
full run stalled at ~96% with flat CPU while a frontend production build was
running beside it; killed and split, the two halves ran clean
(`--ignore=tests/test_webhooks.py --ignore=tests/test_workers.py` gave 1201
passed / 14 failed / 3 skipped in 446s, and the two ignored files gave 68
passed / 1 failed in 318s). `test_webhooks.py` alone takes five minutes because
its rate-limiting test retries against a Redis that is not there, which is why
every run in this project appears to hang at 95%.

The three the brief calls critical:

- `test_a_feature_at_T_does_not_change_when_the_future_arrives` (§56)
- `test_a_scaler_cannot_be_fitted_on_anything_but_the_training_segment` (§58)
- `test_the_same_bars_and_configuration_rebuild_the_same_dataset` (§59)

And the two that prove the checks can fail:

- `test_the_leakage_check_reports_a_feature_that_reads_forward`
- `test_the_leakage_check_catches_a_scaler_fitted_on_the_whole_series`

---

## 12. Remaining, and honest about it

- **Only one dataset has been built from real data** -- `eurusd-h1` v1, READY,
  638 rows over January 2026, from one instrument. Breadth is what is missing,
  not existence; an earlier version of this line said none had been built.
  Every test runs on a seeded
  synthetic series, because `market_bars` covers one instrument — MT5 is not
  connected and no ingestion has run. The pipeline is verified; its output on
  this broker's history is not, and that is a different claim.
- **Multi-symbol datasets are built by combining single-symbol ones.** One
  symbol and one timeframe per dataset, deliberately: §42 requires the
  instrument to travel with the row, and pooling before that question is
  answered is the metals error again. The combining step is not written.
- **Cross-asset features (§43) are not built.** They need two synchronised
  series and a stated alignment rule, and getting it wrong means silently using
  a later bar from the other instrument.
- **The 252 completed trades are not yet a label source.** §23's trade-outcome
  labels need a join from `trades` back to the bar that opened them, and the
  imported ledger does not carry a bar reference. Stated rather than implied.
- **TradingView signals are not yet dataset rows (§33), and the AI decision
  record (§45) does not exist.** `signals` and `webhook_events` have held every
  alert since L09, and `risk_decisions`, `orders` and `trades` hold what
  happened to each one — so the join that would produce a
  signal → features → decision → outcome row is describable but not written. It
  is the natural next dataset and it belongs after L24, when there is an AI
  decision to record.
- **Incremental updates (§53) are not built.** A build reads the window and
  recomputes it. At the sizes here that costs seconds; at millions of bars it
  would not, and the honest answer is a stated bound (`MAX_BARS`) rather than a
  claim of streaming that is not implemented.
- **Corporate actions (§41) are not handled.** No equity instrument is mapped,
  and applying split adjustment to FX would be worse than not having it.
- **Monitoring (§50) reuses L29 rather than adding a second system**, and is not
  wired to a schedule — like every other worker in this platform, starting it is
  an operator action.
