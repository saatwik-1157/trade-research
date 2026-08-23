---
name: pattern-study
description: Test candlestick patterns, calendar effects and price cycles against forward returns, with multiple-testing correction and date-clustered inference. Use when asked whether chart patterns work, or to analyse patterns and cycles.
---

# Pattern and cycle event study

```bash
python tools/patterns.py --universe tools/universe.txt --years 8 --cycles
python tools/patterns.py --tickers "EURUSD=X,GBPUSD=X" --years 10
```

Detecting a pattern is trivial. Whether anything follows it is the only
question that matters, and this measures that.

## Three corrections, all load-bearing

**Multiple testing.** 23 patterns times 3 horizons is 69 hypotheses; about 3.5
are expected to clear p<0.05 with nothing happening. Benjamini-Hochberg FDR
control is applied and both verdicts are printed, so the gap between "naive
significant" and "survives FDR" is visible.

**Date clustering.** Pooling correlated names treats 35 tickers signalling on
one day as 35 independent observations. They are closer to one. Inference is
clustered on date; the naive t is printed beside it to show the inflation.

**Sign flips.** Two means are reported: pooled (equal weight per observation)
and per-date (equal weight per day). When they disagree in sign the row is
marked `SIGN FLIP`, and the apparent edge lives in a few crowded dates - one
market-wide move counted many times - not in the pattern.

## Reporting

Quote the FDR line, not the top of the table. A row marked `naive only` did not
survive correction and must not be described as working. If nothing survives,
say nothing survived — that is the result, not a failure of the run.

Spectral peaks are reported with their share of variance because a random walk
produces peaks too. A peak explaining ~1% of variance is not a cycle.

Costs are not modelled. Single-day effects of a few basis points are inside the
spread for most retail execution.
