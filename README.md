# trade-research

A stock research pipeline for Claude Code where every number is computed from
real data, every figure traces back to a source, and the composite score has
been tested against forward returns instead of merely asserted.

Research tool. Not advice. It does not trade, and it does not tell you what to
buy.

## Why it is built this way

The common pattern for an "AI stock analyst" is to web-search a ticker and let
a language model write the report. That produces a document with RSI readings,
Fibonacci retracements and support levels to two decimal places — none of which
can be derived from a search snippet. A model cannot compute a Fibonacci
retracement from a headline, so those numbers are pattern-matched or invented,
and they arrive wrapped in the layout and register of a real analyst note. The
formatting is what makes it dangerous: it borrows credibility from a form it
has not earned.

This project inverts the arrangement. Python computes; the model interprets and
never introduces a figure. Then a verifier checks the finished note against the
data and flags anything that does not trace back.

| | Typical approach | Here |
|---|---|---|
| Price data | web search snippets | adjusted OHLCV from a market data API |
| Indicators | produced by the model | computed in `tools/indicators.py` |
| Financials | search results | SEC EDGAR XBRL, with accession numbers |
| Missing data | filled in plausibly | `null`, and listed in `data_gaps` |
| Score | asserted | backtested — and it **fails**, see below |
| Numbers in the report | trusted | machine-checked by `tools/verify.py` |

## Setup

```bash
pip install -r requirements.txt
export SEC_USER_AGENT="your-project/1.0 your@email.com"   # the SEC requires this
```

## Use

```bash
# Full snapshot: indicators, filed financials, scores
python tools/snapshot.py NVDA --out reports/NVDA.snapshot.json

# Fast path, no SEC pull
python tools/snapshot.py NVDA --quick

# Check a written note against its data
python tools/verify.py reports/NVDA.report.md reports/NVDA.snapshot.json --strict

# Does the score predict anything?
python tools/backtest.py --universe tools/universe.txt --years 6 --horizon 63

python tests/test_indicators.py
python tests/test_verify.py
python tests/test_cache.py
```

In Claude Code, `/trade-analyze NVDA` runs the whole pipeline: snapshot, five
parallel analysts, written note, verification gate.

## The backtest result

The composite was tested over six years across 35 large-cap US names, scoring
each month end using only bars available on that date:

| Horizon | Mean IC | t-stat | Q5−Q1 spread | Verdict |
|---|---|---|---|---|
| 21 days | +0.014 | 0.40 | −0.8% | no reliable edge |
| 63 days | +0.002 | 0.06 | −1.6% | no reliable edge |
| 126 days | +0.029 | 0.70 | −5.1% | no reliable edge |

The information coefficient is indistinguishable from zero, the top quintile
did not outperform the bottom, and the quintile means are not monotonic in the
score. **The composite describes measurable current state. It does not
predict.**

That is the honest result, and it is the one piece of information the
score-generating tools in this genre never supply. A 0-100 number that has
never been tested is decoration; it looks like analysis and carries no
information. Reporting the null result is the feature.

Only the price-derived components (technical, risk) are tested. Quality,
valuation and analyst inputs come from a vendor snapshot of *today's* figures,
so scoring a past date with them would leak the future backwards and produce an
excellent, meaningless backtest. They are excluded, and the exclusion is
reported in the output.

Remaining biases, uncorrected and stated rather than hidden: survivorship (the
universe is names that still trade today), no transaction costs, overlapping
forward windows that inflate the t-statistic, and a single market regime.

## Layout

```
tools/
  market.py       market data access and disk cache — the only provider touchpoint
  indicators.py   RSI, MACD, ATR, ADX, Bollinger, beta, swing pivots, Fibonacci
  edgar.py        SEC XBRL filed financials, period-aligned
  score.py        deterministic composite; missing components drop out, never default to 50
  snapshot.py     builds the JSON contract the agents consume
  backtest.py     point-in-time forward-return test of the score
  verify.py       checks every number in a written note against the snapshot
  universe.txt    default backtest universe
  trade_stats.py  execution statistics, shared by both trade sources
  mt5_account.py  read-only MetaTrader 5 account analysis
  tv_import.py    TradingView CSV trade-export importer
  tv_webhook.py   TradingView alert receiver (records signals, never trades)
.claude/
  skills/         trade-analyze, trade-quick, trade-verify, trade-backtest,
                  mt5-account, tradingview-import, tradingview-webhook
  agents/         technical, fundamental, sentiment, risk, thesis
tests/
  test_indicators.py   indicator maths checked against independent calculations
  test_verify.py       the sourcing gate, against a fixture snapshot
  test_cache.py        cache pruning removes only what is past its age
```

## Design rules

**Computed, never generated.** Indicators come from arithmetic on a price
series. Where history is too short, the field is `null` — a plausible
substitute is worse than a gap, because a gap is visible.

**Fibonacci levels name their swing.** Every retracement carries the swing high,
the swing low and both dates. A level with no swing behind it is meaningless.

**Filed financials are period-aligned.** A 10-K contains quarterly facts that
also carry `fp="FY"`, and the `fy` field is the filing's year rather than the
fact's. Filtering naively mixes a Q4 revenue figure into an annual series and
yields a gross margin of 570% — which happened during development, in this
repository, and is what `period_alignment` now exists to expose.

**Missing data never becomes a neutral default.** A component with no inputs is
dropped and the remaining weights renormalised, with `coverage` reporting how
much of the intended weight survived.

**Sentiment is never scored.** There is no defensible mapping from headlines to
a number. The sentiment agent writes prose with source URLs and is explicitly
forbidden from emitting a score, because a fabricated one would propagate into
the composite as though it had been measured.

**No entry, stop, target or position size.** Specific levels plus a "not
financial advice" disclaimer is still trade construction, and the composite
behind it has no measured edge. Support and resistance are reported as places
price has previously turned, with dates.

**The bear case is written at equal length.** Enforced in the synthesizer
prompt. Most retail losses come from a process that only looked for
confirmation.

## What this cannot do

It has no proprietary data, no channel checks, no earnings model, and no view
on guidance. Valuation is absolute rather than sector-relative, so fast
compounders score expensive and declining businesses score cheap. Sell-side
targets are structurally optimistic and lag price. The verifier checks numbers,
not reasoning — a note can pass with every figure correct and still reach a
conclusion the figures do not support.

It is a way to gather real evidence quickly and to be forced to look at the
bear case. It is not an edge.

## Your own trades

Two sources, one report format, so the numbers are directly comparable.

```bash
python tools/mt5_account.py --days 730          # MetaTrader 5, read-only
python tools/market.py --prune                  # drop cache entries past 7 days
python tools/tv_import.py "List of Trades.csv"  # TradingView export
python tools/tv_import.py trades.csv --inspect  # check column detection first
```

**MetaTrader 5** connects to a locally running terminal over IPC. It pairs raw
deals into round-trip trades before computing anything — MT5 logs an open and a
close as two separate deals, so a win rate over raw deals is roughly
double-counted. The module never calls `order_send` or any other mutating
endpoint; it reads.

**TradingView has no public API.** Nothing can be connected to directly, and
libraries claiming otherwise scrape an internal websocket. The supported routes
are CSV export (handled here) and alert webhooks. Column names vary between
versions, so run `--inspect` on any unfamiliar export before trusting the
output; if no P&L column is found the tool refuses rather than guessing.

For live signals rather than history, `tools/tv_webhook.py` receives TradingView
alert webhooks. TradingView cannot reach localhost, so expose it with a tunnel
(`cloudflared tunnel --url http://localhost:8000`), and because TradingView
cannot send custom headers the shared secret travels in the alert body. The
secret is stripped from keys and from string values at any depth before
anything is logged. **The receiver records alerts and never places trades.**

Both report a `sample_size_assessment` alongside the statistics. Under about
100 trades, win rate and profit factor are dominated by luck — and Strategy
Tester output is a simulation of a strategy usually tuned on the same data,
which is the least predictive number in trading.
