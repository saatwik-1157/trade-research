# trade-research

Research and trading tooling where **every number is computed, never generated**
— and where the results are reported even when the result is "this does not
work".

Not advice. Nothing here tells you what to buy, and the one honest finding
running through all of it is that none of the strategies measured here has a
demonstrable edge.

**Built by Saatwik Sairaam Vasamsetti** · [github.com/saatwik-1157](https://github.com/saatwik-1157)

---

## Three things live in this repository

They share an idea and almost nothing else. Knowing which one you want is the
fastest way into the project.

| | What it is | Does it trade? | Start here |
|---|---|---|---|
| **1. Research pipeline** | Stock analysis from real data, machine-verified | No | [Stock research](#1-stock-research) |
| **2. MT5 harness** | An overnight session on a MetaTrader **demo** account | **Yes, by itself** | [The trading harness](#2-the-trading-harness) |
| **3. Execution platform** | FastAPI + Postgres + React; alerts → risk → OMS → broker | Only what you send it | [The platform](#3-the-execution-platform) |

Part 1 answers *"what is this company worth?"*. Part 2 answers *"does this
trading rule make money?"* — and measures the answer. Part 3 is the machinery
that would execute a rule if one ever worked.

---

## Quick start

**"I want to analyse a stock."**

```bash
python tools/snapshot.py NVDA --out reports/NVDA.snapshot.json
python tools/verify.py reports/NVDA.report.md reports/NVDA.snapshot.json --strict
```

In Claude Code, `/trade-analyze NVDA` runs the whole thing.

**"I want to run the overnight demo session."**

```bash
start-trading.bat                  # trades until 06:00, then closes everything
start-trading.bat --harvest-only   # wind down: close positions, open nothing new
```

**"I want to see how the live demo record actually looks."**

```bash
python tools/track_record.py            # report from the ledger
python tools/track_record.py --merge    # pull new closed trades first
```

Read the **R-multiple**, not the balance. The balance is flattered by the
mechanism explained under [Why the win rate lies](#why-the-win-rate-lies).

**"I want to run the platform."** See [The platform](#3-the-execution-platform).

**"I want this on a second laptop."** See
[`SECOND_MACHINE.md`](SECOND_MACHINE.md) — and read the account warning first.

---

## One executable

`python build_exe.py` freezes the whole toolkit behind a single entry point, so
it runs on a machine with no Python installed:

```bash
dist/trade-research/trade-research.exe                     # the 24 commands
dist/trade-research/trade-research.exe account --days 30
dist/trade-research/trade-research.exe track-record --merge
dist/trade-research/trade-research.exe <command> --help    # the tool's own help
```

It finds `data/` and `reports/` by searching upward from the working directory,
then from the executable, so it acts on the project you are standing in and
falls back to the one it was built from. `TRADE_RESEARCH_ROOT` overrides both.

A **directory** rather than a single file, and that is measured rather than
preferred. PyInstaller's onefile mode unpacks the whole bundle to a temporary
directory on every invocation:

| build | startup | on disk |
| --- | --- | --- |
| `--onefile` | 5,400 ms | 38 MB |
| default (onedir) | 1,000 ms | 71 MB |
| from source | 410 ms | — |

Five seconds per command is not a startup cost, it is a different tool. Pass
`--onefile` when one portable file matters more than speed.

## Why it is built this way

The common pattern for an "AI stock analyst" is to web-search a ticker and let
a language model write the report. That produces a document with RSI readings,
Fibonacci retracements and support levels to two decimal places — none of which
can be derived from a search snippet. A model cannot compute a Fibonacci
retracement from a headline, so those numbers are pattern-matched or invented,
and they arrive wrapped in the layout and register of a real analyst note. The
formatting is what makes it dangerous: it borrows credibility from a form it
has not earned.

This project inverts the arrangement. **Python computes; the model interprets
and never introduces a figure.** Then a verifier checks the finished note
against the data and flags anything that does not trace back.

| | Typical approach | Here |
|---|---|---|
| Price data | web search snippets | adjusted OHLCV from a market data API |
| Indicators | produced by the model | computed in `tools/indicators.py` |
| Financials | search results | SEC EDGAR XBRL, with accession numbers |
| Missing data | filled in plausibly | `null`, and listed in `data_gaps` |
| Score | asserted | backtested — and it **fails**, see below |
| Numbers in the report | trusted | machine-checked by `tools/verify.py` |

The same rule governs the trading side. A price the tool *chose* is not a price
the server *confirmed*, and logging the first as though it were the second makes
the discrepancy invisible rather than absent. That distinction has caught real
defects here, repeatedly.

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export SEC_USER_AGENT="your-project/1.0 your@email.com"   # identifies you to the SEC
```

For the trading harness (Windows only), additionally:

```bash
pip install MetaTrader5 psutil
```

**Do not skip `psutil`.** Nothing crashes without it, which is exactly why it
is easy to miss — the guard that refuses a second trading session on one
account is a `psutil` process scan, and absent it prints one line and proceeds
unguarded.

Install into a virtual environment rather than a global interpreter. `requests`
validates the versions of its own transitive dependencies at import and warns
when a globally installed package has displaced one of them — `urllib3-future`
and newer `chardet` builds both do it — which puts a spurious warning on top of
every run.

`SEC_USER_AGENT` should carry a real contact string. Requests still succeed
without it, but the SEC throttles unidentified traffic under load.

---

# 1. Stock research

```bash
# Full snapshot: indicators, filed financials, scores
python tools/snapshot.py NVDA --out reports/NVDA.snapshot.json

# Fast path, no SEC pull
python tools/snapshot.py NVDA --quick

# Check a written note against its data
python tools/verify.py reports/NVDA.report.md reports/NVDA.snapshot.json --strict

# Does the score predict anything?
python tools/backtest.py --universe tools/universe.txt --years 6 --horizon 63
```

`/trade-analyze NVDA` in Claude Code runs snapshot → five parallel analysts →
written note → verification gate.

## The composite score does not predict

Tested over six years across 35 large-cap US names, scoring each month end
using only bars available on that date:

| Horizon | Mean IC | t-stat | Q5−Q1 spread | Verdict |
|---|---|---|---|---|
| 21 days | +0.014 | 0.40 | −0.8% | no reliable edge |
| 63 days | +0.002 | 0.06 | −1.6% | no reliable edge |
| 126 days | +0.029 | 0.70 | −5.1% | no reliable edge |

The information coefficient is indistinguishable from zero, the top quintile
did not outperform the bottom, and the quintile means are not monotonic in the
score. **The composite describes measurable current state. It does not
predict.**

Reporting the null result is the feature. A 0–100 number that has never been
tested is decoration.

Only the price-derived components (technical, risk) are tested. Quality,
valuation and analyst inputs come from a vendor snapshot of *today's* figures,
so scoring a past date with them would leak the future backwards. They are
excluded, and the exclusion is reported in the output.

Uncorrected and stated rather than hidden: survivorship bias, no transaction
costs, overlapping forward windows that inflate the t-statistic, and a single
market regime.

---

# 2. The trading harness

A self-driving session on a MetaTrader 5 **demo** account. It opens positions on
a rule, closes them when they show a small profit, and writes every trade to an
accumulating ledger.

**It is a measuring instrument, not a strategy.** Read the next section before
running it.

## Why the win rate lies

The session harvests at `+$0.50` and lets losers run to their stop. That caps
every winner near the threshold while leaving every loser at full stop
distance, which manufactures a win rate above 90% **by construction**.

The balance rises for as long as no stop is hit, and gives it back when one is.
So the positions still open at the end of a session are, always, the losing
tail — on 2026-09-07 that was 7 positions, 6 underwater.

The figure that survives this is the **R-multiple**: each outcome divided by the
money actually at risk. A harvested win is worth about +0.01R and a stop is
−1.00R, and no number of the former pays for one of the latter.

## Running it

```bash
start-trading.bat                       # until 06:00 local, live on the demo account
start-trading.bat --harvest-only        # close only; open nothing new
start-trading.bat --until-hour 9        # stop at 09:00 instead
python tools/run_overnight.py --dry-run # show the resolved command, send nothing
```

Three things the session does that are worth knowing:

**It refuses to start twice.** Two harvest loops on one account compete for the
same position slots and double the risk per pass. The check names the running
pid. It is a process scan and *not* a lock file, because a lock outlives the
process that took it.

**It ends flat.** Whatever is still open at the stop hour is closed at what it
is worth, **losses included**. Over the final 45 minutes the profit floor decays
to zero so each position closes at the best moment it is offered rather than all
at the deadline, and nothing new opens inside that window.

**It holds the machine awake.** A session cannot close anything while the laptop
is asleep, and idle standby well short of an overnight run would let the
deadline arrive with the process suspended — the log simply stopping mid-evening
with every position open and no error to explain it. It holds the *system* and
not the display, so the screen still goes dark, and it does **not** defeat
closing the lid.

Every session tees to `logs/overnight-<timestamp>.log`.

## The ledger

```bash
python tools/track_record.py                  # report only
python tools/track_record.py --merge          # pull new closed trades first
python tools/track_record.py --all-magics     # every trade on the account
python tools/track_record.py --merge --exclude 58326177606 ...
```

Two things it now records that it did not before, both of which changed what the
sample means:

**Whose trades these are.** MetaTrader pairs deals for the *account*, not for
this tool, so a trade opened by hand or by another program arrived
indistinguishable from the harness's own. The entry deal's magic number is now
recorded and the merge defaults to the harness's own tag.

**Which account they came from.** A position id is unique per account rather
than globally, so a second demo account's trades merge in cleanly with nothing
to say they are not the same record. The report now breaks the ledger down by
account and raises a data gap when it spans more than one.

## What the rule searches found: nothing

This is the substance of the project, and it is a null result at every level.
Full detail with figures is in [`CLAUDE.md`](CLAUDE.md).

| Search | Universe | Result |
|---|---|---|
| Two live rules | 7 FX majors, 20k H1 bars | both net negative at real spreads (−1.50, −6.54 pts/trade) |
| 36-cell bracket sweep | majors | no cell reaches t=1.96; the **random** rule scored higher in sample |
| 41 candidates, 10 families | majors H1/H4/D1 | best in-sample t 1.29, negative out of sample |
| 25 entries × 8 exits | H1 and H4 | the only candidate ever to beat its null in sample (t=3.10) went to −1.83 out of sample |
| 16 trend entries × 8 exits | D1 | best in-sample t **6.59**, the highest here — then −1.82 out of sample; 0 of 128 cleared 1.96 against 3.2 expected |
| 8 non-USD crosses | crosses | best in-sample t 0.20; walk-forward 0 of 3 |
| 4 metals, 7 indices | metals, indices | large headline figures, both an arithmetic error — see the units warning |
| 7 crypto pairs | Binance daily | monotonic decay to negative across five eras |
| 28 candle-shape rules | majors H1 | best in-sample t **−0.04**; the shuffled null beat the real rules |

The only effect large enough to measure is **cost drag, and it is negative**:
the `random` rule loses at t = −3.60. Breakeven at a 1.5×ATR bracket needs a
50.5–52.7% win rate depending on the pair, and an edge the size of the scatter
between these rules would take roughly **6,600 trades** — about four years at
the observed rate — to demonstrate at 80% power.

Two traps this repository kept falling into, both now fenced in code:

**Units.** "Points" is price movement over the symbol's own point size, and
median H1 ATR runs 160 points in silver against 9,386 in palladium. Pooling
them adds numbers that are not the same quantity. `rule_search.py` raises a data
gap past a 5× spread; when it fires, quote per-symbol and never the pooled
figure.

**Clustering.** Seven majors all carrying USD open together on one dollar move,
so pooling counts one move seven times. Report the date-clustered t, not the
pooled one.

---

# 3. The execution platform

A FastAPI service, an execution worker, a Next.js frontend and the compose stack
that runs them. It does **not** trade on its own — it executes what is sent to
it, and every order passes the RiskEngine, the OMS and a broker adapter in that
order.

It has been driven end to end against a real MetaTrader demo venue, both by hand
and from a TradingView alert. See [`DEMO_VENUE_LIFECYCLE.md`](docs/broker/DEMO_VENUE_LIFECYCLE.md),
[`SIGNAL_PATH_FIRST_RUN.md`](docs/broker/SIGNAL_PATH_FIRST_RUN.md) and
[`LEDGER_COMPARISON.md`](docs/broker/LEDGER_COMPARISON.md).

## It is fail-closed

`TRADING_MODE` is one of:

* **`paper`** — the internal simulator. The default.
* **`demo`** — a real MT5 demo account, still fenced by
  `tools/mt5_paper.assert_demo`, which refuses any account the terminal reports
  as real.
* **`live`** — **refused.** It requires `LIVE_TRADING=true` *and* all ten flags
  in `LIVE_GATES` (`backend/app/core/settings.py`), every one of which is
  `False`. `/health` lists the blockers.

Beyond the mode fence: the RiskEngine is the only thing that can mint an
`Approval` and the OMS creates nothing without one; a close is *approved* rather
than vetoed, because limits that bound the risk of taking a position would
otherwise refuse to reduce exposure at the moment exposure is worst; an order in
`unknown` is never retried, because retrying an uncertain close is how a hedging
account opens a position the other way; and reconciliation reports
disagreements without repairing them.

## Running it

```bash
cp .env.example .env                       # defaults: paper mode, live off
docker compose up -d --build               # postgres 5440, redis 6390, UI on 8080

cd backend
pip install -r requirements-dev.txt
ruff check . && mypy app && pytest -q      # 3,060 tests
alembic upgrade head                       # 27 migrations
BOOTSTRAP_ADMIN_PASSWORD='choose a long one' python -m app.auth.bootstrap --email you@example.org
```

Then `http://127.0.0.1:8080` for the UI and `http://127.0.0.1:8080/health` for
the API. New accounts register as USER; an admin grants TRADER.

**The container cannot reach MetaTrader.** The image is Linux and the
`MetaTrader5` package is Windows-only, so driving a real venue means running a
Windows-local `uvicorn` and registering the venue and bot through the API —
those registries live in the process.

Optional, idempotent extras:

```bash
python -m app.db.import_ledgers --orders ../data/paper_trades.jsonl \
    --trades ../data/track_record.jsonl --reference ../reports/track_record.json
python -m app.symbols.seed          # symbols and provider mappings
python -m app.symbols.sync_mt5      # contract specs from a running terminal
python -m app.brokers.venue_audit --mode demo   # our record vs MetaTrader's own
```

`venue_audit` is the only check in the repository that is not self-referential:
it asks MetaTrader's deal history whether the platform's record of a trade is
true. Every other check compares the platform against itself, and that is
exactly how a P&L of `0.0000` sat in a row that every reader agreed with while
the account had received `−0.04`.

The JSONL files stay the system of record; the importer only reads them.

---

## Your own trade history

Two sources, one report format, so the numbers are comparable.

```bash
python tools/mt5_account.py --days 730          # MetaTrader 5, read-only
python tools/tv_import.py "List of Trades.csv"  # TradingView export
python tools/tv_import.py trades.csv --inspect  # check column detection first
python tools/market.py --prune                  # drop cache entries past 7 days
```

**MetaTrader 5** connects to a locally running terminal over IPC and pairs raw
deals into round-trip trades before computing anything — MT5 logs an open and a
close as two separate deals, so a win rate over raw deals is roughly
double-counted. It never calls `order_send`; it reads.

**TradingView has no public API.** Libraries claiming otherwise scrape an
internal websocket. The supported routes are CSV export and alert webhooks.
Column names vary between versions, so run `--inspect` on an unfamiliar export;
if no P&L column is found the tool refuses rather than guessing.

`tools/tv_webhook.py` receives alerts and **records them without ever trading**.
TradingView cannot reach localhost, so expose it with a tunnel, and because it
cannot send custom headers the shared secret travels in the alert body — it is
stripped from keys and from string values at any depth before anything is
logged.

Both report a `sample_size_assessment`. Under about 100 trades, win rate and
profit factor are dominated by luck.

---

## Layout

```
tools/
  ── research ────────────────────────────────────────────────
  market.py         market data access and disk cache
  indicators.py     RSI, MACD, ATR, ADX, Bollinger, beta, pivots, Fibonacci
  edgar.py          SEC XBRL filed financials, period-aligned
  score.py          deterministic composite; missing components drop out
  snapshot.py       builds the JSON contract the agents consume
  backtest.py       point-in-time forward-return test of the score
  verify.py         checks every number in a note against the snapshot
  patterns.py       candlestick and price patterns, tested for forward edge

  ── trading ─────────────────────────────────────────────────
  mt5_paper.py      THE ONLY MODULE THAT SENDS ORDERS - demo only, fenced in code
  take_profit.py    the harvest loop, the wind-down and the flat-by deadline
  run_overnight.py  session launcher: single-session guard, logging, stop hour
  track_record.py   the accumulating live ledger, by regime, magic and account
  mt5_account.py    read-only MetaTrader account analysis
  trade_stats.py    execution statistics, shared by both trade sources

  ── measurement ─────────────────────────────────────────────
  rule_backtest.py  replays the mt5_paper rules against MT5 history
  rule_search.py    rule families vs a permutation null, walked forward, clustered
  exit_search.py    entry x exit combinations, including exits that let winners run
  shape_search.py   candle shape and volatility regime
  bracket_sweep.py  SL/TP grid with an out-of-sample holdout
  cost_hurdle.py    breakeven win rate the spread imposes
  cost_profile.py   where the spread hurdle is smallest
  swap.py           overnight financing, refusing units it cannot convert
  crypto_market.py  crypto OHLCV, measured in percent so symbols compare
  tv_import.py      TradingView CSV importer
  tv_webhook.py     TradingView alert receiver (records, never trades)
  project_state.py  generates PROJECT_STATE.json - never hand-write it

tests/              seven suites, plain python, no framework, all in CI
backend/            the platform: API, worker, risk, OMS, brokers, 3,060 tests
frontend/           Next.js 16 + React 19 + TypeScript + Tailwind
.claude/skills/     trade-analyze, trade-quick, trade-verify, trade-backtest,
                    pattern-study, mt5-account, mt5-paper-trade,
                    tradingview-import, tradingview-webhook
```

Run the matching suite after touching a tool — the mapping is in
[`CLAUDE.md`](CLAUDE.md). All seven run in CI against Python 3.10, 3.12 and 3.14.

Design, policy, runbook and audit documents are indexed in
[`docs/README.md`](docs/README.md).

---

## Design rules

**Computed, never generated.** Where history is too short, the field is `null` —
a plausible substitute is worse than a gap, because a gap is visible.

**Read the fill, never the quote.** A figure the tool chose is not a figure the
server confirmed. One live trade was a loss fixed at order time and the order
log hid it for three days by recording the requested price as the entry.

**A missing figure is a gap, not a zero.** A close whose money the venue did not
report books nothing. `(exit − entry) × lots` omits the contract size and is
wrong for every instrument whose contract size is not 1 — the server already
knows the answer, so it is read rather than derived.

**Filed financials are period-aligned.** A 10-K contains quarterly facts that
also carry `fp="FY"`. Filtering naively mixes a Q4 revenue figure into an annual
series and yields a gross margin of 570% — which happened here, and is what
`period_alignment` exists to expose.

**Missing data never becomes a neutral default.** A component with no inputs is
dropped and the remaining weights renormalised, with `coverage` reporting how
much of the intended weight survived.

**Sentiment is never scored.** There is no defensible mapping from headlines to
a number. The sentiment agent writes prose with source URLs and is forbidden
from emitting a score.

**No entry, stop, target or position size in a research note.** Specific levels
plus a "not financial advice" disclaimer is still trade construction, and the
composite behind it has no measured edge.

**The bear case is written at equal length.** Enforced in the synthesizer
prompt. Most retail losses come from a process that only looked for
confirmation.

**State the requirement, not the state.** "`market_bars` is empty" is a claim
about a mutable table and went stale on the first ingestion, in 69 lines across
35 files. Say what is *required* instead — it stays true until the thing that
matters changes.

---

## What this cannot do

No proprietary data, no channel checks, no earnings model, no view on guidance.
Valuation is absolute rather than sector-relative, so fast compounders score
expensive and declining businesses score cheap. The verifier checks numbers, not
reasoning — a note can pass with every figure correct and still reach a
conclusion the figures do not support.

On the trading side: nothing measured here has an edge, and "no edge" and "an
edge too small to see with this much data" are not separable at these sample
sizes. Say that, rather than picking whichever reading suits.

It is a way to gather real evidence quickly, and to be forced to look at the
result you did not want. It is not an edge.
