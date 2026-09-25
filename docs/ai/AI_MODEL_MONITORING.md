# AI model monitoring and drift detection (Level 29)

The question this engine answers:

> **Is the currently used AI model behaving consistently with the conditions
> under which it was validated?**

And the answer it is never allowed to act on. Section 26 is the strict rule of
the level: monitoring detects, records and alerts. It never retrains, promotes,
replaces or deploys a model, and never modifies a strategy, a risk limit or a
position size.

---

## 1. What was already here — and the sentence that had gone stale

`app/monitoring/` has existed since before L24, with more built than any other
level's prior art:

| Component | Verdict |
| --- | --- |
| `stats.py` — PSI, KS, categorical PSI, Brier, reliability bins, ECE, Benjamini-Hochberg, Welch's t | **KEEP** |
| `checks.py` — eight checks, each returning a `Finding` | **KEEP + MODIFY** (four added) |
| `findings.py` — `Severity`, `Check`, `Action`, `FORBIDDEN_ACTIONS` | **KEEP + MODIFY** (four checks added) |
| `escalation.py` — the WARNING → PAUSE → REVIEW → RETRAIN → VALIDATE ladder | **KEEP**, untouched |
| `monitor.py` — `evaluate` and `run` | **KEEP + MODIFY** |
| `ai_decisions` (L27), `model_predictions` (L24), `model_deployments` (L28), `trades` (L19) | **KEEP** — read, never duplicated |
| `validation_runs` (L26) | **KEEP** — it is the baseline |
| L07 hub, `Notification`, workers, RBAC | **KEEP** |
| Collection, baselines, health, alert lifecycle, snapshots, API, frontend | **ADD** |

Nothing was replaced or removed. No second monitoring service, drift detector,
metrics pipeline or alert system was created — §51's list, item by item.

### The piece that was missing

`MonitoringInputs` was a dataclass a caller filled by hand, and **nothing filled
it.** The monitor had never run on a recorded inference, and `monitor.py`'s own
docstring said why:

> *"There is no model in this platform yet."*

That was true when it was written. L27 gave it `ai_decisions`, L28 gave it
`model_deployments`, and L29 added `collect.py` — the module that reads them.

---

## 2. The pipeline

```
MARKET DATA -> FEATURE PIPELINE -> AI INFERENCE -> AI DECISION -> TRADE OUTCOME
                                                       │              │
                                        ai_decisions (L27)      trades (L19)
                                                       └──────┬───────┘
                                                              ▼
                                            collect.py   (reads; computes nothing)
                                                              │
                                    baselines.py  ◄───────────┤ validation_runs (L26)
                                                              ▼
                                            monitor.evaluate  (pure; no session)
                                                              │
                                                   twelve findings
                                                              │
                    ┌────────────────────────┬────────────────┴──────────────┐
                    ▼                        ▼                               ▼
             health.derive()          snapshot row                    alert lifecycle
          (precedence, no score)   (reproducible, §34)          (fingerprint, cooldown,
                    │                        │                        recovery, §24-25)
                    └────────────┬───────────┴───────────────┬───────────────┘
                                 ▼                           ▼
                          ALERT  →  REVIEW           L07 hub events
                                 (a human decides)
```

**Never `DRIFT DETECTED → AUTOMATIC MODEL REPLACEMENT`.** The escalation ladder
tops out at `validate`, and `FORBIDDEN_ACTIONS` — `promote`, `deploy`,
`replace_production_model`, `rollback` — has been in `findings.py` since before
this level and is still asserted by a test.

---

## 3. The twelve checks

Eight were already here. L29 added four, and they measure the model's **service**
rather than the model:

| Check | Measures | Added |
| --- | --- | --- |
| `feature_drift`, `feature_distribution` | PSI + KS per feature, with Benjamini–Hochberg | |
| `prediction_drift`, `prediction_distribution` | the output distribution | |
| `calibration` | expected calibration error | |
| `performance` | realised R multiples, Welch's t | |
| `trade_outcomes` | losing streak, loss share | |
| `market_regime` | categorical PSI over the regime mix | |
| **`latency`** | p95, p50, p99 over a window | **L29** |
| **`availability`** | error rate by status | **L29** |
| **`confidence_drift`** | PSI over confidence, plus the extreme-prediction share | **L29** |
| **`possible_concept_drift`** | inferred, never measured | **L29** |

### `latency` reports the p95, not the mean

A mean hides the tail, and the tail is what a time-sensitive strategy actually
experiences. The median is reported beside it so a reader can see whether the
tail is the whole story.

This is about the **distribution over a window**. L27 already enforces a
per-signal budget and applies the configured failure policy when one call
exceeds it; latency never bypasses the risk engine.

### `availability` excludes `DISABLED` from the denominator

A strategy configured `AI_DISABLED` did not fail to answer — it was never asked.
Counting those would make **turning the AI off look like perfect uptime**.

### `confidence_drift` flags, it does not conclude

§10's rule. A model suddenly producing many 0.95–1.00 predictions has changed
behaviour whether or not it has become wrong, and the summary says so in those
words: *"a reason to investigate, not a conclusion that the model is wrong."*

### `possible_concept_drift` is inferred from one pattern only

§15 forbids claiming concept drift when only input drift was measured. So it is
never measured directly and is inferred from the single suggestive signature:

| Inputs | Outcomes | Conclusion |
| --- | --- | --- |
| moved | held | **NOT concept drift** — the market changed |
| held | degraded | **POSSIBLE concept drift** — investigation recommended |
| moved | degraded | not claimed; both moved |
| neither measured | | `INSUFFICIENT_DATA` |

The first row is the one a reader is most likely to misread, so the summary says
"NOT concept drift" explicitly rather than staying silent.

---

## 4. Baselines

§5 and §40: a drift score means nothing without the answer to *"drifted from
what"*.

| Kind | What it is |
| --- | --- |
| `VALIDATION` | the held-out segment L26 measured on. **Preferred**: those figures came from data training never saw, so a current window compared against them is compared against a held-out measurement |
| `TRAINING` | the training record. Weaker, and `reason` says so |
| `PAPER`, `PRODUCTION_REFERENCE`, `PREVIOUS_PERIOD`, `PREVIOUS_MODEL` | declared for the API and the schema |

**A baseline is never changed silently**, because changing it after an alert
makes the alert go away. Every snapshot stores the kind, the id and the period,
so two snapshots computed against different references are visibly different
rather than quietly incomparable.

**A baseline that does not exist is not a baseline of zeros.** `resolve()`
returns `None`, the comparison checks report `INSUFFICIENT_DATA`, and the
snapshot carries a note saying so. A drift score against a fabricated reference
would be a fabricated drift score.

---

## 5. Health, and the two states most often got wrong

There is **no health score.** §21 asks for measurable conditions, and `derive()`
takes the worst per-check state by precedence — the same shape L26's verdict
uses, and for the same reason: a weighted composite can be tuned until it hides
the check that mattered.

```
OFFLINE > CRITICAL > DEGRADED > WARNING > INSUFFICIENT_DATA > HEALTHY
```

**`INSUFFICIENT_DATA` sits above `HEALTHY`.** §43 is explicit that too little
data must never become a false healthy state, and a run in which half the checks
could not be evaluated is not a healthy run. A CHECK constraint enforces the
same thing in the schema:

```sql
health_state <> 'HEALTHY' OR sample_count > 0
```

**`OFFLINE` outranks everything.** A model that is not serving cannot be healthy
*or* degraded — every other reading would be about a model nobody is using. It
is a statement about the registry, not a clean bill of health.

**`DEGRADED` is reserved for the model.** A serious reading on a feature
distribution is a WARNING; the same severity on calibration, prediction drift,
confidence or performance is DEGRADED. That is §42 made operational: *"the
feature distribution changed"* and *"the model's own behaviour changed"* are
different events and an operator reacts to them differently.

---

## 6. Alerts: raise once, keep raised, say when it clears

Sections 22 to 25.

**A fingerprint, not a timestamp.** An alert's identity is
`(model version, scope, check, subject, severity)`. Drift on `rsi_14` at WARNING
is one alert however many times it is observed — and a PSI of 0.31 and one of
0.34 in the same band are the same problem observed twice.

**A severity change is a NEW alert.** The fingerprint includes the severity
precisely so `WARNING → CRITICAL` cannot be suppressed: that is a problem that
got worse, and it is the transition an operator most needs to see.

```
absent  → firing     NOTIFY
firing  → firing     SUPPRESS   (inside the cooldown)
firing  → firing     NOTIFY     (outside it; a day-old problem is worth repeating)
firing  → resolved   NOTIFY     (a recovery, with the time it cleared)
```

The suppression is the point. Without it a monitor that runs hourly produces 24
identical notifications a day, is muted within a week, and then **detects
nothing**.

**One row per condition, not per observation.** A partial unique index over
`fingerprint WHERE status <> 'resolved'` makes that a database guarantee.

**`INSUFFICIENT_DATA` does not raise an alert.** It is reported on the snapshot;
paging somebody because a window was quiet is how a monitor teaches people to
ignore it.

**Acknowledging is not resolving.** A person has *seen* it; the condition may
well still be there. Monitoring resolves an alert when the condition clears, and
never because somebody clicked.

---

## 7. Sample size, everywhere

§13 and §43. Every check has a floor and reports `INSUFFICIENT_DATA` below it
rather than a verdict. The floors differ because the evidence differs:

| | Floor | Why |
| --- | --- | --- |
| distribution checks | 100 | under about 100 a distribution statistic is dominated by luck — the research tooling's own figure |
| outcomes | 50 | outcomes are scarcer than predictions; a shared floor would never be met |
| trades | 30 | |
| latency | 30 | 30 timings is a distribution |
| availability | 20 | ten inference failures is an operational fact whether or not it is statistically surprising |

Every metric block on a snapshot carries its own `n`, and every alert carries
`sample_size`. A profit factor from seven observations and one from seven
hundred render identically otherwise.

**An unresolved decision is not scored as a loss.** Only a `filled` outcome
resolves. A risk veto, an AI rejection or a sizing refusal says nothing about
whether the model was right — and counting a veto as a wrong prediction would
make a conservative risk configuration look like a broken model.

---

## 8. Windows

§14: different metrics need different amounts of evidence, and none is
hardcoded.

| Window | Default | Used for |
| --- | --- | --- |
| health | 1 hour | latency, availability |
| drift | 7 days | feature and prediction distributions |
| performance | 30 days | calibration, performance — they need **outcomes**, and therefore time |

A latency figure is meaningful over an hour; a win rate is not meaningful over
anything short enough to be interesting.

---

## 9. Scope: never aggregate away the problem

§17 and §18. A snapshot covers one deployment — one model version, one strategy,
one symbol, one timeframe, one environment — and the collection is scoped at the
point the data is **read**, not at the point it is displayed. Aggregating EURUSD
and GBPUSD hides exactly the problem worth finding.

---

## 10. Trading impact, labelled as observational

§29 and §30. The snapshot's trading block reports the decision distribution, the
final outcomes, and the outcomes of accepted signals against rejected ones —
with this attached to every payload:

> **OBSERVED COMPARISON.** Accepted and rejected signals are not a randomised
> split: whatever separates them may be what the model was reacting to rather
> than what it caused. No causal claim is made from this.

§30 asks for that label. This repository has its own reason to insist on it: its
whole measured history is a record of differences that looked like effects and
were not.

---

## 11. What monitoring cannot do

§26 and §54, enforced rather than intended:

* **No module in `app/monitoring/` imports** `app.execution`, `app.oms`,
  `app.risk`, `app.sizing`, `app.brokers`, `app.orders`, `app.positions`,
  `app.paper`, `app.bots` or `app.training`. Parsed with `ast`.
* **No module calls** `promote`, `rollback`, `retire`, `deploy_to_paper` or
  `register` — parsed by name, so the registry's verbs are unreachable from
  here.
* **`LIVE_TRADING`, `LIVE_GATES`, `live_execution_allowed` and `get_settings`
  appear nowhere**, and nothing imports `app.core.settings`.
* **`FORBIDDEN_ACTIONS`** still forbids `promote`, `deploy`,
  `replace_production_model` and `rollback` from the recommendation vocabulary.
* **The escalation ladder tops out at `validate`**, not at `deploy`.
* **Never in the execution path.** §45: this reads rows that were already
  written. No execution code calls it, so a drift calculation has never delayed
  an order and cannot.

Verified after the change: `TRADING_MODE=paper`, `LIVE_TRADING=false`, and all
ten live gates still false.

---

## 12. Database

Two tables, migration `0021`. Nothing existing is altered.

`model_monitoring_snapshots` — §34's reproducible snapshot: version, scope,
baseline, exact window bounds, sample count, and one JSON block per metric
family. **A block that was not computed is NULL, not `{}`** — an empty object
reads as "measured, and nothing there".

`model_alerts` — §24's state machine: one row per open condition, fingerprinted,
with `resolved_at` when it clears.

Four constraints carry the rules into the schema:

```sql
window_end > window_start                              -- §34: reproducible
health_state <> 'HEALTHY' OR sample_count > 0          -- §13, §43
(status = 'resolved') = (resolved_at IS NOT NULL)      -- §25
UNIQUE (fingerprint) WHERE status <> 'resolved'        -- §24
```

The partial index is over a NOT NULL column, so the "NULLs are distinct" trap
L28's migration 0020 had to work around does not arise here.

---

## 13. API

| Method | Path |
| --- | --- |
| GET | `/v1/ai/monitoring` — what it measures and what it will not do |
| GET | `/v1/ai/monitoring/health` — current health per deployment |
| GET | `/v1/ai/monitoring/snapshots` |
| GET | `/v1/ai/monitoring/alerts` |
| POST | `/v1/ai/monitoring/alerts/{id}/acknowledge` |
| POST | `/v1/ai/monitoring/run` — run now |

Reads require `manage_ai_models`; acknowledging and running require the same.
There is no route that retrains, promotes, replaces or deploys.

---

## 14. Realtime

Three types on the existing L07 hub: `MODEL_HEALTH_CHANGED`,
`MODEL_ALERT_CREATED`, `MODEL_ALERT_RECOVERED`, all on the `model` scope L28
added. Health **changed** rather than health reported — a state identical to
last run is not an event, and publishing one every run is the flood the alert
deduplication exists to prevent.

---

## 15. What Level 29 does not do

* **Nothing has run on real inference.** `market_bars` covers one instrument, no strategy is
  configured for an AI mode, and no model is deployed. Every test runs against
  seeded `ai_decisions` rows shaped exactly as L27 writes them. **No monitoring
  figure in this repository describes a real model.**
* **No charts.** §37 asks that charts use real data and §52 forbids fabricated
  values for visual appearance. With nothing measured, a sparkline would be
  drawn from nothing — so the dashboard renders measured tables.
* **No automated retraining trigger.** §27 says keep it disabled by default;
  this goes further and does not build one. The escalation ladder recommends
  `retrain` and `validate` to a person, and there is no code path from a finding
  to a training job.
* **No correlated-exposure analysis.** §27 of L30's territory, and §27 here says
  not to build a fake correlation engine — so it is absent rather than
  approximated.
* **No scheduled run yet.** `POST /v1/ai/monitoring/run` runs on demand, and the
  worker that would call it on a schedule is L39's — §39 asks to use the
  existing worker infrastructure rather than add a scheduler, and the existing
  one is deliberately idle until an operator starts it.
