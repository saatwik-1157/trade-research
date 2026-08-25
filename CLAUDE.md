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
(`reports/rule_search.json`). Best in-sample t was 1.29 and went negative out of
sample. It does clear the permutation null in sample (0.74 was the null's best
over the same 41 candidates), which is a reminder that beating the null is the
weakest of the three gates - it says the ranking is not pure exposure, not that
the rule works. Zero cleared 1.96 out of sample against ~1 expected by chance,
and every family's median out-of-sample expectancy is negative - all ten of
them.

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

Those two searches left one cell empty, and it was the cell the argument
pointed at: the exit search ran at H1 only, so no run had ever let a winner
run at the timeframe where `cost_profile.py` puts the spread hurdle lowest.
`exit_search.py` now takes `--timeframe`, and 16 trend entries crossed with the
8 exits at D1 close it (`reports/exit_search_d1.json`). It is the sharpest null
in the project. `donchian_brk_100` with a move-to-breakeven trail scores an
in-sample t of 6.59 - the highest figure this repository has produced, clearing
both the 3.546 Bonferroni threshold and a permutation null that reached 5.5 -
and then posts -1.82 out of sample at -204 points. Zero of 128 combinations
cleared 1.96 out of sample against 3.2 expected by chance, so the search came in
*below* chance, and median out-of-sample expectancy is -117.

Read the exits rather than the winner, because they refute the mechanism that
motivated the run. If fixed brackets were handicapping the trend families by
capping winners, the winner-letting exits should improve on them at D1. They do
the opposite: median out-of-sample expectancy is -58 for the 1.5x1.5 bracket
against -123 for the breakeven trail, -217 for the 3.0 ATR trail and -231 for
the 120-bar hold. All eight are negative. The capping bracket is the least bad
of them, which is the reverse of the prediction, and it retires "the exit was
holding the breakouts back" as an explanation - a wider exit at D1 mostly buys
more adverse excursion per trade.

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

Three universes outside the seven majors have now been searched, and none
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
quantity. The seven majors span 2.16x and pool acceptably; anything wider does
not. `rule_search.py` now measures this and writes a `data_gaps` entry plus
`median_atr_points_by_symbol` when the spread exceeds 5x. When that warning is
present, quote `per_symbol` and never the pooled figure.

Seven equity indices at D1 are the third universe, and they were worth trying
for a measured reason rather than a hunch: the spread hurdle there is the
lowest on this broker. At SL=TP=1.5xATR, US500 breaks even at a 50.27% win rate
and USTEC at 50.11%, against 50.84% for EURUSD H1 and 52.02% for EURUSD D1 - a
3x to 8x smaller edge requirement, on the one lever this project has shown has
a sign. It bought nothing (`reports/rule_search_indices.json`). The best
candidate, `ema_5_20`, scored an in-sample t of 2.14 against a permutation null
that reached 2.45, so the shuffled rule beat the real one; out of sample it is
1.15 pooled and 1.19 date-clustered, one candidate cleared 1.96 against 0.9
expected by chance, and the walk-forward went 2 of 4 with a -933 mean. Take
this as the confirmation of "lower cost does not create an edge" rather than as
one more null - the hypothesis was specific and it was refuted on its own terms.

The unit warning fires here too, at 38x (JPN225=535 points against DE40=20213),
so the +3239 pooled out-of-sample expectancy is the metals error repeated and
must not be quoted. Per symbol it is DE40 +7345 and US500 +2250 against UK100
-439 and US2000 -447, three of seven positive. One thing is new: the trend
families led a universe for the first time, `ema_5_20`, `mom_72` and `sma_5_20`
taking the top places where every previous search was led by mean reversion.
That is consistent with indices trending more than FX, and it is a curiosity
rather than a finding, because it does not clear the null.

Index figures are optimistic by an unmodeled amount, and the gap is overnight
financing. `rule_search.py` costs the spread and nothing else, but six of the
seven indices charge swap on BOTH sides - DE40 -0.70 long and -1.12 short,
AUS200 -6.83 and -0.42, HK50 -5.37 and -4.63 - and the best candidate holds 6.1
bars, so roughly six nights per trade with one weekday billed triple. The two
symbols carrying the result, DE40 and US500, are both negative carry either
way, so correcting for it moves the result down and never up. Worse, the swap
is quoted in three different unit systems across these symbols, which is
structurally the same defect the points warning exists to catch.

`swap.py` converts it (`reports/swap_profile.json`), and what it refuses to
convert is the useful part. Per night, in points: EURUSD -0.82/-1.17,
GBPUSD -0.27/-3.00, US500 -31/-26, DE40 -60/-96, and every FX major bills
triple on Wednesday against Friday for the indices. Two symbols return no
figure at all. XAUUSD converts to 584,000 points a night, 26,603% of its
daily range, because reading a metal's swap as ounces of gold and converting
at the gold price inflates it by roughly the price of gold; JPN225 comes out
at 57% of its daily range. Both are unit errors rather than expensive
instruments, so the tool reports a gap instead of a number - a fence set at
10% of median daily range, on the reasoning that financing which rivals the
daily range would dominate every other term in a trade.

Note what that costs the earlier text: the +294.231 figure quoted for JPN225
above is the raw broker field, not a points-per-night charge, and it should
not be read as one.

`simulate()` now charges financing when asked, and `rule_search.py --cost-swap`
turns it on. It is OFF by default deliberately: every figure in this file was
measured without it, and a default that silently restated them would make the
history unreadable. A symbol whose swap unit `swap.py` cannot convert is
DROPPED from a financed run with a data gap, never charged zero - zero is a
claim that sleeping is free, and it is the claim this whole section exists to
refute.

Measured at D1 over ten years on the majors (`reports/rule_search_d1_swap.json`
against `reports/rule_search_d1_walk.json`), financing costs a median 11.02
points per trade and it moves all 41 candidates the same way - 41 worse, 0
better, which is what a cost that is negative on both sides has to look like.
The best candidate `sma_5_20` goes from +14.34 to +6.14 out of sample, its t
from 0.20 to 0.09, and its walk-forward mean from +51.6 to +40.5.

It does not change a single verdict, and saying so matters more than the
correction. `donchian_fade_55` goes from +227.5 to +207.3 and stays positive,
so financing is NOT the reason that result is not a survivor - the era split,
the date clustering and the walk-forward are, and they were already enough.
An 11-point haircut on a D1 trade is real and worth charging, but it is an
order of magnitude too small to be the thing standing between this repository
and an edge. Nothing here was ever one cost adjustment away from working.

The H4 exit search is worth reading as a worked example of why the era check is
on by default (`reports/exit_search_h4.json`). At H4 the 25x8 grid does not
look like the others: median out-of-sample expectancy is +3.25 rather than
negative, 108 of 200 combinations are positive, and `bracket_2.0_4.0` is
positive in 20 of 25 entries at a +23.5 median. Five combinations cleared 1.96
out of sample - against 4.9 expected by chance - and none of them was pickable,
every one having a negative or flat in-sample t (`rsi_rev_14_20_80` runs -0.23
in sample and +2.59 out).

Cut into four eras it is not an exit effect at all. Every one of the eight
exits is positive in era 2 and most are negative in eras 1 and 3 - the era
medians for `trail_3.0`, the worst exit in the whole grid at -27.6 overall, run
+12.3, +45.8, -38.6, -0.1, so the exit that loses most also scores the HIGHEST
of any exit in era 2. An effect that appears in every exit structure at once,
including the ones that do not work, is the market moving, not a rule. Only 5
of 25 entries are positive in all four eras under `bracket_2.0_4.0`, and 11 are
positive in two or fewer.

That is the `time_120` diagnosis replicating on a different exit at a different
timeframe: a positive median across many unrelated entries is what a directional
era looks like from inside a grid search, and the exits that let a winner run
capture it best, which is why they lead the table exactly when drift is
present. Date clustering does not rescue it - three of the top five stay
significant, but they were selected from 200 combinations against 4.9 expected
by chance, so the clustered t is being read on a pre-selected winner and
confirms nothing. `exit_search.py` now takes `--blocks` and threads entry times
through, so this cut is available rather than reconstructible.

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
`tools/mt5_account.py`, and `python tests/test_swap.py` after touching the
unit conversion, the night count or the plausibility fence in `tools/swap.py`.
All six run in CI on every push, against Python 3.10, 3.12 and 3.14.

The MT5 server clock is not the local clock. Bound a history query with
`mt5_paper.history_end()` and `server_now()`, never `datetime.now()` — a local
upper bound drops every deal the server stamped later, returns no error, and
reads as "no trades" instead of a fault.

The disk cache sweeps entries older than 7 days on first write of each
process. `python tools/market.py --prune` runs it on demand.
