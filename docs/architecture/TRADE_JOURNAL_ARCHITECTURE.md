# Trade journal and trade lifecycle history (L31)

Built 2026-09-04. What happened, why it happened, and what the system knew at
the time.

---

## 1. Order, fill, position, trade

Section 4, and it is the distinction the whole level rests on:

| | What it is | Where it lives |
|---|---|---|
| **Order** | an instruction to buy or sell | `orders` (L19) |
| **Fill** | what the venue actually executed | `executions` (L19) |
| **Position** | what is currently held | `positions` (L21) |
| **Trade** | the completed episode, entry through exit | `trades` (L05, extended here) |

**One journal row per POSITION EPISODE.** A position that filled in two parts
and closed in three is ONE trade, not two and not three — and the six events are
its timeline. Counting execution rows as trades is the error §41 names, and a
partial unique index on `position_id` makes a second row for one episode a
database error rather than a convention.

---

## 2. What the audit found

**The record already existed, with 252 rows in it.** L31 built no second history.

| Component | Verdict |
|---|---|
| `trades` (L05) — the completed round trip, with bracket and R | **KEEP + MODIFY** — 13 nullable columns added |
| `positions` + `position_events` (L21) — partial-close accounting | **KEEP**, read |
| `orders` + `order_events` + `executions` (L19) | **KEEP**, read |
| `signals` + `webhook_events` (L09) | **KEEP**, read |
| `risk_events` (L17) — decision, reason, snapshot, config version | **KEEP**, read |
| `ai_decisions` (L27) — model, version, probability, regime | **KEEP**, read |
| `orders.sizing` (L18) | **KEEP**, read |
| `journal_entries`, `trade_tags` (L05) — user notes | **KEEP**, untouched |
| `app.positions.policies.ExitReason` (L21) | **KEEP** — the exit vocabulary, reused |
| `/v1/trades`, `/v1/trades/{id}`, `/v1/executions` | **KEEP + MODIFY** — filters and four sub-routes |
| Frontend `PlannedPage` at `/journal` | **REPLACE** |
| `journalService` returning `unavailable` | **REPLACE** |
| `app/journal/` — lifecycle, closes, timeline, quality, context, service | **ADD** |
| `app/services/journal.py` — statistics and export | **ADD** |

### The three things that were actually missing

**The live path wrote no trade at all.** `PositionManager._book` closes a
position and books `realized_pnl` on the row, and nothing ever created a `trades`
row from it. Only the paper service and the JSONL importer wrote one.

**The paper path wrote one with no attribution.** No `position_id`, no
`strategy_version_id`, no account, no exit reason — so a paper trade could not be
linked back to the position, the order, the signal, the risk decision or the AI
decision that produced it.

**Nothing assembled the context.** Every fact §8 to §12 asks for was already
recorded by the system that produced it. What did not exist was the thing that
read them together.

---

## 3. Automatic generation: a sweep, not a hook

Section 40. There are four places a position can end — the position manager, the
reconciler, the paper service and the close route — and **a hook missed at one
of them produces a trade that silently never exists.**

So `TradeJournalService.record_pending()` asks the database which episodes have
ended and which have no journal row:

```sql
SELECT positions.* FROM positions
LEFT JOIN trades ON trades.position_id = positions.id
WHERE positions.status IN ('closed','unknown') AND trades.id IS NULL
```

It cannot miss a close site, and it is safe to run repeatedly because
`record_close` is idempotent.

The paper engine is the one exception and it is written directly, because the
simulator books its own fills and writes no `position_events` — the sweep would
find a closed position with no confirmed close and correctly refuse to invent
one.

---

## 4. Idempotency

Section 27, enforced in three places rather than one:

1. `record_close` looks for an existing row on the same `position_id` and
   returns it **untouched**. A re-delivered fill arriving after a correction
   must not silently undo the correction.
2. A **partial unique index** `(position_id) WHERE position_id IS NOT NULL`
   makes a concurrent second write a database error.
3. An `IntegrityError` from that index is caught and resolved by re-reading —
   the other writer won, and its row is the record.

**NULLs are distinct in a unique index**, which migration 0020 learned the hard
way. Here that is the WANTED behaviour: the 252 imported rows carry no
`position_id`, they are not position episodes, and a constraint that collapsed
them into one would destroy the record.

The timeline gets idempotency for free by being derived — see §6.

---

## 5. Partial fills and partial closes

Sections 13 and 14, and this is the arithmetic the level lives or dies on.

**Partial fills — the entry is read, not recomputed.** `Position.entry_price` is
already the volume-weighted average across fills; L21 maintains it. Recomputing
it from `executions` would be a second derivation of one fact.

**Partial closes — the exit is weighted here**, because nothing else holds it:

```
exit = Σ(fill_i × qty_i) / Σ(qty_i)
```

0.40 at 1.2000 then 0.60 at 1.1500 gives 1.1700 on a volume of 1.00. One trade.

**Realized P&L is summed from the closes, never recomputed from the average.**
The subtle way to double-count is to book `(weighted_exit − weighted_entry) ×
quantity` *as well as* the per-close figures. They are close but not equal
whenever the entry itself was weighted, and only one is what the account
received. `Position.realized_pnl` — the running total L21 booked at each close —
is authoritative.

**The exit reason is the LAST close's.** A position scaled out at a target and
then stopped out on the remainder exited at the stop, and reporting the first
reason would describe a trade that did not happen.

**A close with no fill price is skipped, not defaulted.** L21 records one when
the venue confirmed without a price and parks the position `unknown`; a close
valued at a price nobody reported would move the weighted average.

---

## 6. The timeline is derived, never stored

Section 26, and the decision is §3's rule applied literally: *do not create
duplicate trade-history systems.*

Every event is already recorded, with a timestamp, by the system that produced
it — `webhook_events`, `signals`, `risk_events`, `ai_decisions`, `orders`,
`order_events`, `executions`, `position_events`. A `trade_events` table would be
a second copy of all of it: it could be written wrongly, it could fall behind,
and when it disagreed there would be no way to tell which was right.

So the timeline is assembled at read time and **cannot disagree with its
sources, because it has none of its own.**

Two details:

* **Ties break in causal order.** L19 acknowledges and fills within one second,
  and sorting those arbitrarily would render a fill before its own submission.
* **`gaps` names what is missing.** A blank is ambiguous — a timeline with no AI
  event could mean AI_DISABLED or a link never written — so each absence is
  named, and the AI one says explicitly that it is *not* evidence the AI was
  bypassed.

---

## 7. Context: read from then, never recomputed now

Sections 8 to 12 and §23. The question is *"what did the system know when the
trade was opened?"*, and every block answers it from the row written at the time:

| Block | Source | Rule |
|---|---|---|
| strategy | `strategy_versions`, `signals`, `webhook_events` | the webhook PAYLOAD is never served — §45 |
| ai | `ai_decisions` (L27) | an **exact** model version, never "latest" — §9 |
| risk | `risk_events.snapshot`, `configuration_version` | never recalculated with today's values — §10 |
| sizing | `orders.sizing` (L18) | read, never re-run — §11 |
| execution | `orders`, `executions` | the venue's figures beat what was requested — §12 |
| costs | the trade row | each cost separate; none folded into another — §18 |

**An absent block says which kind of absent.** "No AI decision is linked" for an
AI_DISABLED strategy is the correct and expected shape, and the block says so —
because the reader's natural inference from a blank is that something was
bypassed.

---

## 8. Never finalise what the venue has not confirmed

Section 50, in the states themselves:

```
open · partially_closed · closed · reconciliation_required · unknown
```

`cancelled` and `rejected` are deliberately absent. A journal row is written when
a POSITION closes, and a cancelled order never opened one — `orders.status`
already carries both words, and recording a rejection as a trade is the
confusion §25 warns against.

* A position parked `unknown` produces a trade with status `unknown`, never
  promoted to `closed`.
* `mark_reconciliation_required` changes **only** `status` and `data_quality` —
  §32 makes the prices, quantities and P&L historical facts, and the
  disagreement is about whether the episode ended, not about what was recorded.
* **No confirmed close means no row.** No exit price and no booked P&L produces a
  refusal with the reason, not a row with substituted values.

---

## 9. Data quality: detect, flag, never correct

Section 44 and §32. Nine checks, and none of them writes a price, a quantity or a
timestamp:

`missing_entry` · `missing_exit` · `missing_timestamp` · `negative_duration` ·
`invalid_quantity` · `pnl_does_not_reconcile` · `missing_broker_id` ·
`reconciliation_required` · `missing_exit_reason` · `no_confirmed_close`

A journal that quietly repaired an impossible timestamp would leave a record that
looks clean and is wrong, and the repair would be invisible — the same failure
`CLAUDE.md` records about the order log that stored the requested price as the
entry.

`pnl_does_not_reconcile` is §18's double-counting made detectable: `net` must
equal `gross − commission − swap − fees` to four decimals.

`data_quality` is `None` when the checks have not run and `{"checked": true,
"findings": []}` when they ran and found nothing. Those are different facts.

---

## 10. Schema: 13 columns, no new table

Migration `0022_trade_journal`, verified on real PostgreSQL including the
round-trip downgrade.

```
status                  open | partially_closed | closed | reconciliation_required | unknown
broker_account_id       §30. Two columns, never one nullable account_id.
paper_account_id        Carried on the trade because a position row can be deleted.
order_id                §6, §12
signal_id               §8
ai_decision_id          §9  — an exact version, by reference
risk_event_id           §10 — the snapshot written then
bot_id                  §6
exit_reason             §17 — L21's vocabulary, beside the broker's close_reason
requested_entry_price   §12, §16 — the 279-point live trade CLAUDE.md records
entry_slippage_points
fees                    §18 — distinct from commission
currency                §19
data_quality            §44
```

Every column is nullable; `status` takes a server default of `closed`, which is
true of all 252 imported rows. Nothing is backfilled with a value nobody
measured.

**The defect this migration found:** `op.drop_constraint("ck_trades_status", ...)`
produced `ck_trades_ck_trades_status`. The naming convention interpolates the
prefix, so a pre-prefixed name is doubled. This is the **fourth** time in this
repository, and the first on a DROP rather than a CREATE. The rule, without
exception: every constraint kind, both directions, bare name, always.

---

## 11. API

```
GET /v1/trades                          filters, search, pagination
GET /v1/trades/statistics               counts and totals; no Sharpe
GET /v1/trades/export                   CSV, capped
GET /v1/trades/{id}
GET /v1/trades/{id}/timeline            derived
GET /v1/trades/{id}/decisions           the context, as written
GET /v1/trades/{id}/executions          each close, itemised
GET /v1/trades/{id}/analysis            recorded vs recomputed quality
```

**The static paths are registered before `/trades/{trade_id}`.** Starlette
matches in registration order, and the parameterised route would otherwise
capture `statistics` and `export` as trade ids and answer 404. A test asserts
both still resolve — it was caught by that test, not by reading the code.

Filters (§34): mode, side, symbol, result, exit_reason, status, account_id,
strategy_version_id, bot_id, model_key, model_version, ai_mode, date range.
Search (§35): exact match across trade, position, order, signal and broker ids —
exact rather than LIKE, because these are opaque identifiers and a prefix search
over them is meaningless.

**No mode filter is applied by default**, and that is deliberate. §34 says not to
mix paper and live BY DEFAULT; every row carries its own `mode` and the caller
sees which. Silently defaulting to paper would hide live trades from somebody who
asked for all of them, which is the worse failure.

The AI filters JOIN `ai_decisions` rather than reading copied columns — a copy of
a model version is a copy that can drift from the decision it describes.

---

## 12. Statistics: descriptive, and the line is deliberate

`/v1/trades/statistics` counts, sums and averages. It computes **no Sharpe, no
Sortino, no expectancy curve and no significance**, and says so in
`not_computed`.

§42 assigns analytics to L32, and the line is not arbitrary: those are the
figures somebody quotes as a track record. `CLAUDE.md` records a live t of 9.33
that was arithmetic rather than evidence — a random entry with a 6:1 adverse
bracket wins about six times in seven by construction.

**`win_rate` is `null` on an empty set, never 0.** Nought wins from nought trades
is not a nought percent win rate; it is no measurement.

**R is the figure to pool.** Net currency cannot be pooled across trades sized by
different stop distances, and the response says so beside the number.

---

## 13. Realtime

Three account-scoped types in L07's catalogue: `TRADE_RECORDED`,
`TRADE_UPDATED`, `TRADE_RECONCILIATION_REQUIRED`.

Three, not the brief's six. `trade.opened` and `trade.partially_closed` are
POSITION events and already exist as `POSITION_OPENED` and `POSITION_UPDATED`;
publishing them again under a trade name would give the platform two
vocabularies for one fact, which is the duplication §41 warns against. What is
genuinely new is the journal ROW.

---

## 14. Frontend

`/journal` — a filterable table with a detail view.

* **NO TRADES, never a sample row.** §48 and §51.
* **R first, net beside it**, with the header saying which pools.
* **Every row carries its environment badge**; the filter is opt-in.
* **Data-quality findings render**, they are not hidden.
* **Each absent context block shows its own reason**, including the AI one that
  says it is not evidence of a bypass.
* **System facts are read-only.** §39: notes are `journal_entries`, a separate
  table, and no control here can overwrite a recorded fact.

---

## 15. Safety

Parsed with `ast`, never grepped, across `app/journal/*`, the router and the
query module:

* No reference to `place`, `place_order`, `submit_order`, `cancel_order`,
  `modify_order`, `close_position`, `modify_position`, `open_position`,
  `send_order`, `order_send`.
* No import of `app.oms`, `app.orders`, `app.sizing`, `app.execution`,
  `app.brokers`, `app.risk.engine`, `app.risk.service`, any position
  manager/executor/reconciler, or `MetaTrader5`.
* No reference to `LIVE_TRADING`, `live_trading`, `LIVE_GATES`,
  `live_execution_allowed`; no import of `app.core.settings`.
* No reference to `delete`, `drop_all`, `drop_table`, `truncate`.
* No reference to `password`, `api_key`, `secret`, `webhook_secret`,
  `credential`.

And a behavioural test: a webhook payload containing a secret reaches neither the
timeline nor the context.

`TRADING_MODE=paper` and `LIVE_TRADING=false` are unchanged.

---

## 16. Handoffs

**To L32 (analytics).** `trades` now carries environment, account, strategy
version, bot, AI decision, risk event, exit reason, costs and R on one row.
`/v1/trades` filters by every one of them.

**To L33 (AI trade review).** `/trades/{id}/decisions` returns the strategy, AI,
risk, sizing and execution context as it was written, and
`/trades/{id}/timeline` returns the chronology. That is the complete "what did
the system know" record §43 asks for.

---

## 17. What L31 did not build, and why

**No `trade_events` table.** The timeline is derived; see §6.

**No MAE/MFE.** It needs intratrade price data, and `market_bars` covers one instrument on
this deployment. §20 of the brief says not to approximate it from incomplete
data.

**No entry slippage figure yet.** The column exists and is written `None`: the
requested price is recorded and the actual entry is recorded, so the difference
is visible, but converting it to POINTS needs the symbol's point size and a
figure derived from an assumed one is the metals-points error again.

**No currency conversion.** §19 asks for the rate and timestamp when converting;
no FX rate source is wired, so the currency is recorded and nothing is converted.

**Nothing has run on a real venue.** No broker adapter is connected here, so
every live-path test runs against a fake. The 252 rows in `trades` are imported
from `data/track_record.jsonl` and carry none of the new attribution — which is
correct, and is why every added column is nullable.
