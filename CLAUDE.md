# trade-research

Stock research tooling where numbers are computed, not generated.

## The one rule

**Python computes. The model interprets. The model never introduces a figure.**

Every indicator, ratio and level comes from `tools/`. If you find yourself about
to write a price level, an RSI reading or a margin that you did not read out of
a snapshot JSON, stop — that is the exact failure this project exists to
prevent. A generated indicator is indistinguishable from a real one by eye,
which is why the constraint is absolute rather than a preference.

## Workflow

```bash
python tools/snapshot.py TICKER --out reports/TICKER.snapshot.json   # build data
python tools/verify.py reports/TICKER.report.md reports/TICKER.snapshot.json --strict
```

Never show a written note to the user while `verify.py --strict` still flags
figures. Source them inline or delete the sentence.

Use the `trade-analyze` skill for the full pipeline.

## Conventions

- A field that could not be computed is `null` and is listed in `data_gaps`.
  Report the gap; never fill it from memory.
- SEC EDGAR beats the vendor profile wherever both have a figure.
- Check `edgar.period_alignment` before quoting any ratio. Ratios built from
  mismatched fiscal periods are the known trap here.
- A dropped score component is reported via `coverage`, never defaulted to 50.
- No entry prices, stop losses, profit targets or position sizes — that is
  trade construction whatever disclaimer is attached, and the composite has no
  measured predictive power.
- Sentiment is prose with source URLs, never a number.

## Before quoting the composite score

It has been backtested and shows **no reliable forward-return edge** (mean IC
≈0.00–0.03, t < 1, negative quintile spreads; see `reports/backtest_*.json`).
Present it as a description of current state, never as a prediction.

## Environment

`SEC_USER_AGENT` must be set to a contact string or the SEC will throttle.

Run `python tests/test_indicators.py` after touching `tools/indicators.py`,
and `python tests/test_verify.py` after touching `tools/verify.py`. Both run
in CI on every push.
