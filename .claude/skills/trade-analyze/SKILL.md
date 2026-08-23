---
name: trade-analyze
description: Full research note on a stock - computes technicals from real OHLCV, pulls filed financials from SEC EDGAR, runs five analysis agents in parallel, and verifies every number in the output traces back to the data. Use when asked to analyze, research, or write up a ticker.
---

# Full stock research note

Produces a research note on one ticker where every figure is computed from real
data and machine-checked before the note is shown to anyone.

## The rule this workflow exists to enforce

**A number that is not in the snapshot JSON may not appear in the report unless
it carries an inline source URL on the same line.**

Language models are good at producing text that has the shape of analysis. An
RSI reading, a Fibonacci level or a support price can be generated fluently
without any price series behind it, and the result is indistinguishable from
real analysis by eye. That is the failure mode this pipeline is built to make
impossible: the numbers come from `tools/`, the agents only interpret them, and
`tools/verify.py` checks the finished note.

## Steps

### 1. Build the snapshot

```bash
python tools/snapshot.py TICKER --out reports/TICKER.snapshot.json
```

This computes every indicator from adjusted OHLCV, pulls filed annual
financials from SEC EDGAR with accession numbers attached, and scores five
dimensions. Read the resulting JSON before doing anything else.

Check two fields first:

- `data_gaps` - everything that could not be resolved. Each entry is a claim
  you are **not** entitled to make.
- `scores.composite.coverage` - below 1.0 means components were dropped and the
  weights renormalised. Say so in the note.

If the command fails, report the failure. Do not proceed with a partial picture
and do not fill the hole from memory.

### 2. Run the five analysts in parallel

Dispatch these subagents concurrently in a single message, passing each the
snapshot path:

| Agent | Reads | Produces |
|---|---|---|
| `technical-analyst` | `technical` | trend, momentum, levels |
| `fundamental-analyst` | `edgar`, `provider_fundamentals` | margins, growth, balance sheet |
| `sentiment-scanner` | web search only | catalysts and narrative, **no numeric score** |
| `risk-analyst` | `technical`, `edgar`, `shares` | volatility, drawdown, concentration |
| `thesis-synthesizer` | the other four | bull case, bear case, what would falsify each |

`sentiment-scanner` is the only one permitted to introduce facts from outside
the snapshot, and every one of them must carry a URL.

### 3. Write the note

Sections, in order:

1. **What the data says** - price, date, composite score with its coverage
2. **Technical state** - levels quoted with the swing dates behind them
3. **Filed financials** - with the fiscal period end and accession number
4. **Narrative and catalysts** - sourced, qualitative, no invented numbers
5. **Risk** - what breaks, and the measured volatility and drawdown
6. **Bull case / bear case** - both, at equal length, each with a falsifier
7. **What this analysis cannot tell you** - the standing limits, below

Write to `reports/TICKER.report.md`.

### 4. Verify before showing the user

```bash
python tools/verify.py reports/TICKER.report.md reports/TICKER.snapshot.json --strict
```

Every flagged number must be removed or given an inline source, then re-run.
**Do not present the note to the user while the verifier still flags figures.**
If you cannot source a number, delete the sentence. Report the final coverage
figure alongside the note.

## Standing limits to state in every note

- Sell-side price targets are optimistic on average and lag price.
- Valuation scores use absolute multiples, not sector-relative ones, so
  fast-growing companies score poorly and declining ones score well.
- The composite has been backtested and **shows no reliable forward-return
  edge** (see `reports/backtest_*.json`). It describes measurable current
  state. It does not predict.
- This is research, not advice, and it does not size positions or place trades.

## What this skill will not do

It does not produce entry prices, stop losses, profit targets or position
sizes. A specific entry-and-stop recommendation is trade construction whatever
disclaimer is attached to it, and the composite behind it has no measured
predictive power. Support and resistance are reported as observed swing pivots
with their dates, which is a description of where price has turned before - not
an instruction about where to buy.
