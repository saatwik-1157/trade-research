# The nine viral prompts, and what this repository does instead

A carousel circulating on Instagram in August 2026 — *"CLAUDE can replace hours
of stock research in minutes"* — lists nine prompts "smart traders are quietly
using." A second post pairs Claude with TradingView and promises charts
"analyzed before you even ask."

This page exists because seven of those nine prompts ask a language model to
produce numbers it has no way to compute, and that is the exact failure this
project was built to make impossible:

> **Python computes. The model interprets. The model never introduces a
> figure.** — `CLAUDE.md`

The prompts are not stupid. Every one of them names a question a trader
genuinely wants answered. The problem is the answer's provenance: an RSI
reading, a support level, a win rate or a maximum drawdown can be generated
fluently with no data behind it, and the result is indistinguishable from real
analysis by eye. `tools/verify.py` exists because instructing a model to "cite
or omit" is a request, and this repository wanted a test.

So: each prompt below, what it actually asks for, and the command here that
computes the honest version — including the two cases where the honest answer
is that the data does not exist.

---

## 1. Trade Idea Generator

> *Analyze today's market and identify 5 high-probability trade opportunities
> for [sector]. For each setup, provide the suggested entry price, profit
> targets, stop-loss level, and expected risk-to-reward ratio.*

**What it asks for:** four generated numbers per idea, times five ideas.

**What this repo does:** refuses. From `CLAUDE.md`:

> No entry prices, stop losses, profit targets or position sizes — that is
> trade construction whatever disclaimer is attached, and the composite has no
> measured predictive power.

```bash
python tools/snapshot.py TICKER --out reports/TICKER.snapshot.json
python tools/verify.py reports/TICKER.report.md reports/TICKER.snapshot.json --strict
```

Or the `trade-analyze` skill, which runs the whole pipeline and gates the
finished note on `verify.py --strict`.

**Why the refusal is not squeamishness:** the composite score has been
backtested. Mean IC ≈0.00–0.03, t < 1, negative quintile spreads. It describes
current state; it does not predict. "High-probability" is a claim that has been
measured here and did not hold.

---

## 2. Automated Technical Analyst

> *Evaluate [ticker] using both daily and weekly timeframes. Identify key
> support and resistance levels, trendlines, moving averages, and momentum
> indicators. Then deliver a clear Buy, Hold, or Sell signal.*

**What it asks for:** levels and a signal.

**What this repo does:** computes the indicators from real OHLCV in
`tools/indicators.py`, surfaces them through `snapshot.py`, and stops short of
the signal.

```bash
python tools/snapshot.py TICKER --quick     # or the trade-quick skill
```

The `trade-quick` skill says it outright: *do not write a thesis, a
recommendation, or entry and exit levels off a quick snapshot. Report what was
measured.*

**The measurement behind that:** `rsi_reversion` and `sma_cross` — the two
rules a "moving averages and momentum" prompt is reaching for — were run over
20,000 H1 bars across seven FX majors. Neither separates from a coin flip. At
this broker's real spreads both are net negative, −1.50 and −6.54 points per
trade. The only result in that exercise reaching significance is that the
**random** rule loses, at t = −3.60. Cost drag was the one effect large enough
to measure.

---

## 3. News-to-Trade Converter

> *Summarize the most recent news related to [company/sector] and convert it
> into actionable trading insights. Outline the potential short-term and
> long-term impact, expected price movement range, and suggested positioning.*

**What it asks for:** a numeric price range, derived from prose.

**What this repo does:** the `sentiment-scanner` agent in `.claude/agents/`
reads news and returns prose with source URLs attached. From `CLAUDE.md`:

> Sentiment is prose with source URLs, never a number.

An "expected price movement range" inferred from headlines has no estimator
behind it. The sentence is the output; the number would be decoration.

---

## 4. Strategy Backtester

> *Backtest the [strategy] on [stock/index] over the past [time period]. Report
> the win rate, profit factor, maximum drawdown, and suggest potential
> improvements.*

**This is the most dangerous one on the list.** Asked this, a model will return
a clean table of statistics it did not compute, and there is nothing in the
output to distinguish it from one that was measured.

**What this repo does:** actually backtests, with five tools and four
corrections.

```bash
python tools/rule_backtest.py                    # replay rules against MT5 history
python tools/rule_search.py                      # many families vs a permutation null
python tools/bracket_sweep.py                    # SL/TP grid with a holdout
python tools/exit_search.py --timeframe D1       # entries x exit structures
python tools/backtest.py --universe tools/universe.txt --years 6 --horizon 63
```

The corrections are the point, and no prompt produces them: a **permutation
null** per candidate (its own signals shuffled, preserving trade count and
buy/sell mix, so the null pays the same spread), a **Bonferroni threshold** over
the true candidate count, **era blocks**, a **walk-forward**, and **date-clustered
inference**.

**What that machinery is worth, in one number.** The highest t-statistic this
repository has ever produced is 6.59 — `donchian_brk_100` with a
move-to-breakeven trail at D1, clearing both its 3.546 Bonferroni threshold and
a permutation null that reached 5.5. Out of sample it posts **−1.82**, at −204
points. Zero of 128 combinations cleared 1.96 out of sample against 3.2
expected by chance; the search came in *below* chance.

A model asked to "report the win rate" would have reported the 6.59 and
stopped. Six search families across five universes have now failed their own
nulls. The improvements Prompt 4 asks it to suggest have been tried: wider
exits, cheaper timeframes, hour filters, four additional universes. From
`CLAUDE.md`: *nothing here was ever one cost adjustment away from working.*

---

## 5. Portfolio Risk Manager

> *Evaluate my portfolio: [tickers and % allocations]. Identify areas of
> overexposure, weak positions, and hidden correlations. Recommend
> risk-adjusted rebalancing and hedging strategies designed to withstand a
> potential 20% market decline.*

**What it asks for:** correlations, invented.

**What this repo does:** `app/portfolio/allocation.py`, and the RiskEngine's
correlation veto — which **fails** when a correlation limit is configured
without the data to evaluate it. A MISSING input fails the check rather than
passing it. `CLAUDE.md` puts the general form well:

> State the REQUIREMENT instead — correlation needs a common window across two
> or more instruments.

That is the whole difference. Asked for hidden correlations with no price
history in context, the prompt gets plausible ones. Asked the same thing, this
project returns `not_enforced` and names the gap.

Scenario work — the "withstand a 20% decline" half — lives in
`PORTFOLIO_SCENARIO_ARCHITECTURE.md` and the stress policies, and it is
calibrated rather than asserted.

---

## 6. Trading Journal Analyzer ✅

> *Analyze my last 20 trades: [insert trades with entry, exit, and results].
> Identify recurring errors, missed opportunities, and behavioral biases. Then
> provide 3 personalized rules to improve consistency immediately.*

**This one is good**, and this repository is better placed to run it than
almost anyone reading that post, because it has 869 real closed trades rather
than a recollection of twenty.

```bash
python tools/track_record.py --merge     # pull new closed trades, then report
```

`tools/trade_stats.py` normalises MT5 and TradingView into one schema so the
two are directly comparable rather than "two dialects of win rate that quietly
mean different things."

**One correction to the prompt: not 20.** This account has already run that
experiment, and it is recorded in `CLAUDE.md`:

> On 20 trades this account scored +4.60 at a pooled t of 3.27; seven trades
> later it was +3.44 at 1.15, and −0.10 clustered by entry hour. Nothing
> changed except that the sample grew.

Twenty trades is where a narrative is most confident and least supported. The
current ledger stands at 617 trades on this account and needs roughly 89 more
before the pooled t reaches 1.96. "Recurring errors" found in 20 are noise with
a story attached.

---

## 7. Fully Automated Trade Plan ⚠️

> *Create a structured daily trading plan for [market/asset]. Include a
> pre-market scan, opening execution strategy, midday adjustments, and closing
> approach. Present the plan as a time-stamped checklist I can follow step by
> step.*

**Mostly fine.** This is process, not numbers, and a checklist is a reasonable
thing to ask a model for. The sober equivalents here are
`FIRST_LIVE_TRADE_CHECKLIST.md`, `LIVE_TRADING_RUNBOOK.md` and
`PRODUCTION_OPERATIONS_RUNBOOK.md`.

**The caution is in the word "automated."** From `CLAUDE.md`:

> The only effect that replicates out of sample is negative: tight brackets
> trade often and pay the spread every time. Frequency is the one lever with a
> proven sign, and it points down — which is the reason not to wire these rules
> to an interval and leave them running.

A time-stamped checklist that fires on a schedule is precisely that wiring. If
you build it, `run_overnight.py` is the version that has been through the
failure modes: it ends flat by default, it refuses a weekend deadline in the
*venue's* week, a risk halt is not the deadline, and it records
`ended_not_flat` rather than `completed` when its flush is refused. Four
sessions in September are documented in `PROJECT_STATUS.md`; two of them ended
badly, and neither for a reason a checklist would have anticipated.

---

## 8. Smart Money Tracker ❌

> *Analyze the latest institutional activity for [stock/ticker]. Identify hedge
> fund positions, insider buying/selling, unusual options activity, and volume
> anomalies. Explain what smart money appears to be doing.*

**There is no honest version of this prompt in this repository, and that is the
finding.**

`tools/edgar.py` pulls filed financials from the SEC XBRL company-facts API
with an accession number and filing date on every figure — but that is 10-K and
10-Q data. It does not cover 13F holdings, Forms 3/4/5, or options flow. No
data source for those exists here.

The repository's answer to this question is the one in `PROJECT_STATUS.md`
under **Blocked**:

> Base rates, expectation gaps, channel and guidance intelligence — no data
> source exists. Not stubbed; they return `insufficient_data` naming the gap.

Asked about institutional positioning with no filings in context, a model has
four plausible-sounding answers available and no way to tell you which it used.
Volume anomalies are computable from OHLCV; the other three are not computable
from anything this project has.

---

## 9. Earnings Prediction Engine

> *Review the last 8 earnings reports for [company]. Identify recurring
> patterns in revenue, EPS, guidance, and price reactions. Predict the most
> likely outcome for the next earnings report and outline bullish, bearish, and
> neutral trading scenarios.*

**Half of this is implementable and half is not.**

The review half is real work and `tools/edgar.py` does it properly:

```bash
python tools/edgar.py TICKER
```

Filed figures straight from the filings, with two XBRL traps handled that
"silently produce numbers that look plausible and are wrong" — a 10-K carries
quarterly facts tagged `fp="FY"` alongside annual ones, and the `fy` field is
the fiscal year of the *filing*, not of the fact. Flow concepts are filtered on
period duration instead. Always check `edgar.period_alignment` before quoting
any ratio; mismatched fiscal periods are the known trap here.

The prediction half has no estimator. Scenarios written off eight filings are
prose, and should be labelled as prose.

---

## The scoreboard

| # | Prompt | Verdict | Honest version |
|---|---|---|---|
| 1 | Trade Idea Generator | ❌ generates 4 numbers per idea | `trade-analyze`, no levels |
| 2 | Automated Technical Analyst | ❌ generates levels and a signal | `snapshot.py`, `indicators.py` |
| 3 | News-to-Trade Converter | ❌ generates a price range | `sentiment-scanner`, prose + URLs |
| 4 | Strategy Backtester | ❌❌ fabricates statistics | `rule_search.py` and four corrections |
| 5 | Portfolio Risk Manager | ❌ generates correlations | RiskEngine veto, fails closed |
| 6 | Trading Journal Analyzer | ✅ good — but n≫20 | `track_record.py`, `trade_stats.py` |
| 7 | Fully Automated Trade Plan | ⚠️ process is fine, automation is the risk | `run_overnight.py` |
| 8 | Smart Money Tracker | ❌ no data source exists | returns `insufficient_data` |
| 9 | Earnings Prediction Engine | ◐ review yes, predict no | `edgar.py` |

Two of nine survive intact. One survives with a sample-size correction the
prompt gets wrong by a factor of forty.

---

## A note on the source

The first carousel is from `stockizen_research`, a SEBI-registered Research
Analyst (INH000017675), and its caption carries a full disclaimer: educational
and informational only, not investment advice, not a recommendation.

That disclaimer is accurate and the slides are in tension with it. A prompt
that returns "5 high-probability trade opportunities" with entry prices and
stop-losses is trade construction, and a disclaimer in the caption does not
change what the output is. `CLAUDE.md` takes the same position about this
repository's own output, which is why the rule there is phrased *whatever
disclaimer is attached*.

Both posts are also engagement mechanics — the final slide asks you to comment
"CLAUDE" to be sent the prompts, which is why the comment section is several
hundred people typing one word. The nine prompts are reproduced above in full,
so there is nothing left to unlock.

---

## What to take from this

The prompts are a good inventory of what people want from research tooling.
Read as a specification, the list is useful: idea generation, technical
reading, news synthesis, backtesting, portfolio risk, journal analysis,
planning, positioning data, earnings. That is a reasonable product.

Read as instructions to a model, seven of the nine are requests to fabricate,
and the one they push hardest — Prompt 4 — is the one where fabrication is
least detectable and most expensive.

This project has the measured version of most of them. It also has the answer
those measurements keep returning, which is the part no carousel will post:
across six search families, five universes, 36-cell bracket sweeps, 128-cell
exit grids and 869 live trades, **nothing here has demonstrated an edge**, and
the live ledger has now moved from "indistinguishable from zero" to negative
with significance under symbol clustering.

`ModelTrainingNeedAssessment` = **DO_NOT_TRAIN**. A model would search the same
space with more parameters. So would a prompt.
