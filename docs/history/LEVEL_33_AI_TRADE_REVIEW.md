# AI trade review and post-trade intelligence (L33)

Built 2026-09-04. What happened on one completed trade, assessed from what was
recorded — and kept honest about which half of the record it is allowed to use.

---

## 1. The audit's finding: there is no LLM in this platform

Section 2 asks what exists before anything is built. The search found **no
`openai`, no `anthropic`, no API key setting, no prompt template and no provider
abstraction** anywhere in the repository. `app/ai/` holds logistic, regime and
anomaly models written in stdlib; nothing in it talks to a language model.

That is the finding L24 recorded about models — *"No AI model existed anywhere
before it"* — and it has the same consequence: nothing is preserved, nothing is
replaced, and the honest thing to build is **the seat rather than an occupant**.

| Component | Verdict |
|---|---|
| `trades` + attribution links (L31) | **KEEP**, read |
| `positions`, `orders`, `executions`, `risk_events`, `ai_decisions` | **KEEP**, read |
| `strategy_versions.config` (L12) | **KEEP**, read at the trade's own version |
| L32 analytics | **KEEP**, consumed for baselines and patterns |
| L29 model monitoring | **KEEP**, untouched — §51 keeps the two apart |
| L28 model registry | **KEEP**, identity only; its verbs are unreachable |
| `journal_entries.ai_review` (a `# L33` column since L05) | **KEEP, unused** — see §10 |
| `app/review/*`, `app/api/v1/reviews.py`, `trade_reviews` | **ADD** |

---

## 2. The seat, not an occupant

`ReviewProvider` is a Protocol with one method. The default implementation,
`DeterministicProvider`, is **not a language model**: it writes the summary from
the sections the deterministic assessors already produced.

This is L27's AI_DISABLED decision again. There, the seat is `None` rather than a
filter that accepts everything, because a permissive stub and a real component
that agrees look identical in the record. Here the equivalent trap would be a
provider returning plausible prose; instead the default returns prose that is
*derived*, says so, and is labelled `deterministic` in `review_model`.

**Nothing leaves this process.** When a real provider is added, the payload it
receives is `ReviewInput.as_dict()` — prices, quantities, identifiers and
recorded decisions. A test asserts no credential name appears in it, and a second
asserts neither context dataclass has a field that could hold one.

---

## 3. The wall: decision context and outcome context

Sections 18, 19 and 63, and the module the level turns on.

```
DecisionContext    everything known AT OR BEFORE the entry
OutcomeContext     everything that happened after
```

**Four of the five assessors take a `DecisionContext` and nothing else.**

```python
def entry_quality(decision: DecisionContext) -> Section: ...
def strategy_alignment(decision: DecisionContext) -> tuple[Section, Compliance]: ...
def risk_quality(decision: DecisionContext) -> Section: ...
def execution_quality(outcome: OutcomeContext) -> Section: ...
def exit_quality(decision: DecisionContext, outcome: OutcomeContext) -> Section: ...
```

§63 asks for a test proving the entry assessment does not gain access to future
information when post-entry data changes. **That test passes because the function
is not given the data** — `exit_price`, `net_profit`, `r_multiple`, `mae` and
`mfe` are absent from `DecisionContext`, and a second test asserts that absence
directly. A field added there later is a claim that the platform knew that thing
before the entry, and it should be uncomfortable to make.

`exit_quality` is the deliberate exception. An exit is itself a decision, so it
is rated against the levels **planned before entry** and never against how far
price eventually travelled.

The reverse direction is allowed and is the point: §19 permits post-trade
information to explain the OUTCOME. What it may not do is conclude "therefore the
entry was poor", and the split makes that sentence unwriteable in the section
that rates the entry.

---

## 4. Deterministic first, narrative second

Section 36: *do not ask an LLM to calculate values that can be deterministically
calculated.* So:

| Deterministic | Generated |
|---|---|
| every rating | the summary |
| every OBSERVED statement | key factors |
| strategy compliance, where machine-checkable | lessons |
| the confidence figure | follow-up questions |
| the outcome (WIN/LOSS/BREAKEVEN, from `net_profit`) | |

A provider returns a `Narration` — a summary and three lists — and **there is
nowhere on it to put a rating, a price, a P&L or a model version**. The
hallucinations §37 lists cannot enter the stored record by the front door.

---

## 5. Three kinds of claim

Section 41, enforced by the type: `Evidence` cannot be constructed without a
kind, and `observed()` requires a source.

| Kind | Means | Source |
|---|---|---|
| `OBSERVED` | a fact read from a recorded row | **required** |
| `INTERPRETED` | a reading of those facts | optional |
| `HYPOTHESIS` | a possible explanation, offered as one | none, and that is informative |

Validation rejects an OBSERVED statement with no source: an observation nobody
can check is exactly what §5 forbids. The dashboard renders the three
differently, with a hypothesis visually quieter than an observation.

---

## 6. UNKNOWN is a rating, and it is not POOR

Section 12: *do not label a trade non-compliant when the required strategy data
was not recorded.* Generalised across every section:

* no strategy version linked → `UNKNOWN`, **not** `NON_COMPLIANT`
* no risk or sizing record → `UNKNOWN`, **not** `POOR`
* no order or fill rows → `UNKNOWN`, **not** `POOR`

Every UNKNOWN section carries `unavailable_reason` naming the field it needed,
and validation rejects one that does not. This is L26's BLOCKED-is-not-FAIL and
L29's INSUFFICIENT_DATA-is-not-HEALTHY, one level along: *"we could not tell"* and
*"it was bad"* are different claims.

---

## 7. Nothing is estimated

Section 5, and the cases where it bites:

* **MAE and MFE are `null`.** They need an intratrade price series this
  deployment does not store. §4 lists them; §5 forbids estimating them.
* **Market context is one field.** The platform records a regime label on the AI
  decision and nothing else — no spread, no volatility, no session. The block
  lists those as `not_recorded` rather than filling them in.
* **Latency is measured only between timestamps that both exist.**
* **A trade with no recorded planned risk gets no R-based judgement.**

---

## 8. Confidence is derived, not self-reported

Section 22. Two measurable terms:

```
confidence = (completeness + section_coverage) / 2
```

`completeness` is the fraction of twenty tracked fields that were recorded;
`section_coverage` is the fraction of the five sections that could be rated at
all. **There is no term for how sure the narrative sounded**, because §22 says
not to treat a generator's confidence as statistical certainty.

A fluent review of a trade with no strategy, no risk and no sizing record is a
low-confidence review, and this is what makes that true.

---

## 9. Hallucination control

Sections 37 and 38, in two passes.

**Structural.** Enums in range, confidence a probability, every section present,
every UNKNOWN explained, every OBSERVED sourced, and the review naming the model
that produced it. A CHECK constraint enforces the last one for COMPLETED rows.

**Referential.** The trade id, the environment, the symbol, the prediction model
version, and **every decimal figure in the narrative** must appear in the facts
the review was built from. A provider writing "closed at 9.8765" when the exit
was 1.1200 is rejected; so is an invented symbol and an invented model version.

A failure stores the review as `FAILED` with the reasons attached. §38: never
stored as trusted data, and never silently repaired.

---

## 10. Storage

`trade_reviews`, migration `0023`, verified on real PostgreSQL including the
round-trip downgrade.

**`journal_entries.ai_review` is deliberately left alone.** It has carried a
`# L33` comment since L05, and it is a USER's note column — L31 §39 already
separated user notes from system-generated facts, and putting a machine review
there would erase that separation the moment somebody queried it.

Four constraints carry the rules into the schema:

```sql
status IN ('PENDING','PROCESSING','COMPLETED','FAILED','RETRYING','CANCELLED')
review_version >= 1
confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
status <> 'COMPLETED' OR (review_model IS NOT NULL AND review_model_version IS NOT NULL)
UNIQUE (trade_id, review_version)
```

The enum in `app.review.contract` asserts against the schema's tuple **on
import**, so a state the code can produce and the schema rejects cannot exist.

---

## 11. Idempotency and versioning

Section 11: `review_for` returns the existing row rather than writing a second,
and the unique constraint makes the race a database error. A redelivered
TRADE_CLOSED event produces the same review.

Section 33 and §44: a regeneration writes the **next** version and leaves every
earlier one readable. `versions_for()` returns them all, oldest first, and the
API says so in the response.

---

## 12. Asynchronous, and never blocking

Sections 8, 9 and 64. Generation is a **sweep** over completed trades with no
review — the shape L31 chose for the same reason: several paths close a trade,
and a hook missed at one produces a review that silently never exists.

**A provider failure marks the review FAILED and touches nothing else.** A test
records the trade's status, P&L and exit price before a deliberately broken
provider runs, and asserts all three are unchanged afterwards.

Two ceilings, because §47 asks that reviews not be generated without one:
`MAX_PER_SWEEP = 50` and `MAX_ATTEMPTS = 3`.

---

## 13. Patterns across trades

Sections 27 to 30. `PatternFinder` **computes no metric** — it calls
`AnalyticsService.by()` and reads shapes in the answer.

* Every observation carries `trades`, and below L32's own
  `MIN_TRADES_FOR_COMPARISON` it is labelled *"observed in 4 trades;
  insufficient sample for a reliable pattern"*.
* The floor is imported, not re-picked: a group that is "not enough to compare"
  on the analytics screen must not be "a recurring pattern" here.
* Grouping is by recorded attributes. §30 permits ML clustering only where the
  project already supports it; it does not.
* The response carries a `caution` saying these groups were not chosen in
  advance, and that looking along six dimensions and reporting the worst is a
  search — which this repository's own record says does not survive out of
  sample.

---

## 14. API

```
GET  /v1/trade-reviews/contract        the schema, the provider, the rules
GET  /v1/trades/{id}/review            the latest version
POST /v1/trades/{id}/review            generate (idempotent)
POST /v1/trade-reviews/{id}/regenerate a NEW version; the old one stays
GET  /v1/trade-reviews                 filterable, paginated
GET  /v1/trade-reviews/patterns        aggregate shapes, with sample sizes
GET  /v1/trade-reviews/summary         counts by status and outcome
POST /v1/trade-reviews/sweep           bounded batch generation
```

Reading is gated at `view_journal`; regenerating and sweeping at
`manage_ai_models`, because both spend provider budget. Access follows the trade:
a review is readable exactly by whoever may read its trade, checked in the query,
and somebody else's trade gives the same 404 as a missing one.

---

## 15. Frontend

The review renders inside the existing trade detail panel — §40 asks to extend
the trade page rather than build a second one.

* **Each statement carries a badge for its kind**, and a hypothesis is quieter.
* **UNKNOWN is neutral**, with the missing data named.
* **Review model and prediction model are separate rows** in the metadata.
* **Version, schema, prompt, confidence and status** are all shown — §43.
* **A failed review says the trade is unaffected.**
* **Confidence is labelled "from data completeness"**, so nobody reads it as the
  model's certainty.

---

## 16. Safety

Parsed with `ast` across `app/review/*` and the router:

* No order-placing verb anywhere.
* No import of `app.oms`, `app.orders`, `app.sizing`, `app.execution`,
  `app.brokers`, `app.risk.engine`, `app.risk.service`, any position
  manager/executor/reconciler, or `MetaTrader5`.
* **No import of `app.ai.registry_service`**, so `promote`, `rollback` and
  `retire` are unreachable — §1's "do not automatically replace models" is a
  property of the import graph.
* No `LIVE_TRADING`, `live_trading`, `LIVE_GATES`, `live_execution_allowed`; no
  `app.core.settings`.
* No `delete`, `drop_all`, `drop_table`, `truncate`.
* No `password`, `api_key`, `secret`, `credential`, `auth_token`.

`TRADING_MODE=paper` and `LIVE_TRADING=false` are unchanged.

**One test was wrong and was narrowed rather than suppressed.** The
forbidden-name list first included `rollback` and `register`, which caught
`db.rollback()` and `WorkerRegistry.register`. A rule that flags unrelated code
is a rule people learn to ignore, so the name check now lists the registry's
specific verbs and the import check does the real work.

---

## 17. Handoffs

**To L34 (notifications).** Three account-scoped event types are in L07's
catalogue: `TRADE_REVIEW_COMPLETED`, `TRADE_REVIEW_FAILED`,
`TRADE_PATTERN_DETECTED`. §49: the interface, not the notification system.

**From L32 (analytics).** Baselines on every review and every pattern
observation, consumed rather than recomputed.

**From L29 (monitoring).** Kept separate, per §51. A review answers *"what
happened on this trade?"*; monitoring answers *"how is the model behaving over
time?"*. The AI context block says so explicitly where a single disagreement
might otherwise read as a verdict on the model.

---

## 18. What L33 did not build, and why

**No LLM integration.** No provider exists, no key is configured, and adding one
would mean choosing a vendor, a cost model and a data-egress policy that nobody
has asked for. The seat is defined and the default fills it deterministically.

**No background worker registration.** The sweep exists and is exposed as a
route. Registering it to run on a timer is an operator decision, and this
platform's convention since L20 is that workers are registered and **not
started** — beginning to act on a schedule is an operator action.

**No MAE/MFE.** Intratrade price data does not exist here. §20 of L31 recorded
the same blocker.

**No trade chart.** §38 of the brief asks for one *if market data is available*.
`market_bars` covers one instrument, and §48 forbids fabricating candles.

**No clustering model.** §30 permits one only where the project already supports
it.

**Nothing has run on a real review.** The 252 trades in the journal are imported
rows with no strategy, risk, sizing or AI attribution — so a review of one is
mostly `UNKNOWN`, and **that is the correct output**, not a gap. The end-to-end
test constructs a fully attributed trade to prove the chain works when the data
exists.
