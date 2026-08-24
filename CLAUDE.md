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

## Before quoting any mt5_paper trading rule

`rsi_reversion` and `sma_cross` have been measured on 20,000 H1 bars across 7
FX majors and neither separates from a coin flip (`reports/rule_backtest.json`).
At this broker's real spreads both are net negative: −1.50 and −6.54 points per
trade. The only result in the whole exercise that reaches significance is that
the `random` rule loses, at t = −3.60 — cost drag is the one effect here large
enough to measure.

Cost the rule before believing it. Breakeven at SL=TP=1.5×ATR needs a
50.5–52.7% win rate depending on the pair (`reports/cost_hurdle.json`);
`rsi_reversion` averages about a point of win rate *below* that. An edge the
size of the scatter between these rules would take roughly 6,600 trades — some
four years at the observed rate — to demonstrate at 80% power, so "no edge" and
"an edge too small to see here" are not separable with the data available. Say
that, rather than picking whichever reading suits.

Spread comes from `--spread-source median`, the recorded per-bar spread across
the history. A single live quote can understate this broker by 3–8×, and bars
recording 0 are unrecorded rather than free — averaging them in halves the
apparent cost of trading.
A 36-cell sweep of SL/TP bracket ratios found no configuration reaching even an
uncorrected t of 1.96 — best in-sample 0.83 for `rsi_reversion` and 1.18 for
`sma_cross`, both negative out of sample — while the `random` rule searching the
identical grid scored higher in-sample at 1.76. Over 30 of the 36 cells are
negative in-sample for each rule (`reports/bracket_sweep.json`,
`reports/bracket_sweep_sma.json`).

The only effect that replicates out of sample is negative: tight brackets trade
often and pay the spread every time. Frequency is the one lever with a proven
sign, and it points down - which is the reason not to wire these rules to an
interval and leave them running. Describe a rule's behaviour; never present one
as an entry signal, and never tune the grid until a cell looks profitable.

## Environment

`SEC_USER_AGENT` should carry a contact string. Requests still succeed
without it - the SEC throttles unidentified traffic rather than refusing it -
so an unset variable shows up as slowness under load, not as an error.
Work inside a virtualenv; see the README on the `requests` import warning.

Run `python tests/test_indicators.py` after touching `tools/indicators.py`,
`python tests/test_verify.py` after touching `tools/verify.py`,
`python tests/test_cache.py` after touching the cache logic in
`tools/market.py`, `python tests/test_edgar.py` after touching period
alignment in `tools/edgar.py`, and `python tests/test_rule_backtest.py` after
touching `tools/rule_backtest.py`, `tools/bracket_sweep.py`, or the order
construction or history windows in `tools/mt5_paper.py` and
`tools/mt5_account.py`. All five run in CI on every push, against Python
3.10, 3.12 and 3.14.

The MT5 server clock is not the local clock. Bound a history query with
`mt5_paper.history_end()` and `server_now()`, never `datetime.now()` — a local
upper bound drops every deal the server stamped later, returns no error, and
reads as "no trades" instead of a fault.

The disk cache sweeps entries older than 7 days on first write of each
process. `python tools/market.py --prune` runs it on demand.
