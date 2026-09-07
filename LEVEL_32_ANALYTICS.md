# Analytics and performance analytics (L32)

Built 2026-09-04. Performance over what actually happened, from the systems that
already own it.

---

## 1. The audit found three copies of one formula

Before writing anything, §2 asks what already exists. It found the metric
definitions implemented three times:

| Where | Denominated in | Computes |
|---|---|---|
| `app/backtest/runner.py` `compute_metrics` | **points** | win rate, PF, expectancy, Sharpe, Sortino, max drawdown, t |
| `app/training/metrics.py` `economic` | **label returns** | win rate, PF, expectancy, max drawdown |
| `app/services/journal.py` `statistics` (L31) | **account currency** | win rate, counts, sums |

**They agreed.** Three copies of one formula do not stay agreeing, and the one
nobody looked at is the one somebody quotes. So L32 did what L18 did to
`value_per_price_unit` and L22 and L27 did to `AiVerdict` and `AiPolicy`:
extracted the definitions into `app/analytics/metrics.py` and pointed the
existing callers at it.

**The output shapes did not change.** `compute_metrics` still returns
`NOT_AVAILABLE` rather than `INSUFFICIENT_DATA`, because the backtest API has
served that token since L14 and renaming it would be a breaking change for a
cosmetic gain. §50 point 8: preserve existing API compatibility. 98 backtest and
training tests pass unchanged, which is the evidence the refactor moved no
number.

### Everything else, classified

| Component | Verdict |
|---|---|
| `trades` (L31) — the journal, one row per position episode | **KEEP**, read |
| `portfolio_snapshots` (L05), `PortfolioService` (L30) | **KEEP**, delegated to |
| `orders`, `executions` (L19) | **KEEP**, read |
| `ai_decisions` (L27) | **KEEP**, read through a 1:1 join |
| `backtests.summary` (L14) | **KEEP**, served, never recomputed |
| `app/monitoring/*` (L29) — drift and model health | **KEEP**, untouched — §53 |
| `app/validation/statistics.py` (L26) | **KEEP**, untouched |
| `app.risk.state.day_start` (L17) | **KEEP**, reused for the trading day |
| `/analytics/summary` 501 stub | **REPLACE** |
| Frontend `PlannedPage` at `/analytics` | **REPLACE** |
| `app/analytics/{metrics,equity,windows,service}.py` | **ADD** |
| `app/api/v1/analytics.py` — 11 GET routes | **ADD** |

**No new table and no migration.** §26 says to create structures only if
necessary. Every figure is aggregated from `trades`, `orders`, `executions` and
`portfolio_snapshots` at read time; a materialised snapshot would be a fourth
copy of the same facts and would need invalidating whenever a reconciliation
changed a trade — which §59 says must eventually be reflected.

---

## 2. Source of truth, kept literally

```
   TRADE      the journal (L31)          realized P&L, win/loss, expectancy,
                                          profit factor, duration, streaks
   POSITION   the position manager (L21)  counted here, VALUED by the portfolio
   ACCOUNT    the portfolio engine (L30)  balance, equity, margin, exposure
   ORDER      the OMS (L19)               counts, states, latency, slippage
   AI         ai_decisions (L27)          model, exact version, regime
   BACKTEST   the backtest engine (L14)   read; analytics runs no simulator
```

`AnalyticsService.exposure` calls `PortfolioService` and returns what it says.
§54: a second portfolio state would eventually disagree with the first, and
there would be no way to tell which was right.

---

## 3. The unit is a type

```python
class Unit(StrEnum):
    points = "points"      # NOT poolable across symbols
    currency = "currency"  # NOT poolable across trades sized differently
    r = "r"                # poolable. The figure to read.
    percent = "percent"
```

`Series.concat` raises `UnitMismatch` rather than adding two series in different
units. This is the metals-points lesson written as a type: `CLAUDE.md` records
median H1 ATR at 160 points in silver against 9,386 in palladium, and pooling
those produced a +4,236-point "result" that was arithmetic rather than a
finding. The same error wearing a lot size is why net currency cannot be pooled
across trades whose stops differ in distance.

**Every summary is computed twice**, once in currency and once in R, each
labelled, with `which_to_read` saying which pools. A single figure would leave
the reader to guess.

**A trade without an `r_multiple` is EXCLUDED from the R series, not given one.**
Deriving R from the realised loss would make every loser exactly −1R by
construction, which is a distribution rather than a measurement.

---

## 4. INSUFFICIENT_DATA is a value, not an absence

Sections 9 and 25. A string sentinel rather than `null`, so it survives JSON and
a reader cannot mistake it for zero or for a field the API forgot.

| Situation | Answer |
|---|---|
| No trades | `win_rate` is `INSUFFICIENT_DATA`, not 0 |
| Fewer than 20 trades | Sharpe and Sortino are `INSUFFICIENT_DATA` |
| No losing trades | `profit_factor` is `INSUFFICIENT_DATA`, **not infinity** |
| Zero variance | Sharpe and t are `INSUFFICIENT_DATA`, not a division |
| One trade | deviation and t are `INSUFFICIENT_DATA` |
| Fewer than 5 trades | median and percentiles are `INSUFFICIENT_DATA` |
| No drawdown | `recovery_factor` is `INSUFFICIENT_DATA`, not a large number |
| No order carries both timestamps | latency is absent, with the count that was measurable |

A profit factor of infinity from a run of winners reads as a strong edge from a
sample that simply never fell — which is exactly how this repository's own
93%-win-rate live regime looked decisive while being arithmetic.

---

## 5. Metric definitions

Every one is a named function with the definition in its docstring, so §5's
*"do not silently change metric definitions between screens"* has one place to
be true.

```
win_rate        winning / total closed. A break-even trade counts in the
                denominator and in neither numerator.
profit_factor   gross profit / abs(gross loss)
expectancy      the average trade result
payoff_ratio    average win / abs(average loss)
sharpe          mean / standard deviation, PER TRADE, not annualised
sortino         mean / downside deviation, divided by the DOWNSIDE count
max_drawdown    peak-to-subsequent-trough over time-ordered points
recovery_factor net profit / maximum drawdown
```

**Annualisation assumption: there is none, stated.** Annualising needs a
trades-per-year figure a fixed window does not supply, and inventing one is how a
Sharpe of 0.3 becomes a Sharpe of 2. The names carry the caveat.

**Risk-free rate: zero, stated.** These are per-trade results over no position,
and financing belongs in the trade's own costs, where `swap.py` already charges
it.

**The t-statistic is never returned without the trade count beside it.**
`CLAUDE.md` is unambiguous: *"Do not quote a live t-stat without saying how many
trades it rests on."* Below 30 trades the summary also carries an explicit
warning naming the 9.33 that meant nothing.

---

## 6. Two equity curves, never merged

Section 7 says not to interpret deposits and withdrawals as trading profit, and
asks that they be handled *if these exist*. **They do not exist**: this platform
has no cash-movement table and no column records a transfer.

So the honest answer is two curves:

| Curve | Built from | Can contain a deposit? |
|---|---|---|
| **realized** | cumulative net P&L from `trades` | **No** — every point is the sum of trade results |
| **account** | equity from `portfolio_snapshots` | **Yes**, and it says so |

Performance is read from the realized curve. The account curve carries a note
saying a deposit and a profit look identical in it.

`equity.combine(a, b)` exists only to raise `EnvironmentMismatch`. A function
that says no, rather than an absent one, because the absence would be filled by
somebody writing `a.points + b.points`.

---

## 7. Drawdown

A period opens when the curve falls below a peak and closes when it regains it.
Every case §8 names is covered by a test: no points, one point, monotonically
rising, monotonically falling, and several separate episodes.

**The final period is left OPEN when the curve has not recovered.** Reporting it
as recovered at the last point would say the account came back when it has not.

**`depth_pct` of a non-positive peak is `INSUFFICIENT_DATA`.** The realized curve
legitimately starts at zero and can pass through negative territory, and a
percentage of a non-positive peak is not a percentage.

---

## 8. Time and timezone

Section 43. The policy is served at `GET /v1/analytics`:

```
database       naive datetimes that mean UTC
api            ISO 8601, naive, meaning UTC
trading_day    app.risk.state.day_start — L17's boundary, REUSED
display        converted at the presentation boundary only
session        NOT MODELLED
```

**The trading day is L17's, crossed from naive to aware and back once.** A third
definition would reintroduce the bug `CLAUDE.md` records: a naive
`.replace(hour=0)` starts the day at 18:30 the previous evening on a UTC+5:30
machine and is correct on a UTC one. §43's own example — *23:59 UTC must not
become the next trading day* — has its own test.

**Sessions are not modelled**, and that is a refusal rather than a gap: the
broker's session boundaries are a venue fact this platform does not record, and
bucketing by an assumed one would label trades with a session they were not in.

**Every window is half-open `[start, end)`.** A closed range double-counts a
trade closing exactly on a boundary when two adjacent windows are summed, and
§41 asks that analytics agree with itself.

**Buckets are computed in Python from UTC**, not with `date_trunc`, whose
behaviour differs between PostgreSQL and SQLite — a boundary that moved with the
engine would make the tests agree with a production they do not match.

---

## 9. Counting: one trade row is one trade

Section 41. Every aggregate counts rows of `trades` and joins only through 1:1
foreign keys, so a trade with three fills and two partial closes is counted once.
L31 made that true by writing one row per position episode; analytics must not
undo it with a fan-out join.

The AI dimensions need `ai_decisions`, and that join goes through
`trades.ai_decision_id` — a nullable FK to a single row. It cannot fan out.

**Breakdowns group in Python over the rows the scope already selected**, rather
than with a second set of SQL aggregates. Two reasons: the metric definitions
stay in one module, and the grouped totals cannot disagree with the ungrouped
summary because they are the same rows. A test asserts they sum.

There is an end-to-end test that `/v1/analytics/summary` and `/v1/trades` report
the same count for the same account.

---

## 10. Comparison without causation

Section 14 and §24. `/v1/analytics/compare` returns both blocks, both sample
sizes, `smallest_sample`, `comparable`, and a `language` field that says:

> observational. This is a difference between two SELECTED sets, not a measured
> effect: nothing here was randomised, so whatever separates them may be what the
> filter selected for rather than what it did. Say "this set had a higher win
> rate in this sample", never "X improved performance".

Below 30 trades in either set the response also carries a warning. That is not
timidity: this repository's own AI-filter caveat records that a filter which
removes trades will improve total P&L on many of its datasets *for that reason
alone*, because frequency is the one lever with a proven sign and it points down.

---

## 11. Execution analytics

Section 16. Fill ratio, rejection ratio, partial fills, latency and slippage from
the OMS.

**Latency is measured only between timestamps that both exist.** An order missing
one is excluded and `measured_from` reports how many of how many were measurable
— §16 says not to invent latency, and an average over the orders that happened to
carry both stamps, presented as the average, is an invention.

**Slippage stays in POINTS and is never summed into the account-currency cost
total.** It is a per-fill figure and the costs are per-trade in currency; adding
them would be a unit error. The note points at the live trade `CLAUDE.md` records
— filled 279 points from its quote, invisible for three days because the log kept
the requested price.

---

## 12. API

```
GET /v1/analytics                  the contract: definitions, floors, policy
GET /v1/analytics/summary          §29's block, in both units
GET /v1/analytics/equity           both curves
GET /v1/analytics/drawdown         periods, current, maximum, recovery
GET /v1/analytics/breakdown        by strategy | symbol | bot | model | …
GET /v1/analytics/time-breakdown   hour | day | week | month | quarter | year
GET /v1/analytics/execution        OMS quality
GET /v1/analytics/exposure         delegated to the portfolio engine
GET /v1/analytics/positions        counted here, valued there
GET /v1/analytics/compare          two scopes, both sample sizes
GET /v1/analytics/backtest/{id}    read, never recomputed
```

Every verb is GET, behind `Permission.view_analytics`. Account ownership is
scoped in the query; a missing account and somebody else's give the same 404.

**An unknown filter value is a 422, never ignored.** A filter that silently stops
applying returns the wrong set as though it were the right one — an unknown
symbol, an unknown dimension, an unknown bucket, an unknown period and a reversed
window are all refused.

---

## 13. Frontend

`/analytics` — summary, equity, drawdown, breakdown and execution quality.

* **`N/A` for `INSUFFICIENT_DATA`, `0.00` for a measured zero.** A test asserts
  both: showing an uncomputable Sharpe as 0.00 and showing zero fees as a dash
  are the same error in opposite directions.
* **`NO COMPLETED TRADES` rather than sample figures.** §48.
* **No charting library.** The equity curve is an inline SVG polyline over the
  points the API returned, so there is no library interpolation that could invent
  a value between two readings.
* **R beside net currency**, each labelled, with the note saying which pools.
* **The environments the rows spanned are badges**, so a mixed set reads as mixed.
* **A low-sample group is amber** in the breakdown table.
* **A P&L that does not reconcile is shown in red**, not hidden.

---

## 14. Safety

Parsed with `ast` across `app/analytics/*` and the router:

* No reference to any order-placing verb.
* No import of `app.oms`, `app.orders`, `app.sizing`, `app.execution`,
  `app.brokers`, `app.risk.engine`, `app.risk.service`, any position
  manager/executor/reconciler, or `MetaTrader5`.
* No reference to `LIVE_TRADING`, `live_trading`, `LIVE_GATES`,
  `live_execution_allowed`; no import of `app.core.settings`.
* **No write verb at all** — no `add`, `commit`, `delete`, `flush`, `drop_all`,
  `truncate` or `merge`. Analytics reads; a write would make it an owner.
* No reference to `password`, `api_key`, `secret`, `credential` or `login`.

`TRADING_MODE=paper` and `LIVE_TRADING=false` are unchanged.

---

## 15. What L32 did not build, and why

**No caching.** §27 permits it *where appropriate* and §40 says not to optimise
prematurely without measuring. The largest table here holds 252 rows and the
summary computes in under 10ms; a cache would add an invalidation path that could
serve a stale equity figure as current — which §27 itself warns against — for no
measured gain. `calculation_ms` is on every summary so the decision can be
revisited with evidence.

**No materialised view and no `analytics_daily` table.** Same reason, plus §59: a
reconciliation that changes a trade must eventually be reflected, and a
precomputed row is one more place that has to notice.

**No MAE/MFE.** §20 of L31 already recorded it: intratrade price data does not
exist here, and `market_bars` covers one instrument.

**No realtime push.** §37 asks analytics to subscribe to existing events rather
than build a system. The events exist — `TRADE_RECORDED`, `PORTFOLIO_UPDATED` —
and the honest position is that a dashboard recomputing on every trade close is a
cache-invalidation design, which is the thing above that has no measured need
yet. The frontend refetches on filter change.

**No Excel or PDF export.** §38 says to extend what exists; what exists is L31's
CSV, and it already covers the trade set analytics reads.

**Nothing has run on real data.** `market_bars` covers one instrument, no broker adapter is
connected, and the 252 trades in the journal are imported from
`data/track_record.jsonl`. Every metric here is verified against seeded fixtures.
**No figure this level produces describes a real edge**, and the project's
measured position — nothing clearing its permutation null out of sample across
five universes — is unchanged.
