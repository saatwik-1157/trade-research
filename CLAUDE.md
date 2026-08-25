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

A 41-candidate search across 10 rule families (RSI, MA crosses, Donchian,
Bollinger, momentum, and the inverse of each) found nothing either
(`reports/rule_search.json`). Best in-sample t was 1.37, negative out of sample,
below what a permutation null reached over the same 41 candidates. Zero cleared
1.96 out of sample against ~1 expected by chance, and every family's median
out-of-sample expectancy is negative.

That search fixes the exit at SL=TP=1.5xATR, which caps winners and so handicaps
the trend-following families by construction. The supportable claim is "nothing
works at this exit, and the mean-reversion families the exit suits still lost" -
not that breakouts do not work. Sweeping brackets across all candidates would be
1,476 configurations, which correction would swallow.

The same 41 candidates were then run at H4 and D1, and 25 entries were crossed
with 8 exit structures - fixed brackets, ATR trailing, time-based and
move-to-breakeven - so the trend families got exits that do not cap a winner
(`reports/rule_search_h4.json`, `reports/rule_search_d1.json`,
`reports/exit_search.json`). Nothing survived. At H4 and D1 the permutation null
outscored the best real candidate (2.89 vs 1.42, and 2.77 vs 2.23). The exit
search produced the only candidate ever to beat its null in-sample, at t=3.10,
and it went to -1.83 out of sample; three combinations cleared 1.96 out of
sample against 4.9 expected by chance.

One out-of-sample split is not enough. The `time_120` exit showed +26 median
out-of-sample expectancy across 24 unrelated entries and survived a permutation
check (+23.0 real against -0.9 shuffled), which looked like a finding. Split
into four sequential blocks it fell apart: real was worse than shuffled in the
first block, identical in the second (+13.1 against +13.5, so that quarter was
market drift), better in the last two. A 120-bar hold has enough variance that
the shuffled series alone ranges from -35 to +13.5 - which is why large
points-per-trade numbers sit next to t-stats near zero. Check stability across
several blocks before believing any exit that holds positions a long time.

Cost is the lever that does move (`reports/cost_profile.json`). Measured over a
common window, H4 costs 0.41x and D1 0.27x of the H1 spread hurdle, because ATR
grows with bar length far faster than spread does. Hour of day barely matters on
this broker: only the rollover hour stands out, at 8 points against 3. Lower
cost does not create an edge, and it trades against verification - D1 accrues
trades roughly 24x slower, so a smaller hurdle comes with a much longer wait for
evidence.

The nearest thing to a survivor was `donchian_fade_55` at D1 — fade a 55-day
breakout — and it is worth knowing why it is not one, because it will surface
again. Over ten years it shows +227 points per trade out of sample on a pooled
t of 2.79, and it is the only candidate a sequential split has ever marked
`holds_out_of_sample`. Three checks dissolve it. Cut into five two-year eras it
runs −107.8, −0.8, +180.7, +93.4, +283.4: positive in three, and the two losing
eras are the two oldest. Clustering by entry date — one shared dollar move
opens trades in all seven pairs at once — takes the out-of-sample t from 2.79
to 1.96 against a 2.042 threshold, and takes the in-sample t from +0.53 to
−0.60. And the search that found it does not survive being walked forward
across those eras either: re-ranked on only the eras before each test era it
picks `mom_24`, then `mom_168`, then `sma_5_20` twice, and is profitable in 1
fold of 4 — +331.8 in era 4 against −42.4, −29.8 and −53.0, so the +51.6 mean
is one era wearing a mean's clothing.

Sequential splits cannot settle this on their own, because every one of them
puts the same recent era in the holdout. Splits at 0.5, 0.7 and 0.85 all
called `donchian_fade_55` positive out of sample; that is one observation
counted three times, not three confirmations. Use `--blocks N` and read the
walk-forward before believing any candidate the split likes.

Report the date-clustered t, not the pooled one. The by-symbol figure in the
same output is a consistency check and moves the other way — when all seven
pairs agree it comes out *higher* than pooled (5.6 against 2.79 here), which is
what one shared move looks like rather than evidence against one. Check
`signs_disagree` before quoting either: the two means can point opposite ways
when the losses land on a few crowded dates, and at H1 the best candidate does
exactly that — pooled expectancy negative, per-date mean positive.

Use `rule_search.py`'s permutation null rather than a plain coin flip when
judging a new rule: shuffling a candidate's own signals preserves its trade
count and buy/sell mix, so the null pays the same spread and the comparison
isolates timing rather than exposure. The era blocks, the walk-forward and the
date clustering are on by default; `--blocks 0` turns the era work off, which
is worth doing only when a timeframe has too little history to cut.

Two universes outside the seven majors have now been searched, and neither
helps. Eight non-USD crosses (`reports/rule_search_crosses.json`) matter because
they cannot be carried by a shared dollar leg, which is the correlation that
inflated the D1 result — best in-sample t was 0.20, and the walk-forward went 0
for 3 with a −42.4 mean. Four metals (`reports/rule_search_metals.json`) produced
the largest headline in the whole project and it is an arithmetic error, not a
finding: pooled out-of-sample expectancy +4236 points, date-clustered t 3.16,
`holds_out_of_sample` true. Per symbol it is XPTUSD +6129 and XPDUSD +1595
against XAGUSD −223 (t = −2.28, significantly negative) and XAUUSD −5, on an
out-of-sample win rate of 50.4%.

The reason is units. "Points" is price movement over the symbol's own point
size, and median H1 ATR runs 160 points in silver against 9,386 in
palladium — a 59x difference, so pooling adds numbers that are not the same
quantity. The seven majors span 2.15x and pool acceptably; anything wider does
not. `rule_search.py` now measures this and writes a `data_gaps` entry plus
`median_atr_points_by_symbol` when the spread exceeds 5x. When that warning is
present, quote `per_symbol` and never the pooled figure.

The hour filter was the one idea with a measured mechanism behind it, and it
also fails. `cost_profile.py` puts the rollover hour at 4x the normal spread, so
`--skip-hours 0` should have helped; across the 41 candidates it moved median
out-of-sample expectancy from −6.64 to −7.40 and improved 18 of 41, which is a
coin flip. The reason is arithmetic: the rollover hour carries 1.0% of entries,
so avoiding a 4x spread on one trade in a hundred cannot pay for itself. A cost
lever has to move a large share of trades to matter, and hour-of-day does not.

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
