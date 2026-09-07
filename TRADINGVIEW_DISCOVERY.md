# TRADINGVIEW_DISCOVERY.md

What "automatic discovery" can legitimately mean, what each source actually
yields, and where the pipeline starts without an API. Written 2026-09-03 for
the autonomous-builder brief §2–§4.

---

## 1. There is nothing to connect to

TradingView publishes **no public API for reading your account, charts,
scripts or strategies.** This is not a missing credential or an unimplemented
adapter; the endpoint does not exist. `tools/tv_import.py` has said so in its
docstring since August and it is still true.

So discovery is not "log in and enumerate strategies". It is "detect which of
the four legitimate inputs is present, and extract what that input can
actually carry". Anything else on offer — a session-cookie scrape, a headless
browser reading a protected script, an unofficial reverse-engineered endpoint
— is refused under brief §2 and is not designed here.

**Refused, explicitly:** reading a protected or invite-only script's source;
authenticating as the user to a private account; scraping chart data behind a
paywall; working around a rate limit or CAPTCHA. Each is an access control,
and a system that steps over one is not autonomous, it is unauthorised.

## 2. The four legitimate sources

| # | Source | How it arrives | Needs | Carries |
|---|---|---|---|---|
| 1 | **Alert webhook** | TradingView POSTs an alert body to a URL | paid plan (Essential+), a public tunnel, a shared secret | one *decision*: ticker, action, time, optional price/strategy/timeframe |
| 2 | **Pine Script source** | the user supplies the `.pine` text they own or that is published open-source | nothing | the *whole rule set*: indicators, params, conditions, entries, exits, brackets, declared sizing |
| 3 | **Strategy Tester export** | "List of Trades" → CSV | paid plan for deep history | the *result claim*: one row-pair per trade, P&L, entry/exit prices and times |
| 4 | **Strategy configuration** | the user states symbol, timeframe, inputs | nothing | the *run context* a source alone does not fix |

Only source 2 answers brief §3's question list. An alert says what to do, not
why; an export says what happened, not what the rule was. This matters for
sequencing: **without Pine text there is no strategy to compile**, only alerts
to route and results to reconcile.

## 3. What each source yields, field by field

Brief §3 asks discovery to identify 22 things. Honest coverage:

| §3 field | Webhook alert | Pine source | CSV export | Config |
|---|---|---|---|---|
| strategy name | if the alert names it | `strategy("...")` | filename only | ✓ |
| strategy version | if the alert names it | `//@version=N` + own hash | – | ✓ |
| Pine version | – | ✓ | – | – |
| symbol | ✓ `ticker` | usually not (chart-bound) | if a column exists | ✓ |
| timeframe | if supplied | usually not (chart-bound) | inferable, unreliably | ✓ |
| indicators | – | ✓ | – | – |
| indicator parameters | – | ✓ (`input.*` defaults) | – | ✓ overrides |
| variables | – | ✓ (single-assignment only) | – | – |
| conditions | – | ✓ | – | – |
| entry rules | the *fired* one | ✓ | implied by rows | – |
| exit rules | the *fired* one | ✓ | implied by rows | – |
| stop loss | advisory only | ✓ `strategy.exit(stop=)` | implied | – |
| take profit | advisory only | ✓ `strategy.exit(limit=)` | implied | – |
| trailing stop | advisory only | ✓ `trail_points/trail_offset` | – | – |
| position sizing | advisory only | ✓ declared (`default_qty_*`) | volume column | – |
| filters | – | ✓ if expressible | – | – |
| sessions | – | recorded, **unsupported** | – | – |
| alerts | ✓ is one | `alertcondition`/`alert()` | – | – |
| order types | advisory only | ✓ | – | – |
| long / short conditions | the fired side | ✓ | ✓ direction | – |
| dependencies | – | ✓ (`request.*`, libraries) | – | – |
| unsupported functionality | n/a | ✓ **this is the point** | n/a | n/a |

"Advisory only" is the L09 rule already in force: `app/webhooks/schema.py`
records `quantity`, `sl`, `tp` and `risk` from an alert into `advisory` and the
platform never obeys them. An alert that could set its own lot size is an alert
that could set its own risk limit. The same rule extends to Pine's declared
sizing in §4 below.

## 4. Where autonomy actually starts: the inbox

Since no source pushes a *strategy*, the unattended entry point is a watched
directory rather than a poll:

```
data/tradingview/inbox/     the user drops a .pine, .csv or .json here
data/tradingview/accepted/  discovered, hashed, spec written
data/tradingview/refused/   with the refusal reason beside it
```

One drop, and A0→A6 run without further input: discover → parse → normalize →
compile → generate tests → backtest → replay. That is the maximum honest
automation available, and it is a great deal of it — what it does not include
is inventing the source.

Nothing under `data/` is rewritten (standing rule 3). The inbox only ever gains
files, and a processed source is moved, never edited.

The webhook path (source 1) is already unattended and already live at
`POST /v1/webhooks/tradingview`. It needs no inbox.

## 5. Components that already do this work

Three exist. None is replaced.

| Component | Decision | Why |
|---|---|---|
| `tools/tv_webhook.py` (257 lines) | **KEEP** unchanged | the operator's standalone receiver: stdlib only, records to JSONL, holds no credentials, places nothing. Right tool for a run somebody is watching |
| `backend/app/webhooks/` (L09, 712 lines) | **KEEP**, extend at A8 only | the server path: constant-time secret, 7-word action vocabulary, redaction at depth, age check, idempotency on sender id or fingerprint, symbol resolution, `SIGNAL_CREATED`. The alert half of discovery is finished |
| `tools/tv_import.py` (CSV) | **KEEP**, call from A6 | tolerant column matching across TradingView's varying export layouts, already reconciling into the MetaTrader trade schema. It becomes the *reconciliation* input, not a second parser |

What A0 adds is the source **detector and inventory** — which of the four is
present, what it can carry, what it cannot — plus the inbox. It adds no second
webhook handler and no second CSV parser (brief §9).

## 6. What discovery refuses rather than guesses

- **A source whose symbol or timeframe is unknown.** Pine is chart-bound; a
  script does not say what it ran on. Discovery records `symbol: null` and the
  orchestrator stops at NORMALIZED asking for it. It does not default to
  EURUSD H1 because that is what the repository has most data for.
- **A protected script.** No source text, no compile. Reported, not worked
  around.
- **An unmapped symbol.** L11 already refuses ambiguous resolution and
  `validate_for_trading` gates tradability. Discovery inherits both.
- **A CSV whose P&L column cannot be found.** `tv_import.load` already raises
  with the detected headers rather than parsing zeros.

## 7. The one input required from the operator

Per brief §41: nothing in the autonomy ladder can begin against a real
strategy until a legitimate source exists on this machine.

- **Missing:** any TradingView strategy source. `data/tradingview/` does not
  exist yet and no `.pine` file is in the repository.
- **Why required:** sources 1, 3 and 4 cannot describe a rule set; only Pine
  text can, and it is the input to every stage after discovery.
- **What is available:** the alert path end to end (recorded, not executed),
  the CSV importer, and `tests/tv_list_of_trades.csv` as a fixture.
- **What is blocking:** nothing technical. A0–A2 can be built and tested
  against synthetic Pine fixtures written for the declared subset.
- **What input is needed:** one `.pine` file the user owns or that is published
  open-source, plus the symbol and timeframe it was run on.
