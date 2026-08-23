---
name: trade-backtest
description: Measure whether the composite score has actually separated forward returns historically, reporting information coefficient, quintile spreads and robustness checks. Use before trusting any score, or when asked whether the scoring works.
---

# Backtest the score

```bash
python tools/backtest.py --universe tools/universe.txt --years 6 --horizon 63
```

Scores every ticker at each month end using only bars available on that date,
then measures the forward return over the horizon.

## Reading the output

- **Mean IC** - rank correlation between score and forward return. Real quant
  factors run 0.02-0.05. Below about 0.02 is noise.
- **IC t-stat** - overlapping windows make this optimistic. Below 2 is not
  significant even before that caveat.
- **Quintile spread** - Q5 minus Q1. If this disagrees in sign with the IC, the
  two measures are describing noise, not signal.
- **Robustness checks** - significance, sign agreement, monotonicity. Fewer
  than two of three passing means no reliable edge, whatever the IC says.

## What is being tested

Only the price-derived components (technical, risk). Quality, valuation and
analyst inputs come from a vendor snapshot of *today's* figures; scoring a past
date with them would leak the future backwards and produce an excellent,
meaningless result. They are excluded and the exclusion is reported.

Survivorship bias, transaction costs and regime dependence are **not**
corrected for. Read the `caveats` block in the JSON output and repeat it
whenever you quote a result.

## Reporting the result

Quote the verdict verbatim. If it says no reliable edge, say so plainly rather
than reaching for the most flattering horizon - testing several and reporting
the best one is how a null result gets dressed up as a finding.
