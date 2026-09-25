# LEVEL 18 — POSITION SIZING ENGINE

Completed 2026-09-03. Adaptive build mode: audit → plan → baseline → implement
→ integrate → test → verify → document.

The engine answers one question — *given the account, the entry, the stop, the
risk budget and the broker's contract terms, what is the largest quantity that
keeps the loss at the stop inside the budget?* — and it answers no other. The
Risk Engine decides whether the order may exist; sizing only measures.

---

## 1. What already existed

`app/sizing/calculator.py` was written before this level as 200 lines of
arithmetic with three modes, one consumer (`app/paper/engine.py`, since L16)
and **no tests of its own**. `tests/test_paper.py` proved the paper engine
*called* it; nothing proved it computed correctly.

The audit found the separation of concerns already correct, which is the most
important finding in it:

| Question | Answered by | Verdict |
|---|---|---|
| "Is this trade allowed?" | `app/risk/` — 26 checks, kill switches, latched locks | **KEEP**, untouched |
| "How much?" | `app/sizing/` | **KEEP + MODIFY** |
| "How is it executed?" | `app/paper/oms.py` | **KEEP**, untouched |
| "How do we reach the broker?" | `app/brokers/` | **KEEP**, untouched |
| Broker contract terms | `app/symbols/` `ContractSpec` | **KEEP**, reused |
| One flooring implementation | `app/symbols/precision.py` | **KEEP**, now shared |

**No sizing logic was embedded in the Risk Engine**, so brief §12's extraction
was not needed — `RiskEngine.approve` reads `proposal.volume` and vetoes; it
never computes one. No second Risk Engine, OMS, broker adapter or symbol
metadata system was created, and none existed to merge.

---

## 2. The two defects this level fixed

These are the reason the level was not a documentation exercise.

### 2.1 A size below the broker minimum was raised to the minimum

`_apply_broker_limits` did this:

```python
if volume < spec.minimum_volume:
    volume = spec.minimum_volume          # <- risks MORE than the budget
    if would_risk > budget:
        gap = "... risks {would_risk} against a {budget} budget ..."
```

The overshoot was recorded in a `gap` string, and `result.ok` was still true,
so a caller that did not read `gap` traded a position risking more than the
configured budget. A 1.00 budget on an instrument whose minimum lot risks 5.00
became a 5.00 trade.

This violated brief §9 ("0.05 → REJECT... Do not automatically increase"),
§10 ("Never silently exceed the user's configured risk") and safety invariant
§36.2. It also **contradicted the project's own code**:
`app.symbols.precision.normalize_quantity` already refused this exact case,
with a docstring giving the exact reasoning — "the minimum lot exceeding the
risk budget means the trade cannot be taken at this size, not that it should
be taken larger". Sizing disagreeing with the canonical normalizer *was* the
duplication.

**Now:** refused, with a message naming what the minimum lot would have cost
so an operator can tell a coarse instrument from a risk setting that is too
small. Pinned by `test_a_size_below_the_venue_minimum_is_refused_not_raised`.

The maximum is treated the *opposite* way, deliberately: it binds downward,
which can only reduce risk, so it is applied and reported as a warning. That
asymmetry is the whole of the rule.

### 2.2 Stop direction was never validated

The engine only ever received a `stop_distance`, and `abs()` makes a target
indistinguishable from a stop. A long order with its stop *above* the entry
sized normally and passed a plausible volume to the OMS.

This is not hypothetical in this repository. `CLAUDE.md` records position
10200315596 — an NZDUSD sell whose stop and target both ended up above the
entry after a 279-point fill discrepancy, so every branch was a loss and the
retcode still said DONE. Direction validation is the class of check that would
have caught that shape.

**Now:** `side`, `entry_price` and `stop_loss` are accepted and validated.
A long stop above its entry (or a short stop below) is **refused, not
corrected** — both available corrections, flipping the stop and flipping the
side, change what the caller asked for. Pinned by
`test_a_stop_on_the_wrong_side_is_refused`.

---

## 3. Decisions

### 3.1 Three modes, not seven

The brief names seven. This platform has three, and the other four are the
same three under different names:

| Brief | Here | Why |
|---|---|---|
| MODE 1 fixed quantity | `fixed_quantity` | — |
| MODE 2 fixed lot | `fixed_quantity` | On every instrument this platform trades, MT5 included, **the quantity IS the lot**. A second mode would be a second name for one calculation. |
| MODE 3 percentage of equity | `percent_equity` | — |
| MODE 4 monetary risk | `fixed_risk` | — |
| MODE 5 stop-loss based | both risk modes | Neither will size without a stop; there is no denominator otherwise. |
| MODE 6 ATR sizing | not a mode | It is stop-distance sizing over a stop the caller derived from ATR — which is what `PaperEngine._bracket` and the backtester already produce. A separate mode would put the bracket calculation in two places. |
| MODE 7 broker-constrained | applied to **every** result | A volume the venue will not accept is not a size, so it is not an option. |

The brief's names are accepted as aliases (`METHOD_ALIASES`) and resolved at
the edges, so a caller can say `fixed_lot` without the engine growing a fourth
branch. `GET /v1/position-sizing/modes` documents the mapping.

### 3.2 The engine is pure; the service counts

`calculator.py` has no clock, no randomness, no I/O and no model — so the same
request always produces the same result, and `test_the_engine_is_deterministic`
is a property rather than a coincidence. Everything non-deterministic
(counters, latency, logging) lives in `service.py`, which follows the shape
`app.risk.service` already uses: an in-process `status()` dict rather than a
second metrics stack. Adding a Prometheus registry for six counters would be
the duplicate monitoring infrastructure §35 warns against.

### 3.3 `calculate()` never raises

A malformed request comes back as a refusal carrying its reason. A caller in
the middle of a trading pass needs a decision it can record, not an exception
it has to classify.

### 3.4 No new event type

The L07 catalogue is a fixed 29-type vocabulary with a scope and an owning
level per type, and its counts are asserted by tests. Sizing outcomes ride in
the order payload (`orders.sizing`), the risk decision and the metrics, rather
than adding a 30th type. §28's list is offered as "potential events"; the
instruction that binds is "do not introduce a second event system", and this
does not.

### 3.5 Replay stays fixed-quantity, and says so

L18 wired the risk modes into the **backtester**, not into replay. The replay
engine carries one `quantity` on its portfolio and is proved trade-for-trade
identical to `simulate()`; making the size vary per position changes that
engine rather than configuring it, and that equivalence proof is worth more
than the feature. `POST /v1/replay/sessions` accepts the same body shape as a
backtest (a shared shape is asserted by a test) and **refuses** a risk mode
with that reason — accepting the field and ignoring it would report a replay
at a flat lot that the caller believes was risk-sized.

---

## 4. Changes, classified

### KEEP + MODIFY

**`backend/app/sizing/calculator.py`** — the engine. Kept its three modes, its
refusal doctrine and its float-floor guard verbatim. Added: direction
validation, entry/stop levels, NaN and infinity guards on every numeric input,
a `max_risk_amount` ceiling, the full result shape from brief §15
(`raw_quantity`, `stop_distance`, `broker_constraints`, `warnings`, `status`,
`as_dict()`), and volume rendered at the venue's own `volume_precision`.
Replaced: the minimum-volume upsize (§2.1) and the private `_round_to_step`.

**`backend/app/backtest/config.py`** — `fixed_risk` and `percent_equity` go
from "declared but refused" to wired. What is refused now is a risk mode with
nothing to size from.

**`backend/app/backtest/runner.py`** — `_enrich` sizes each trade through
`app.sizing.calculate`. `run()` takes an optional `ContractSpec`.

**`backend/app/paper/engine.py`** — passes `side`, `entry_price`, `stop_loss`
and `take_profit` alongside the distance, so direction validation runs in the
live pipeline. Three lines; the pipeline order is unchanged.

**`backend/app/api/v1/backtests.py`**, **`replay.py`** — sizing fields on the
request bodies; the assumptions endpoints restate what is now true.

**`frontend/src/app/risk/page.tsx`** — the L18 placeholder becomes the real
calculator.

### MERGE

**`value_per_price_unit`** moved from `app/paper/portfolio.py` to
`app/symbols/precision.py` and is re-exported from its old home, so existing
imports and tests are untouched. Sizing had its own inline copy of the same
conversion (`stop / tick_size * tick_value`); there is now one. It also gained
a refusal on a non-positive `tick_value`, which it did not have — a tick value
of zero previously made a price move worth nothing and produced silent zero
P&L rather than a refusal.

**Flooring** — the calculator now floors through
`app.symbols.precision.floor_to_step` / `normalize_quantity`, the same
implementation the broker layer uses. `test_there_is_one_flooring_implementation`
parses the module and fails if a second one reappears.

### ADD

- `backend/app/sizing/service.py` — counters, latency, logging.
- `backend/app/sizing/__init__.py` — the package's public surface.
- `backend/app/api/v1/position_sizing.py` — `POST /calculate`, `GET /modes`,
  `GET /status`.
- `backend/tests/test_sizing.py` — 59 tests covering the brief's 30 cases.
- 7 backtest sizing tests, 10 API tests, 4 frontend tests.
- `frontend/src/components/SizingCalculator.tsx` + its service and tests.

### REPLACE

Only the two behaviours in §2, both documented above.

### REMOVE

Nothing. No file, table, column, route or test was deleted.
`tools/mt5_paper.lot_for_risk` — the research toolkit's own sizing, and the
ancestor of this module's rules — is untouched, and `data/` was not written.

---

## 5. Safety invariants (§36)

| # | Invariant | How it holds |
|---|---|---|
| 1 | Size never exceeds Risk Engine limits | Risk runs *after* sizing and vetoes; `OrderProposal.volume` is checked against `max_position_size`. |
| 2 | Actual risk never silently exceeds configured risk | Recomputed after rounding and compared at full precision. Refuses. Tolerance is `STEP_EPSILON x step x risk_per_unit` — the exact bound of the float-floor guard, not a fudge. |
| 3 | Invalid stop placement rejected | §2.2. |
| 4 | Broker constraints respected | Every result, reported even on refusals. |
| 5 | Rounded to broker step | One shared implementation, always down. |
| 6 | Risk Engine has final authority | `PaperOms.submit` takes an `Approval` as its first positional argument, and `Approval` is constructible only by `RiskEngine.approve`. |
| 7 | AI cannot bypass risk | The AI seat runs before risk and can only decline. |
| 8 | TradingView cannot execute | The gateway publishes `SIGNAL_CREATED`; nothing consumes it. |
| 9 | Live trading disabled by default | `TRADING_MODE=paper`, `LIVE_TRADING=false`, all ten `LIVE_GATES` false. Unchanged. |
| 10 | Unknown state reconciled, never retried | `app/paper/oms.py` `unknown` exits only to `filled`/`cancelled`. Untouched. |
| 11 | No future data in backtests | Stop distance is `atr[entry-1]` — the figure `simulate()` itself used; equity is the balance from already-closed trades. Both pinned by tests. |
| 12 | No secrets in logs | The sizing service holds no broker connection; a log line carries a symbol, a side, two prices and three money figures. |
| 13 | Duplicate signals → one order | `orders.intent_id` is unique and the intent id IS the signal key. Untouched. |
| 14 | Working functionality preserved | Nothing removed; every pre-existing test still passes. |

**Architecture fences, asserted by tests:** `app/sizing/` imports neither
`app.brokers`, `app.paper` nor `app.risk`; the API router holds no adapter;
the backtester defines no sizing function of its own.

---

## 6. Tests

Baseline (before): backend **885 passed, 15 failed, 3 skipped**; research
**34**; frontend **77**. The 15 failures are `redis.exceptions` — Docker was
not running on this machine — and are environmental, not code.

After: backend **962 passed, 15 failed (the same Redis ones), 3 skipped**;
research **34**; frontend **81**. Ruff, ruff-format and mypy clean.

Brief §32's 30 cases: fixed quantity, fixed lot, equity percentage, monetary
risk, stop-loss sizing, long, short, invalid stop, zero/negative risk, zero
equity, tick size, tick value, quantity step, minimum, maximum, rounding down,
actual-risk recalculation, risk-ceiling rejection, broker metadata missing,
paper trading, backtesting, TradingView-shaped input, MT5 constraints,
duplicate signal, invalid numerics, extreme values, kill switch and
live-trading-disabled — all covered, plus determinism and the architecture
fences.

The brief's own worked example (§6: entry 100, stop 98, tick 0.01/0.01, risk
100 → **50 units**) is asserted literally by
`test_the_briefs_worked_example_computes_exactly_fifty_units`.

---

## 7. Remaining, and honest about it

- **Margin and leverage sizing** — the account does not report margin per
  symbol, so `insufficient_margin` remains a Risk Engine check against
  reported figures rather than a sizing input. Stated at `/v1/risk/limits`.
- **Currency conversion** — a stop priced in a non-account currency is not
  converted. Every instrument currently traded quotes in the account currency.
- **Replay** — fixed quantity, refused rather than faked (§3.5).
- **Per-instrument calculators** (§21's `EquityRiskCalculator`,
  `ForexRiskCalculator`, …) — not built, because `tick_size`/`tick_value` from
  the measured contract spec already expresses every instrument class this
  platform trades. Building four classes that all compute
  `distance / tick_size x tick_value` would be the duplication the brief warns
  against. When an instrument arrives that genuinely needs different
  arithmetic, the adapter goes in then.
- **Demo/live sizing** is identical to paper by construction (one engine, mode
  is not a parameter of it), but is unexercised against a live terminal
  because MT5 was not running here.
