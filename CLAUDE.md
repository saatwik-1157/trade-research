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
sample. **It does NOT clear the permutation null, and the earlier claim here
that it did was an artefact of sampling the null three times.** Corrected
2026-09-18: at `--null-rounds 20` the null's best is 1.00 and the candidate
scores 0.98 (`reports/rule_search_nullcheck.json`). The two 3-round runs on
file put the null at 0.42 and at 1.40 — same code, windows three weeks apart —
and the first of those is what produced the original `beats_permutation_null:
True`. See the note in `rule_search.py`'s docstring; the default is now 10.

This makes the result stronger rather than weaker. The 41-candidate search
now fails ALL THREE gates rather than two of three, and the one time anything
in this project ever passed a gate, it turned out to be noise in the gate.
Zero cleared 1.96 out of sample against ~1 expected by chance,
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
sample against 4.9 expected by chance. **That null was also measured at 3
rounds**, so "beat its null" there carries the same caveat as the 41-candidate
result above and has not been re-checked at 20. It failed out of sample either
way, which is why it is not worth re-running to find out.

Re-run 2026-09-12 on the current toolkit, and the verdict reproduces on numbers
of its own: H4 best 1.65 against a null reaching 1.98, D1 best 2.18 against a
null reaching 2.87. Both still fail all three gates. A replication matters here
because a single search that finds nothing can always be dismissed as one
unlucky draw; two, on different code and a different window, cannot. Note D1
judged only 31 of 41 candidates -- the rest had too few trades -- so its
"nothing" is thinner evidence than H1's, and its null reaching 2.87 is the
sample size talking rather than the market.

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

Four universes outside the seven majors have now been searched, and none
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

Crypto is the fifth universe and the first that is not this broker's data at
all (`reports/rule_search_crypto.json`, seven Binance spot pairs, 3,000 daily
bars from 2018, taker fee 10bps charged both ends via `tools/crypto_market.py`).
It was chosen because no shared dollar leg can carry it and the venues never
close, so there is no session structure to fit by accident.

The unit choice is the part worth keeping. Measured in quote units these
symbols differ by 232,485x - BTC near six figures beside DOGE near 0.09 - which
is the metals error several orders of magnitude worse. Measured in PERCENT of
each symbol's median price they differ by 1.6x, tighter than the majors' 2.16x,
and the 5x fence correctly stops firing. So the pooled figures here are
quotable, unlike the metals and indices runs, because the unit was fixed before
the search rather than explained after it.

The verdict is the same as everywhere else. `ema_20_50` scores 2.93 in sample
against a 3.22 Bonferroni threshold and a permutation null that reached 2.56,
then goes to -0.69 out of sample at -1.31%, and -0.83 date-clustered. One
candidate cleared 1.96 out of sample against 1.0 expected by chance. Median
out-of-sample expectancy across candidates is -0.15%.

What is new is the shape of the failure. The five era blocks run +8.36, +2.62,
+1.18, +0.80, -0.28 percent - a monotonic decay to negative, not the ragged
in-and-out of the FX results. Trend following in crypto worked in 2018-2020 and
has decayed era by era to nothing since. That is what either an arbitraged-away
effect or a fitted early regime looks like, and the two are not separable here;
what matters either way is that the most recent era is the negative one. The
walk-forward agrees and declines the same way: +4.47, +0.64, -2.21, -0.28.

Note also which families led. As at the indices, the trend families took the
top places - `ema_20_50`, `sma_20_50`, `ema_10_50` - where the FX searches were
led by mean reversion. Two universes now agree that trend is the right family
for markets that actually trend, and both still fail to clear their nulls.

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

Candle shape and volatility regime are the sixth search, and they were run
because they are the only families the L23 feature catalogue offers that the
other five never covered. Five of its 22 features reconstruct rules already
searched — `rsi_14` is the RSI family, `ema_spread_10_50` is the EMA cross
before it is thresholded, `high_20_distance`/`low_20_distance` are the Donchian
break, `sma_distance_20` with `volatility_20` rebuilds the Bollinger band, and
`roc_10` is momentum at a shorter lookback. What was left untried is the shape
of the bar itself and the ratio of short volatility to long.

`tools/shape_search.py` tests 28 such candidates through `rule_search`'s own
pipeline — same permutation null, same Bonferroni threshold, same era blocks,
walk-forward and date clustering, same measured spread — over 19,851 H1 bars per
symbol across the seven majors, 2023-06-26 to 2026-09-04
(`reports/shape_search.json`). It is the cleanest null the project has produced.

The best candidate, `body_fade_0.8`, scores an in-sample t of **-0.04**. It never
looked good even before correction, which none of the previous winners can say.
The permutation null over the same 28 candidates averaged **0.37**, so shuffling
these rules' own signals produced better timing than the rules had. Zero of 28
cleared 1.96 out of sample against 0.7 expected by chance, median out-of-sample
expectancy is **-5.90 points**, and 2 of 28 are positive.

Every downstream gate agrees. Date clustering takes the best out-of-sample t to
-1.43; by symbol it is -3.06 with **0 of 7 pairs positive**. Cut into four eras
the best candidate is positive in one (-1.79, +4.42, -3.27, -9.33). The
walk-forward is 0 of 3 folds profitable at a mean of -20.05, and its selection
step is worth reading: it picked `vol_expand_ride_1.3` twice on training t-stats
of 2.95 and 0.81, and that candidate then lost -23.56 and -27.27 in the eras it
had not seen. A 2.95 that decays to a loss in the next block is what selection
noise looks like from inside a walk-forward.

The one significant reading is negative and is not about shape. `body_ride_0.6`
loses at **t = -4.65** over 4,884 out-of-sample trades, and its mirror
`body_fade_0.6` is +1.89 at t = 0.61. A rule and its inverse cannot both be
skill; what separates them is that one trades into the spread more often than
the other. That is the same result the `random` rule gave at t = -3.60 — cost
drag is once again the only effect in the exercise large enough to measure.

Read this as closing the feature catalogue rather than as one more null. The
families the platform can compute from H1 OHLC have now all been searched, and
none of them separates from its own shuffle. Two remain untested and both carry
their own caveat: MT5 supplies TICK volume, a count of quote updates rather than
traded size, and hour-of-day already has a measured negative reading from the
`--skip-hours 0` run. A model trained on these features would be searching this
same space with more parameters, which is the condition under which the 36-cell
bracket sweep scored the RANDOM rule at 1.76 against the best real candidate's
0.83.

Supertrend is the seventh search and the first that came from outside this
project. Three public trading repositories were read for a strategy worth
measuring — `TraderAlice/OpenAlice`, `HKUDS/AI-Trader` and
`studiogangster/next-gen-algo-trading-bot`. Two contain no testable rule at
all: OpenAlice is an orchestrator that delegates research to agents, and
AI-Trader's `research/` is an A/B experiment about how 5,289 LLM agents POST
on a social copy-trading platform, with no broker adapter anywhere. The third
ships exactly one: `strategies/supertrend_rsi.py`, buy when Supertrend flips
bullish and RSI > 50, sell when it flips bearish and RSI < 50, at
Supertrend(10, 3.0) and RSI(14).

It earned a run because it is not a rename. MACD is an EMA cross, a breakout
is Donchian, a moving-average trend filter is an MA cross — Supertrend is none
of those, because of its RATCHET: the band only ever moves toward price and
never away, so the flip level carries state from every bar since the last
flip. Donchian is a pure function of the last n bars and Bollinger of the last
n closes; nothing searched here had that property.

`tools/supertrend_search.py` runs 14 candidates through `rule_search`'s own
pipeline — same permutation null, same Bonferroni threshold, same era blocks,
walk-forward and date clustering, same measured spread — over 19,851 H1 bars
per symbol across the seven majors, 2023-07-07 to 2026-09-18
(`reports/supertrend_search_h1.json`).

**The strategy loses, and significantly.** `st_rsi_10_3`, the source rule as
written, scores an in-sample t of **-2.27** (-2.28 date-clustered against a
2.042 threshold) and an out-of-sample t of **-4.07** (-2.95 date-clustered).
Net expectancy runs -10.56 points in sample and -25.1 out. It is negative in
**all seven pairs**, at per-symbol t-stats from -1.37 to -2.95, and positive in
**one of four eras**. Per-symbol win rates run 44-48% against the 50.5-52.7%
breakeven `cost_hurdle.py` measures — it is below the hurdle before any t-stat
is consulted.

The gross/net split is the useful part and it is the same finding as
everywhere else. `supertrend_10_3` is -5.36 points gross and -10.51 net, so
the spread costs about 5.15 points a trade. Its INVERSE is +1.40 gross and
-3.75 net — the same 5.15. Flipping the rule turns the sign of the timing and
changes nothing about the cost, which is why the inverses cluster just under
break-even rather than mirroring the winners. Out of sample the four inverted
candidates score +8 to +11 points at t-stats of 1.65 to 1.85, none clearing
1.96 and all collapsing to 0.42-1.37 under date clustering.

Nothing clears any gate. Best in-sample t across all 14 is **-0.50**, and the
permutation null over the same 14 averaged **-0.14** — the shuffle timed these
rules better than the rules did, which is what `shape_search` also found. Zero
of 14 cleared 1.96 out of sample against 0.4 expected by chance. The
walk-forward is 1 of 3 folds positive and its selection step is the familiar
one: it picked on training t-stats of 0.95, -0.24 and -0.43, and the 0.95 pick
lost -31.85 points at t = -4.11 in the block it had not seen.

Read this as closing the outside-contribution question rather than as a
seventh null. The families this platform can compute have now been searched,
and a public repository with a working strategy in it was searched too. Its
strategy is significantly negative on this venue's data, before costs and
worse after them.

Volume is the eighth search and the last untested input. Every family above
is a pure function of OHLC, and this file recorded volume as untested with
MT5's limitation as the reason -- `real_volume` is 0 on spot FX and
`tick_volume` counts quote updates rather than size. That reasoning is right
about MT5 and was never true of crypto, where a ccxt exchange reports size
actually traded and `crypto_market.py` had been downloading it in column 5
and discarding it since the universe was added.

`tools/volume_search.py` tests a conviction-weighted return: today's return
times how unusual today's volume is against its own recent baseline, so a 1%
move on triple volume and a 3% move on a third of it score the same. Six
candidates over the same seven Binance pairs and 3,000 D1 bars the crypto
search used, 2018-07-03 to 2026-09-18 (`reports/volume_search_d1.json`).

**Two of the six are unweighted controls, and they are the point.** The
return leg alone is momentum at n=1, which is already refuted, so the novelty
is strictly the weighting and a run without the control could not attribute a
result to it. The trigger is a trailing quantile rather than the source's
absolute cut, because an absolute cut makes the two arms fire a different
number of times and frequency is the one lever here with a measured sign. It
works: 1,324 control trades against 1,411 weighted at q=0.10.

**The weighting makes it worse, in all four pairwise comparisons.**

| q | control | volume-weighted (n=5) | volume-weighted (n=20) |
|---|---|---|---|
| 0.10 | **+0.50** | -0.01 | +0.26 |
| 0.20 | **-0.34** | -0.83 | -1.18 |

Out-of-sample expectancy in points. At both quantiles the arm that ignores
volume beats both arms that use it. Nothing clears any gate either: best
in-sample t across the six is **0.14**, the permutation null over the same
six averaged **1.57** — the shuffle beat the real rules by more than a full
t-stat — and zero cleared 1.96 out of sample against 0.2 expected by chance.
Era blocks are 2 of 4 positive for every candidate, which is a coin flip, and
the walk-forward is 1 of 3 at a -0.40 mean, selecting on train t-stats of
0.57, 0.10 and 0.50.

`signs_disagree` is true for the best candidate, so its pooled and per-date
means point opposite ways — one more reason not to read anything into it.

The result worth keeping is not "volume fails". It is that **the control beat
the treatment**, which is only visible because the control was in the run.
Six of the seven previous searches had no equivalent, and their nulls are
weaker for it: they establish that a family did not work, not that the thing
being added subtracted.

Both the supertrend search and the 41-candidate baseline were re-run on
2026-09-19 against 19,855 H1 bars per pair to 2026-09-18, and both reproduce
on numbers of their own.

`st_rsi_10_3`, the rule taken from `studiogangster/next-gen-algo-trading-bot`,
scores an in-sample t of **-2.18** and an out-of-sample **-4.24** (-3.18
date-clustered) at -26.07 points a trade, against -2.27 / -4.07 / -2.95 and
-25.1 on the first run. Best in-sample across the 14 is -0.58, and the
permutation null over the same 14 averaged **+0.10** - the shuffle timed
these rules better than the rules did, for the second time. The walk-forward
selected on a training t of 0.94 and then lost -30.91 points at t = -3.94 in
the block it had not seen.

The 41-candidate baseline still fails all three gates. Best in-sample is 0.98
(`boll_fade_50_2.0`) against a 3.234 Bonferroni threshold and a permutation
null averaging **0.91** - within noise of the real thing - and **zero of 41**
cleared 1.96 out of sample against 1.0 expected by chance. The walk-forward is
1 of 3.

That best candidate is also a live example of the `signs_disagree` warning
below: its pooled out-of-sample t is **-0.34** while its date-clustered t is
**+3.9**, because the losses land on crowded dates. `rule_search.py` reports
`best_survives_date_clustering: false` rather than quoting the +3.9, which is
the behaviour to expect and the reason to check the flag before quoting
either number.

A replication matters here for the reason it did at H4 and D1: a single
search that finds nothing can be dismissed as one unlucky draw, and two on
different windows cannot.

Carry is the ninth search and the only one whose candidate is not a rule.
Every family above is a pattern fitted to price and judged against a shuffle
of itself. Overnight financing is a TERM OF THE CONTRACT: the broker publishes
it, it is known before any trade, and it does not have to clear a permutation
null because there is no timing in it to shuffle. On this account three long
sides are PAID rather than charged -- AUDUSD +4.49 points a night, NZDUSD
+1.60, USDCHF +0.08 -- against a charge on every other side of every other
symbol (`reports/swap_profile.json`, `reports/carry_check.json`).

That is worth taking seriously for a reason none of the eight rule searches
could claim: **it is the one candidate here that pays you for not trading.**
The live loop pays the spread on all 1,382 opens a night; a carry position
pays it once and earns every night after. At AUDUSD's rate the spread repays
in **4.2 nights**.

**It still does not clear the bar, and the arithmetic is worth keeping
because it is a different failure from the other eight.** AUDUSD carry is
1,638 points a year, 2.30% at current price. Net of realised spot drift over
five windows it runs +4,211 (3y), +1,353 (5y), +1,467 (10y), **-817 (15y)**
and +2,205 (25y) points a year. The carry term is a constant -- it is today's
contract -- so every one of those differences is the spot term, and the spot
term moves by 5,028 points a year depending only on where the history is cut.
The first version of `carry_check.py` judged one window, returned
CARRY_EXCEEDS_DRIFT at ten years, and would have been confidently wrong at
fifteen.

The rebuttal to that is correct and has to be answered rather than waved at:
if spot is a martingale then the drift term has expectation zero, its
variation across windows is sampling noise rather than evidence, and a
contractual carry is positive expected value however the history is cut. The
answer is a calculation. At 2.30% a year against 10.14% annual volatility the
ratio is **0.227**, so a t-statistic growing as Sharpe*sqrt(T) needs
**74.6 years** to reach 1.96. One standard error of the ten-year drift
estimate is 3.20% a year, and the carry is **0.72** of it -- the noise in the
thing that could eat the carry is larger than the carry, and stays larger
until about twenty years. Worst peak-to-trough on the pair is **-48%**.

So the honest statement is not "carry does not work". It is that the carry is
real, is positive in expectation if spot is unforecastable, and is **too small
relative to the volatility it rides on to be demonstrated within a working
lifetime** -- which is the same shape as `cost_hurdle.py`'s 4,300 trades and
the 6,600 in the composite note, in the units a hold rather than a trade is
measured in. A -48% drawdown collecting 2.3% a year is the carry risk premium
behaving exactly as the literature describes: payment for crash risk, not an
inefficiency.

Two cautions for anyone re-running this. **The swap rate is a snapshot and
the history is not.** MT5 reports what the broker charges today; the AUD-USD
policy differential has changed sign inside every window above, so the carry
column does NOT describe what a position would have earned over that history
and `carry_check.py` refuses to multiply the two into an equity curve. And
**the conversion is the trap it is everywhere else in this repository**: the
first version of the tool divided the raw swap by `trade_tick_value` and
skipped the base-to-deposit step, reporting AUDUSD at 6.3 points a night
against the true 4.49 -- a 40% overstatement of the only positive number on
the account, in the direction that manufactures an edge. It now calls
`swap.swap_points_per_night`, which was already correct, rather than
restating the arithmetic.

The cross-section is the tenth search and the first that is not a
time-series rule at all. Every family above judges each symbol on its own
history and pools the seven results, and that construction carries the
confound this file keeps correcting for: the majors share a dollar leg, one
dollar move opens correlated trades in all of them, and `clustered_by_date`
exists entirely to undo the double counting. It has dissolved the two best
results the project ever produced.

`tools/cross_search.py` removes the leg BY CONSTRUCTION instead. Rank the
seven foreign currencies against the dollar, go long the strongest n and
short the weakest n, and a pure dollar move lifts every leg together and
cancels -- measured exactly, not approximately: a synthetic common move
returns 0.0 to the last bit. Three things change with it. The observation
unit becomes one portfolio return per bar, so there is **no date clustering
left to undo** and the t-statistic is the honest one rather than a pooled
figure awaiting correction. The null has to change too: shuffling through
TIME would leave the cross-section intact, so the null permutes weights
ACROSS SYMBOLS at each rebalance, holding leg count, gross exposure and
turnover fixed while destroying which currency is strongest. And cost is
charged on **turnover** -- `sum(|w_new - w_old| * spread)` -- because a leg
held through a rebalance is free.

**It finds nothing, and the null is a well-behaved one.** At D1 over 2,618
bars and ten years, the best of 24 candidates is `rev_k60_n2_h20` at an
in-sample t of **1.27** against a 3.078 Bonferroni threshold and a shuffle
that reached **2.58** -- the shuffle beat the real thing, for the fourth time
in this project. Out of sample it is 0.50. **Zero candidates** cleared 1.96
out of sample against 1.2 expected by chance, and median out-of-sample Sharpe
across the 24 is **-0.076**.

H1 over three years is the one that needed a second look. `rev_k24_n2_h120`
scores an in-sample **2.47** and CLEARS its permutation null at 1.90 -- and
the gap of 0.57 is barely outside the 0.5 fence this file sets for re-running
an under-sampled null. Re-run at 25 rounds the null rises to **2.16** and the
margin falls to 0.31, inside the fence, which is the documented behaviour
rather than a surprise. It fails Bonferroni either way, posts **0.56** out of
sample, and again zero of 24 cleared against 1.2 expected.

One pattern is consistent enough to note and not strong enough to trade: at
both timeframes **every** leading candidate is `rev` rather than `mom` --
short-horizon relative-currency reversal, the opposite of the trend families
that led the indices and crypto searches. Twelve of twelve at H1. It is a
curiosity on the same footing as those, because it does not clear a gate.

**The engine was verified to have power before its null was believed**, which
none of the earlier searches in this file can say. A planted persistent
currency is found at t = **+11.16** by momentum and -11.16 by reversal, so a
real cross-sectional effect of that size would not have been missed. Pure
noise returns 0.49.

The defect caught on the way is worth more than the result. A cross-sectional
signal is a cumulative past return sliced out of a cumsum, and an off-by-one
there does not break anything visibly -- it manufactures an edge. Introduced
deliberately, contaminating only 1/k of the signal, it scored an in-sample
**2.47 and CLEARED BOTH Bonferroni and the permutation null**; only the
out-of-sample gate caught it. Worse, `test_cross_search.py` did not catch it
either, because the test RESTATED the signal construction instead of
importing it and so never executed the line that was wrong. `momentum_signal`
is now a function in the tool, the test imports it, and the check is
algebraic -- a single planted return must be invisible at the bar it is acted
on and visible on the next -- rather than a t-statistic hoping to notice.
Reverting the off-by-one now turns three checks red.

"Do not close at the deadline, hold until it is in profit" was asked for
on 2026-09-23 and measured rather than argued about. It is the worst exit of
the ten now tested (`reports/exit_search_holdprofit.json`, D1, 25 entries x
10 exits, 249 combinations judged).

| exit | median out-of-sample expectancy | win rate |
|---|---|---|
| bracket_2.0_4.0 | -8.7 | 33.2% |
| **bracket_1.5_1.5** (what the harness runs) | **-51.2** | 49.2% |
| time_120 | -216.6 | 28.4% |
| **hold_for_profit** | **-272.7** | 6.4% |
| **hold_for_profit_nostop** | **-727.4** | 0.0% |

Two mechanisms produce that, and both are worth keeping because neither is
obvious from the description.

**Closing at the first profit closes at BREAKEVEN.** The moment the
favourable excursion covers the spread is the moment the exit fires, so the
winner captures the spread and nothing more. Measured on a random walk with
149 signals: `hold_for_profit` exited 111 trades on "profit" at a mean net of
**-0.000** and was stopped out on the other 38 at up to -1.09. The bracket on
the same path ran 80 stops against 69 targets. Removing the upside while
keeping the downside is why it scores worse than the exit it replaces: -0.278
a trade against -0.103.

**Refusing to close does not remove a loss, it hides it in the open book.**
With the stop removed, the same 149 signals produced only **46 closed
trades**, every one exiting at exactly -0.000, for a mean of -0.000 and a
worst trade of -0.000. A perfect record - and the other 103 positions were
still open, carrying their losses unrealised. On the real D1 data, where
unclosed positions are marked to market at the end rather than dropped, the
same exit scores **-727.4**. The two readings are the same fact seen with and
without the open book counted, which is exactly why a live account cannot use
the flattering one.

**And with a stop in place the request is not actionable at all.** `sl` and
`tp` are transmitted with the order as absolute GTC levels, so the venue
closes the position at -1R whether or not this program agrees. `simulate_exit`
shows it: on a dip that later recovers, `bracket_1.5_1.5` and
`hold_for_profit` exit at the SAME bar for the SAME loss. Only
`sl_atr=None` - deleting the stop - changes the outcome, and that is the
-727.4 row.

Financing compounds it. Every major bills swap on both sides here, triple on
Wednesday, so a position held waiting for a recovery pays -0.27 to -10.59
points a night (`reports/swap_profile.json`) for the privilege.

`tests/test_exit_search.py` pins all of this, including the counter-intuitive
part: `profit_exit` must return ~zero net, because an edit that let it
capture upside would erase the comparison. One caution found writing those
tests - the intrabar band decides the outcome. At an 0.0005 band on a price
of 100 the wiggle is 0.05 against an 0.02 spread, so `profit_exit` fires on
the next bar's high before the price can reach the stop. That is realistic,
and it means in live trading this exit fires on noise within a bar or two,
capturing nothing, far more often than it waits for any recovery.

The calendar is the eleventh search, and it came from the bookshelf rather
than from this repository. Seven trading books in `trade books/` were read and
mapped against what has been tested; the reference work among them is
Kaufman's `Trading Systems and Methods`, whose 24 chapters cover trend
systems, momentum and oscillators, charting, pattern recognition, volume and
risk control -- all of which this project has already searched and refuted.
The one prominent chapter with no counterpart here was **seasonality and
calendar patterns**. `patterns.py` does day-of-week and month-of-year with FDR
control, but it pulls equity tickers through yfinance and has never seen a
currency pair.

A calendar rule is the cleanest test available, which is the reason to run it:
day-of-week has five buckets and NO free parameters, so unlike every other
family here a null result cannot be answered with "maybe another setting
works", and the correction is honest rather than a floor on a larger hidden
search.

**Two defects in the tool had to be fixed before its first result meant
anything, and both flattered it.** The first version reported a Friday effect
of +0.0306% against Tuesday's +0.0123%, clearing its permutation null at
p = 0.0002 and stable in all four eras -- and it was mostly an artefact.

  * **The Friday-to-Monday return spans THREE calendar days** while every
    other return spans one. Three times the exposure is a larger mean by
    arithmetic. Per day that bucket is **+0.0101%** against Tuesday's
    +0.0123% -- lower, not higher.
  * **It was labelled with the day the return STARTS from.** The convention
    names a return for the bar it ENDS on, so the Friday-to-Monday move is
    the MONDAY observation. The first version had relabelled the classic
    weekend effect as a Friday effect.

Corrected (`reports/calendar_search_d1.json`, D1, seven majors, 10 years,
18,333 returns over 2,620 dates):

| bucket | days | mean % | per day | t by date |
|---|---|---|---|---|
| Mon (Fri->Mon) | 3.0 | +0.0303 | +0.0101 | 4.21 |
| Tue | 1.0 | +0.0077 | +0.0079 | 0.95 |
| Wed | 1.0 | +0.0120 | +0.0124 | 1.41 |
| **Thu** | 1.0 | -0.0186 | **-0.0183** | **-2.28** |
| **Fri** | 1.0 | -0.0221 | **-0.0222** | **-2.58** |

**This is the first candidate in this project's history to survive the era
check**, which is where donchian_fade_55, the H4 exit grid and every other
near-survivor died. Thursday and Friday are negative in all four era blocks
and in both out-of-sample halves; the permutation null over shuffled calendar
labels gives p = 0.0002 against a shuffled mean spread of 0.0202% (month
p = 0.88 and turn-of-month p = 0.75, so the correction across the three kinds
does not touch it). Per symbol, Friday is negative in **6 of 7** pairs and
Thursday in 5 of 7, concentrated in AUDUSD, NZDUSD, USDCHF and GBPUSD with
EURUSD flat.

It is NOT an edge yet and must not be described as one. What has not been
done: a walk-forward, a simulation through `simulate()` with the real bracket
and measured spread, and any out-of-sample period this tool has not already
seen. The effect is -0.0222% a day against a round-trip cost of **0.0031%**,
so it clears cost by about 7x on paper -- which is exactly the shape that has
dissolved here before once it was traded rather than tabulated. The direction
is also a basket of pairs quoted inconsistently (four with USD as the counter
currency, three with USD as the base), so "short the basket on Friday" is not
yet a position anyone can take; `cross_search.py`'s foreign-currency
normalisation is the fix and has not been applied here.

Read the sign pattern before believing the mechanism. Friday is negative in
AUD, NZD, GBP and in USDCHF and USDJPY -- that is high-beta currencies falling
and CHF/JPY strengthening, which is a risk-off Friday rather than a dollar
effect, since a dollar move would show opposite signs on the two quoting
conventions. That is a coherent story and a story is not evidence.

**The calendar effect does not survive being traded, and the CONTROLS are
what proved it** (`reports/calendar_rule_d1.json`). Tabulating returns by
weekday is not a strategy; `calendar_rule_search.py` runs the same days
through `rule_search`'s pipeline -- entered at the next bar's open, bracketed
at 1.5xATR, charged the measured spread, judged by the permutation null,
Bonferroni, era blocks, walk-forward and date clustering.

Twelve candidates: the measured days, their inverses, and Tuesday and
Wednesday as controls, chosen because they showed NO effect in the table
(t 0.95 and 1.41). Out-of-sample expectancy in points:

| candidate | out-of-sample expectancy | out t |
|---|---|---|
| cal_long_mon | **+165.1** | 3.31 |
| cal_long_fri | +86.9 | 1.75 |
| **cal_long_thu** | +86.6 | 1.73 |
| **cal_long_tue** (CONTROL) | **+86.0** | 1.72 |
| **cal_long_wed** (CONTROL) | **+83.2** | 1.69 |
| cal_short_tue (CONTROL) | -100.6 | -2.01 |
| cal_short_mon | -175.3 | -3.52 |

**The controls score the same as the tested days.** Tuesday +86.0 against
Thursday +86.6 and Friday +86.9; every long is near +86 at an out-of-sample t
near 1.7, and every short is its mirror. That is not a calendar effect -- it
is that going long ANYTHING in the out-of-sample window earned about 86
points, which is directional drift seen from inside a grid. It is the
`time_120` and H4-exit diagnosis for the third time: a positive median across
unrelated variants is what a directional era looks like, not what an edge
looks like.

The one outlier is Monday, at +165 long and -175 short, and that is the
weekend again: the Monday bar carries three calendar days of exposure against
every other bar's one, so it moves about twice as far in whichever direction
the window went. The same artefact that inflated the table survived into the
trade simulation, which is worth knowing -- `simulate()` does not normalise
for bar span either.

It fails the gates. Best in-sample is 2.36 against a 2.865 Bonferroni
threshold, it does NOT beat its permutation null, and the only candidate that
holds out of sample is the three-day-exposure Monday. So the eleventh search
ends where the other ten did, with one difference worth recording: **it is the
first whose table cleared the era check and whose TRADE did not.** The gap
between those two is the whole reason this repository simulates rather than
tabulates.

**Do not train a model on these features.** The point is now measured rather
than argued: a model handed day-of-week plus the L23 catalogue would be fed
Tuesday and Thursday columns that score identically, and would fit the drift
with more parameters and less ability to notice. That is the condition under
which the 36-cell bracket sweep scored the RANDOM rule at 1.76 against the
best real candidate's 0.83.

## Before building an execution or cost model

The argument for modelling execution rather than direction is sound: cost is
the one quantity this repository has measured to significance repeatedly,
it has a known sign, it is a property of the broker rather than a bet against
anyone, and direction has now failed eleven searches. So it was scoped on
2026-09-23. **The scope came back empty, and the numbers are worth keeping so
nobody scopes it again.**

**Size the prize before building the model.** Measured over 935 live trades
carrying a recorded fill:

| target | addressable | verdict |
|---|---|---|
| slippage | **0.00018R per trade** | 0.6% of the spread cost |
| refusals | 181 of 2,393 attempts (7.6%) | none are market prediction |
| spread timing by hour | rollover is 1.0% of entries | already refuted on cost grounds |
| symbol choice on measured cost | **0.0153R per trade** | needs NO model |

Slippage is nonzero on 13.4% of trades and averages **0.00018R**, against a
measured spread cost of 0.0299R and an observed loss of 0.0235R. A model that
predicted slippage PERFECTLY and avoided all of it would recover 0.8% of the
loss. The worst single instance is 0.041R, so even the tail is small.

The refusals decompose, and not one is a modelling problem:

  * **10031 no connection, 85 of 181.** Host and network, not the market. It
    is the same failure family as the 41-minute Modern Standby sleep, and the
    watchdog and `preflight.py` are the controls.
  * **10018 market closed, 83.** Pure calendar. Knowable exactly from the
    venue's week; `deadline_in_the_weekend` already encodes it. A model here
    would be a lookup table with error bars.
  * **10016 invalid stops, 10.** All inside the single minute 21:00-21:01 UTC,
    which is server rollover -- documented above and fixable with a one-line
    filter rather than a classifier.

So the only lever with real size is **which symbols are traded**, and it
needs no learning at all: `cost_hurdle.py` already measures the spread, and
trading the three cheapest pairs moves the drag from 0.0299R to 0.0146R. That
is arithmetic on numbers already computed, and it still leaves expectancy at
-0.0082R -- better, and still negative.

**The general lesson, which is the reusable part.** The case for an execution
model was made from a true premise -- cost dominates this record -- and the
premise does not imply the conclusion. Cost dominating does not mean cost is
VARIABLE enough to be worth predicting. Here it is almost entirely the posted
spread, which is known before the trade and needs no model, and the part that
varies is 0.6% of it. Size the addressable quantity before building anything
to predict it.

**The symbol set changed on 2026-09-23 and the record is not one sample
across it.** `run_overnight.SETTINGS` now trades **USDJPY, EURUSD, GBPUSD**
only; every figure above, up to 1,003 trades, was measured on all seven
majors. `--max-positions` went 7 to 3 with it, since one position per symbol
means three symbols can hold at most three, and money at risk per pass falls
from 35 USD to 15.

The change is defensible for exactly one reason and it is worth stating
precisely, because the obvious reason is the wrong one. It is NOT that those
three performed best -- the per-symbol ranking was measured
**indistinguishable from shuffled labels at p = 0.62**, so live performance
carries no information about which pair to trade. It is that their SPREAD is
lower, measured by `cost_hurdle.py` independently of any outcome:

    kept     USDJPY 0.0096R   EURUSD 0.0168R   GBPUSD 0.0174R
    dropped  USDCAD 0.0224R   AUDUSD 0.0384R   USDCHF 0.0502R   NZDUSD 0.0548R

That moves the drag from 0.0299R a trade to 0.0146R, about 65% of the
measured loss, and the saving persists because it is a property of the broker
rather than of these trades.

**It is still negative: -0.0082R a trade expected, against -0.0235R
observed.** It makes a loser lose more slowly. Nothing in the record from
here should be read as an edge appearing, and a quieter loss is the predicted
outcome rather than an improvement in the strategy.

**Quote the two periods separately.** `track_record.py` raises a data gap
when bracket regimes are mixed, because a trade's size is set by its stop
distance; the symbol set is the same class of problem one level up. A pooled
figure spanning the change adds a seven-pair sample to a three-pair one, and
the cost per trade differs by 2x between them by construction.

## What the trading books on this shelf do and do not contain

Seven books in `trade books/`, 4,519,514 characters, read and counted on
2026-09-23. The reference work among them is Kaufman's `Trading Systems and
Methods` (1,232 pages, 24 chapters). The strategy content is mapped in the
calendar section above -- it is ground this repository has already searched.
**The methodological content is the part worth recording, because of what is
missing from it.**

Term counts across all seven:

| term | count | | term | count |
|---|---|---|---|---|
| robust | 116 | | **overfit / over-fit** | **0** |
| optimi(se/sation) | 110 | | **statistically significant** | **0** |
| out-of-sample | 41 | | sample size | 3 |
| in-sample | 28 | | walk-forward | 2 |
| risk of ruin | 26 | | curve-fit | 6 |
| martingale | 59 | | degrees of freedom | 6 |

**They say "optimise" 110 times and never once say "overfit".** Four and a
half thousand pages of methods, with essentially nothing on how to tell
whether a method worked. That gap is exactly the failure mode that has killed
every candidate here: `donchian_fade_55` at a pooled t of 2.79, the D1 exit
grid at an in-sample 6.59, the H4 exit grid's +23.5 median, and the calendar
effect at p = 0.0002. Every one of those is a good result by the books'
standard and dissolved under a permutation null, era blocks, date clustering
or a control.

So the machinery in this repository is not a local convention to be trimmed
when it is inconvenient. It is the part the literature on this shelf does not
supply, and it has now overturned four of its own findings.

**Kaufman warns against the position sizing this project was asked for.** On
the Trident system, which increases size after each loss, he writes that the
concept "can result in ruin", and gives it a chapter section, *Martingales
and Anti-Martingales*. On averaging down -- adding to positions making new
losses -- chapter 23 does not recommend it; it RUNS THE TEST. That is the
same idea as "do not close at the deadline, hold until it is in profit",
which `exit_search` measured as the worst of ten exits at -272.7 with a stop
and -727.4 without. The reference work and the measurement agree.

**What the books supply that is already in use here.** Position sizing off
the stop, stated in the beginners' guide exactly as `lot_for_risk` implements
it -- decide the stop first and the size follows, never the reverse -- with
1-2% of balance per trade. Transaction costs (73 mentions) and slippage (67).
Correlation (184) and diversification (105). Risk of ruin as a closed-form
expression rather than a rule of thumb.

Read them for the catalogue of what to try. Do not read them for whether it
worked.

## Before training a model on the L23 dataset

The pipeline is sound: 119 tests pass across `app/datasets` and
`app/training`, the leakage checks are behavioural rather than structural
(feed the pipeline future bars and require the output not to change), and
`LabelConfig` has no zero-cost default -- `spread_points` must be stated,
because a WIN label computed without the spread is a label for a market
nobody trades in. None of that is the problem.

**The dataset is too small to demonstrate the edge a model would have to
find, by a factor of fourteen.** Measured 2026-09-23:

| | |
|---|---|
| rows in the READY dataset | **638** |
| features in the L23 catalogue | 22 -> **29 rows per feature** |
| mean breakeven win rate (`cost_hurdle`, 7 pairs) | 51.50% |
| edge a model must BEAT | **1.50 win-rate points** |
| standard error of a win rate at n=638 | **1.98 points** |
| so the edge is | **0.76 standard errors** -- significance needs 1.96 |
| rows needed at 80% power | **8,744** |
| shortfall | **13.7x** |

**The target is smaller than the noise floor.** A model that was PERFECT --
that found the whole 1.5-point edge the spread demands -- could not show it
on 638 rows, because one standard error of a win rate at that sample size is
1.98 points. Training here does not produce a weak result; it produces a
result that cannot be read either way, and a fit over 22 features on 29 rows
each will report a number regardless.

So the sequence is the dataset, then the model, and not the reverse. The bars
to fix it exist -- `rule_search` routinely runs 19,851 H1 bars per symbol
across seven pairs -- so 638 is a property of how the dataset was built
rather than of what is available.

**And when it is large enough, do not hand it the whole feature catalogue.**
`shape_search.py` documents that 5 of the 22 features reconstruct families
already refuted: `rsi_14` is the RSI family, `ema_spread_10_50` is the EMA
cross before thresholding, `high_20_distance`/`low_20_distance` are the
Donchian break, `sma_distance_20` with `volatility_20` rebuilds the Bollinger
band, and `roc_10` is momentum. Handing a model all 22 searches dead ground
and new ground at once, and no result could be attributed to either. The
calendar search made the same point empirically on 2026-09-23: a Tuesday
control scored +86.0 against Thursday's +86.6, so a model given day-of-week
columns would fit that drift with more parameters and less ability to notice.

## The first trained models, and what they show

The dataset problem is fixed: `market_bars` went 705 -> 35,700 and seven
READY datasets of 4,926 rows each replaced the 638-row fixture, so the
1.50-point spread hurdle is no longer inside the noise. Models were then
trained -- `trade_probability`, 22 features, all gates passed
(`dataset_is_ready`, `leakage_report_passed`, `series_quality_usable`).

**No model clears the hurdle, and the control overlaps the result.**

| dataset | accuracy | majority | margin | shuffled-label control |
|---|---|---|---|---|
| eurusd_h1_v1 | 51.42% | 50.10% | **+1.32pt** | -0.51pt |
| gbpusd_h1_v1 | 51.62% | 51.22% | +0.41pt | **+0.61pt** |
| usdjpy_h1_v1 | 51.93% | 52.64% | **-0.71pt** | -4.56pt |

The margin is accuracy over the majority-class prior, which is the only part
that could be skill: `metrics.py` says in its own docstring that a dataset
70% WIN gives 70% accuracy to a model that always says WIN. The best margin
is **+1.32 against a 1.50 requirement**, one of three is negative, and on
GBPUSD the SHUFFLED control (+0.61) beat the real model (+0.41). Three real
margins spanning -0.71 to +1.32 sit inside a control spanning -4.56 to +0.61.

**The shuffle control was a flag wired to nothing in the first version.** It
was declared in the help text, passed into `run()`, and never consulted, so
the output would have said CONTROL while training on real labels. It now
wraps the loader and permutes the labels, preserving class balance, row
count, features and fitting procedure exactly. That it works is visible in
the trade counts changing between paired runs (EURUSD 241 -> 23).

**DO NOT QUOTE THE `economic` BLOCK. It is degenerate.** Across all six runs
-- three real, three shuffled, trade counts from 23 to 669 -- `gross_profit`
is **exactly 0** and `win_rate` is **0.0**, making `profit_factor` 0.0 and
`expected_value` simply minus the mean loss. Zero winning trades out of 669
is not a result, it is a bug, and the figure is excluded here rather than
reported as devastating evidence. It needs fixing before any economic claim
is made from a training run.

So the position is unchanged and now measured on the model side too: 22
features over 4,926 rows per symbol, leakage-checked, cost-charged labels,
and the best margin is below the spread hurdle with a control that reaches
the same range. Eleven rule searches and a first pass at models agree.

## The economic metric bug, and the model that cleared three gates and died at the fourth

**The `economic` block reporting "profit factor 0.0, win rate 0.0" across 669
trades was not a devastating result. It was a units bug, and it was mine.**
`LabelConfig.spread_points` is subtracted straight from a PRICE in
`compute_labels` (`net_exit = closes[end] - spread`), so the value it wants is
points x point size -- EURUSD's two points is **0.00002, not 2.0**. Passing
2.0 gives `1.17 - 2.0 = -0.83` and a forward return of **-1.71 on every row**.
Measured: 4,926 rows, **zero positive**, mean -1.720.

The field name is what made it invisible. Every existing caller passes a
price under a name that says points, which is numerically harmless and makes
the name a lie; a caller who reads the name is the one who gets hurt. The
arithmetic in `labels.py` was correct all along.

`LabelConfig` now carries a plausibility fence on `swap.py`'s reasoning:
nothing here has a spread near a tenth of its own price, so a value above 0.1
is refused with a message naming the likely cause rather than calling the
value invalid. It caught a SECOND occurrence in the same script immediately,
and it retroactively refuses to rebuild the seven poisoned `_v1` datasets --
which are now demoted to CLEAN with the reason attached, so they cannot be
trained on. Corrected, the labels are 45.6% / 47.9% / 62.4% positive with
ranges of +-1-3%.

**Then USDCAD cleared three gates and died at the fourth.**

| gate | result |
|---|---|
| single chronological split | **+7.40pt** margin, 58.82% against a 51.42% prior, **CLEARS** the 1.50 hurdle |
| non-degenerate | tp 286, tn 294, fp 213, fn 193 -- predicts positive 48.6% of the time |
| permutation null (shuffled labels) | real +7.40 against a control of **-0.81**; the control's best across all eight datasets is +0.81 |
| Bonferroni over 8 datasets | +7.40 is **4.65 standard errors** at n=986; clears |
| **walk-forward, 4 folds** | **-9.95, +1.02, -3.35, +5.68 -- mean -1.65, 2 of 4 positive** (corrected; see the scaling note below) |

It is the first candidate in this project's history to survive a permutation
control, and it still died, in the same place `donchian_fade_55` and both
exit grids died. Across all eight datasets the walk-forward gives **10
positive folds of 31 against the shuffled control's 4**, and a mean margin of
**-1.61 against the control's -1.72** -- indistinguishable. Two datasets have
a positive mean (+1.31 on the 638-row fixture, +0.99 on USDJPY) and neither
reaches the 1.50 hurdle.

**The training service does not run this gate, and says so itself.** Its
comparison block reads *"this is a comparison on a held-out segment of one
dataset. It is not the L26 gate: no permutation null, no correction for the
number of candidates tried, and no walk-forward,"* and its gate report adds
*"Read the walk-forward folds before believing a result."* Neither the folds
nor the null were computed anywhere. `tools/walk_forward_models.py` and
`train_models.py --shuffle-labels` supply both, and the first thing they did
was retire the one result that looked like an edge.

**THE `+0.00` FOLDS WERE MY BUG, NOT A PROPERTY OF THE DATA, AND THE
CORRECTION IS THE MOST INSTRUCTIVE THING HERE.** The first walk-forward
reported 0 of 4 folds and a model emitting a single constant class in every
fold. I checked that per fold, confirmed the constant class, and recorded it
as "on a walk-forward this model makes no prediction". The observation was
true. The conclusion drawn from it was wrong.

`training/service.py` SCALES before vectorising (`scaled = scaler.transform(
rows)`), and my walk-forward vectorised the RAW features. A logistic over
unscaled FX features saturates: measured on the first fold, every training
probability came out at exactly **1.0000**, spread **0.0000**, with
non-trivial weights (max +1.86) and a bias of -0.31. The fitter was not
failing to find signal; it was never given a chance to look.

Corrected -- a scaler fitted on each fold's own training range, since
`scaler.fit` takes a Split precisely so that "fit on everything" has no
spelling -- the model varies its predictions and the numbers move a lot:

| | positive folds of 31 | mean margin |
|---|---|---|
| raw features (WRONG) | 1 | -0.74 on USDCAD |
| scaled, real labels | **10** | **-1.61** |
| scaled, shuffled control | 4 | -1.72 |

**The verdict is unchanged and the evidence for it was not.** No dataset
clears 1.50 on a walk-forward mean, and the real runs sit on top of their own
control. But "0 of 4, the model never varies" was an artefact I had verified
the symptom of and not the cause. Check that a fitter was given scaled inputs
before concluding anything from what it did or did not learn.

**Excluding the refuted features makes the single split look better and the
walk-forward worse**, which is the clearest statement of the gap between the
two gates this project has produced.

`train_models.py --exclude-refuted` drops the seven columns that reconstruct
families already searched -- `rsi_14`, `ema_spread_10_50`, the two Donchian
distances, `sma_distance_20`, `roc_10`, and `day_of_week`, which the calendar
search measured as drift when a Tuesday control scored +86.0 against
Thursday's +86.6. That leaves 15 features.

| | single split | walk-forward mean (scaled) |
|---|---|---|
| 22 features, USDCAD | **+7.40pt CLEARS** | -1.65, 2 of 4 |
| 15 features, USDCAD | **+7.71pt CLEARS** | **-3.78, 2 of 4** |
| 15 features, EURUSD | **+2.23pt CLEARS** | -0.10, 2 of 4 |

Dropping the refuted columns still raises the single-split margins and still
does not rescue the walk-forward. Across the eight datasets the 15-feature
version gives **13 positive folds of 31** against 10 at 22 features, but its
mean margin is **-1.67** against **-1.61** -- more folds scraping above zero,
no better on average, and no dataset reaching the 1.50 hurdle.

Read that as a warning about the single split rather than about the features.
A smaller feature set fits the held-out segment better and generalises across
eras worse, which is what overfitting looks like from the outside: the check
the training service performs improves, and the check it does not perform
degrades. **Anyone reading only the number the platform reports would
conclude the opposite of the truth.**

## Inside the bar: a boundary bug that produced the best candidate yet

Twenty-fifth search. The input catalogue is closed at H1, but that is a
statement about RESOLUTION rather than data. Two bars with identical open,
high, low and close can have travelled completely different distances getting
there, and nothing above can see it -- `shape_search` measured body and wick
from OHLC, and `path_order_search` asked only which extreme came first. M1
bars expose the path.

**Power stated before the result, as it has to be.** 50,000 M1 bars is 48
days, about 833 H1 bars a pair and 5,824 pooled. One standard error of a win
rate there is **0.66 points**, so the 1.50-point spread hurdle would show at
**t = 2.3**. Adequate for an effect of the size that matters, useless for a
subtle one, and 48 days is far too short to call any split an era.

**The premise was checked before the search and it demoted the lead
candidate.** Each feature was regressed on the OHLC-visible properties to ask
whether it is new at all:

| feature | R2 from OHLC | kept |
|---|---|---|
| **path efficiency** | **0.809** | **no -- mostly a restatement of body and range** |
| path / range | 0.234 | yes |
| open crossings | 0.147 | yes |
| fraction above mid | 0.023 | yes |

Net-move-over-travel is 81% explained by the bar's own body and range, which
makes it close to ground `shape_search` already refuted. Reported and dropped
rather than counted as a fourth family. The control throughout is
RESIDUALISATION: only the part of each feature that OHLC cannot explain is
tested, because testing the raw feature would rediscover candle shape and call
it new.

**THE FIRST RUN PRODUCED THE MOST CONSISTENT CANDIDATE THIS PROJECT HAS EVER
SEEN, AND IT WAS A BOUNDARY BUG.**

    open crossings   residual t = +1.98   permutation null p = 4.5%
                     7 of 7 symbols positive

Seven of seven is better consistency than `donchian_fade_55`, the calendar
effect or DeMark managed. It beat its own permutation null. It failed
Bonferroni over three features (needing p < 0.0167) and its spread was a fifth
of the cost, so it was never tradable -- but it looked like structure.

It was not. The crossing count was `diff(sign(close - open)) != 0`, and when a
close sits EXACTLY on the open the sign passes through zero on the way in and
again on the way out, so a bar that merely TOUCHED its open scored two
crossings it never made. Counting only genuine above-to-below transitions:

| open crossings | with the bug | corrected |
|---|---|---|
| residual t | **+1.98** | **+0.55** |
| permutation null p | **4.5%** | **56.9%** |
| symbols positive | **7 of 7** | 5 of 7 |

Per symbol the collapse is uniform -- EURUSD +1.99 to +0.70, AUDUSD +1.17 to
+0.18, USDCAD +1.07 to +0.57 -- which is what an artefact shared by every pair
looks like when it is removed.

**What caught it was a trivial check**: *a monotone rise never crosses its
own open*. That is true by inspection, it takes one line, and it is the only
reason the search did not report a seven-of-seven candidate that beat its
null.

**Corrected, all three features are null.**

| feature | residual t | null p |
|---|---|---|
| path / range | +0.40 | 68.0% |
| fraction above mid | +0.53 | 60.9% |
| open crossings | +0.55 | 56.9% |

So the path a bar takes inside itself carries nothing beyond what its OHLC
already says -- measured on the only three path statistics that are not
themselves mostly OHLC.

**The reusable lesson is about where bugs hide.** The last several defects
here were in instruments rather than markets, and this one was at a BOUNDARY:
the exactly-equal case, which is rare enough to be invisible in a sanity check
and common enough to move a t-statistic from 0.55 to 1.98. Ties decided the
Pugh classification (1.855% of bars), the gross expectancy bound (0.94% of
trades), and now this. Three separate searches, three times the equality case
carried the result.

    python tools/intrabar_search.py

## Every model here was LINEAR, so interactions were never tested

Twenty-fourth search, and the last untested cell in the modelling work.
`train_models.py` and `pooled_walk_forward.py` both fit
`trainers.fit_weighted_logistic`, which is linear in the features. A logistic
model cannot represent *"feature A matters only when feature B is high"* --
not poorly, but at all. So if the only structure in this data were an
interaction, every model result above would have missed it **by construction
rather than by measurement**, which is the same kind of gap the Kaufman
replication closed when his 28-80 day range turned out never to have been in
the grid.

**The learner is written in the repository because it had to be.** sklearn,
lightgbm, xgboost and scipy are all absent from this environment. So
`tools/nonlinear_search.py` is a small gradient-boosted ensemble of
depth-limited trees in numpy -- depth 3, 60 rounds, learning rate 0.1, minimum
50 rows a leaf -- in the same spirit as this project writing its own Parabolic
SAR and efficiency ratio rather than taking them on trust.

**That is a risk, and the risk is a false NULL rather than a false signal.** A
learner written for one search, in a project where every previous search came
back empty, is most likely to report empty whether or not it works. So its
power is proven first, and proven on the one thing that justifies the search:

    XOR out of sample:   logistic 0.490   boosted 0.984
    coin-flip label:                      boosted 0.489
    plain linear signal:                  boosted 0.93

The XOR label depends on the SIGN AGREEMENT of two features and on neither
feature alone -- measured, each has a correlation under 0.05 with the label.
Logistic is provably at chance there and scores 0.490. The ensemble scores
0.984. It sees what every previous model in this file was blind to, and it
does not learn a coin flip.

**On the real data it finds nothing, and the control says why.**

| fold | boosted | logistic |
|---|---|---|
| 1 | +0.02 | -4.00 |
| 2 | -0.56 | -2.28 |
| 3 | -3.62 | -0.44 |
| 4 | -0.92 | -0.49 |
| 5 | -0.57 | +0.36 |
| **mean** | **-1.13** | **-1.37** |

Neither clears the 1.50-point hurdle and neither is positive. The logistic
column reproduces `pooled_walk_forward`'s tested result (-1.37 against -1.35),
which is the consistency check that makes the boosted column worth reading.

**The decisive figure is the control.** On SHUFFLED labels the boosted model
scores **-1.10**, against **-1.13** on the real ones:

| | real labels | shuffled control |
|---|---|---|
| boosted | **-1.13** | **-1.10** |
| logistic | -1.37 | -0.40 |

The nonlinear model performs **identically on real and random labels**. That
-1.13 is not a small edge being eaten by cost; it is the model's own noise
floor. It is not finding a little and losing it -- it is finding nothing, and
a learner that can recover an XOR at 0.984 would have found an interaction if
one were there.

**One test check was written wrongly and the correction matters.** It asserted
that an unscaled logistic "saturates to one class". It does not:
`predict_logistic` thresholds the score at zero, so weight magnitude never
collapses the prediction. The historical failure in this file was that the
FITTER could not learn from features at wildly different magnitudes -- every
training probability came out at exactly 1.0000 -- which is a different
statement. The check now fits both learners on features scaled by 1e6 and
1e-6 and requires the ensemble to recover the same accuracy, which is the
property actually being claimed.

**Read this as closing the modelling question rather than as one more null.**
Twenty-two searches said no single rule works. The pooled walk-forward said no
linear combination of the feature catalogue works, at four times the power
needed. This says no INTERACTION among those features works either, on a
learner demonstrated to find interactions. What remains untested is not a
model class but a different feature space, and the input catalogue is closed.

    python tools/nonlinear_search.py
    python tools/nonlinear_search.py --self-test
    python tools/nonlinear_search.py --shuffle-labels

## Tick volume on FX: the last untested input, and the clock inside it

Twenty-third search, and it closes a gap this file has carried from the
beginning. CLAUDE.md recorded tick volume as untested with the reason
attached -- *"MT5 supplies TICK volume, a count of quote updates rather than
traded size"* -- and `volume_search` tested real traded volume on Binance
crypto, never this. Measured here, **`real_volume` is all zero on this venue's
FX**, so tick volume is not a proxy for size; it is the only volume there is.

**Kaufman's warning about intraday volume decides the design, and it is
measured rather than taken on faith.** Across 50,000 H1 bars per pair, tick
volume by server hour runs **0.27x at hour 0 to 2.13x at hour 17 -- a 7.9x
range** -- and the hourly profile correlates **+0.959 across pairs**. A shape
that agrees that closely across three different instruments is a property of
the FX day, not of any market. So a rule comparing a bar to a rolling baseline
spanning hours measures the clock, and every bar here is instead expressed
against the SAME BAR-OF-DAY's own trailing history.

**The unnormalised arm is kept as the control**, because if the raw candidates
scored and the normalised ones did not, the finding would have been the clock.
Sixteen candidates from Kaufman's own tick-volume family, each with its
inverse: a spike against a LAGGED baseline, the Force Index in both its
mean-reverting and trend readings, Accumulation/Distribution, the Chaikin
line, the Money Flow Index and the Market Facilitation Index.

**Both arms are null and neither is close.**

| arm | best in | best out | null mean | cleared 1.96 OOS | date-clustered |
|---|---|---|---|---|---|
| normalised by bar-of-day | +0.35 | +0.44 | -0.15 | **0 of 16** (0.4 expected) | -0.69 |
| raw volume (control) | -0.81 | +0.36 | -0.38 | **0 of 16** | +0.16 |

Bonferroni is 2.955 and the best in-sample figure in either arm is +0.35. The
normalised arm's best is marginally above the raw arm's, which is the
direction Kaufman predicts, and both are so far from significance that the
comparison carries nothing. Era expectancies swing sign in both.

**A duplicate was found in the source, not in the data.** Kaufman lists
Chaikin's Volume Accumulator and Intraday Intensity as two indicators. Their
weights are `((C-L)/(H-L) - 0.5) * 2` and `((C-L) - (H-C)) / (H-L)`, and both
reduce to `(2C - H - L)/(H - L)`; over 10,000 random bars they agree to
**1.1e-16**. Carrying both would have put a hypothesis in the Bonferroni
denominator twice, so one is kept and the identity is recorded.
Accumulation/Distribution is genuinely distinct because it weights by
`(C - O)/(H - L)` and therefore uses the open.

**The defect worth keeping is the binding.** `rule_search` hands a candidate
o/h/l/c and nothing else, so volume must be looked up rather than passed. The
first version bound one module-level array at fetch time -- but `rule_search`
fetches EVERY symbol into a dict before running any candidate, so the binding
held only the last one, and `trim_to_years` then changed its length. The
length guard refused the mismatch, which is correct, and the result was that
**0 of 16 candidates produced a single trade**. Failing safe is right; failing
safe unnoticed is not, and what caught it was reading the trade count rather
than the verdict -- the verdict said only "no candidate produced enough trades
to judge", which reads like a data problem rather than a wiring one. Volume is
now keyed to its own close series, and the test requires that an unbound
series returns nan rather than another symbol's numbers.

**One test here was vacuous and is worth naming.** The pairing check was
written as `sorted(x) == sorted(x)`, which is true of any list and could never
fail. Replaced with a real assertion it immediately failed, because the
direction token sits mid-name on the parameterised families
(`vspike_ride_2.0`) and at the end on the rest (`ad_ride`) -- so the honest
version had to be position-agnostic. A check that cannot fail is worse than no
check, because it occupies the place where a real one would have gone.

**With this the input catalogue is closed.** Every field MT5 supplies for FX
-- open, high, low, close, tick volume, spread -- has now been searched, and
`real_volume` is measured empty. There is no untested input left on this
venue, only untested combinations of tested ones.

    python tools/tick_volume_search.py
    python tools/tick_volume_search.py --raw

## The +0.0064R gross does not survive measurement, and that closes the loop

The spread-timing result below leaves the account a whisker from positive, and
every bit of that distance is carried by an implied GROSS expectancy of
**+0.0064R**. That figure was never measured. It is a residual: the observed
-0.0235R a trade minus an assumed 0.0299R of drag. `trade_autopsy` already put
its t at **0.43**. It can be measured directly, and it does not survive.

**331,746 simulated random-entry bracketed trades** across seven majors,
1.5xATR both sides, no spread charged:

    gross R = -0.01284   SE 0.00174   t = -7.40
    95% CI  [-0.01624, -0.00944]

**The inferred +0.0064R is about eleven standard errors outside that
interval.** The gross is significantly NEGATIVE, and a symmetric bracket on
random entries is supposed to give exactly zero, so the cause is worth finding
rather than reporting.

**It is the tie rule, and the whole question lives inside 0.94% of trades.**
When one bar's range covers both the stop and the target, intrabar order is
unknown and `simulate` books the LOSS -- deliberately, as its docstring says,
because assuming the win is how a backtest flatters itself. Ties are only
**1,560 of 165,873 trades (0.94%)**, and they move the answer by more than the
whole effect being argued about:

| tie resolved as | gross R | t |
|---|---|---|
| LOSS (the conservative default) | **-0.01185** | -4.83 |
| WIN (the optimistic bound) | **+0.00696** | +2.83 |

**The +0.0064R the account's case rests on is almost exactly the optimistic
bound of +0.00696R.** So the inference that gross expectancy is positive is
equivalent to assuming every ambiguous bar resolved in the position's favour.
Under a neutral 50/50 resolution the figure is about **-0.0024R**, and under
the conservative one **-0.0119R**.

**What that does to the spread-timing conclusion.** Cutting the drag by 69%
still cuts the drag by 69% -- that measurement stands on its own and is
unaffected. But it was reported as leaving expectancy at -0.0012R, a whisker
from positive, and that whisker was made of the optimistic tie assumption.
With gross at its neutral estimate the filtered expectancy is about
**-0.0069R**, and with the conservative one **-0.0164R**. **Spread timing
cannot flip the sign, because there is no positive gross for it to uncover.**

Read this as the end of a chain rather than one more null. Twenty-two searches
said direction is unforecastable; the cost work then said the drag is
timeable; and this says the thing the timing was supposed to uncover is not
there. The account does not lose because it pays too much for a small edge. It
loses because there is no edge, and it also pays.

    python tools/gross_bound.py

## Spread timing: the first thing measured here that is actually usable

Twenty-second search, and the first aimed at COST rather than direction.

Twenty-one searches say direction is unforecastable on this venue. The ledger
says something different about cost, and says it with numbers: **-0.0235R a
trade observed against 0.0299R of measured drag**, so implied gross expectancy
is **+0.0064R** and the cost is what makes the result negative. Choosing the
three cheapest pairs already took the drag to 0.0146R. The question is whether
timing can take it further.

**Three measurements decided the design and all were made before building
anything.**

  * **41.4% of bars carry no recorded spread**, and the missingness trends --
    44.9% of the first half of the sample against 57.0% of the second. Every
    figure here is conditional on the recorded half.
  * **67.7% of spread variance is BETWEEN days**, not within them.
  * **Within-day autocorrelation is -0.120 at lag 1**, +0.035 at lag 2,
    +0.007 at lag 6. The +0.6 autocorrelation visible at every lag in the raw
    series is entirely the slow between-day component.

So **timing entries inside a session is refuted before it is tried**: the last
bar's spread says nothing about the next bar's. What remains is whether an
expensive DAY can be recognised from the day before, which is where two
thirds of the variance lives. Day-to-day persistence is **+0.245**.

**Bucketing today by YESTERDAY's mean recorded spread, on the three pairs the
harness actually trades** (`reports/spread_timing_search.json`, 5,980 days):

| prior day | days | spread today | gross move | move per unit spread |
|---|---|---|---|---|
| **cheapest third** | 1,965 | **0.0024%** | 0.3512% | **145.3** |
| middle | 1,965 | 0.0040% | 0.4040% | 100.5 |
| dearest third | 1,966 | 0.0075% | 0.3749% | 50.3 |

**Trading only after a cheap day saves 67.6% of the spread and forgoes 6.3% of
the gross move.** That asymmetry is the finding, and the control that produces
it was built in: cheap-spread days are plausibly also quiet days, and a filter
that halves the cost and halves the move has bought nothing. Measured, it does
not -- **move per unit of spread is 14.53 against 5.03, a 2.9x difference.**
That is the largest cost lever this project has found, against 2x for the
symbol-choice lever already in use.

**It survives the checks that killed everything else.**

  * **Not a data-coverage artefact.** Correlation between a day's recorded
    coverage and its spread level is **-0.022**; coverage across the three
    buckets is 63.2%, 64.4%, 59.6%. Restricted to well-covered days (>80% of
    bars recorded, n=2,914) the saving **rises to 74.0%**.
  * **Not one era.** All four blocks agree: savings of 76.9%, 64.5%, 43.7%,
    74.2% against move forgone of 6.3%, 6.4%, 12.1%, 14.1%, with move per
    spread better in the cheap bucket in every era.
  * **Not a lookahead.** The filter uses only the PREVIOUS day, which is the
    distinction `regime_search` failed: bucketing today by today's own spread
    would be selecting cheap days by observing they were cheap.
  * **Not eaten by volatility, which is the unit that matters.** The live
    record measures cost in R, and R divides by ATR -- so if cheap-spread days
    were also low-ATR days the saving would vanish on the way into the
    account. Measured, **ATR per unit of price is flat across the buckets**
    (0.1359%, 0.1455%, 0.1364%, a 0.4% difference end to end), and the cost
    **in R** falls by **68.0%**, matching the 67.6% measured in price units.

**A units defect was found here and it was mine.** MT5's `spread` field counts
BROKER POINTS, and this broker quotes a fractional fifth digit -- so a point is
**1e-5 on EURUSD and 1e-3 on USDJPY**, not the pip. The first version
hard-coded the pip and every absolute spread figure came out **exactly 10x too
large**. It surfaced because the implied cost in R was 0.1314 where this file
has measured 0.0096-0.0174R. The bucket ratios are a constant scale apart and
were therefore unaffected, so the saving and every conclusion below stand, but
the printed percentages above are the corrected ones. The tool now READS
`symbol_info(sym).point` instead of assuming it, and carries a fence on
`swap.py`'s reasoning: a round-trip spread above 1% of price is a unit error
rather than an expensive instrument, and is named rather than reported.

**The gross side was ASSUMED in the first version and has since been
MEASURED, which changed the answer.** That version scaled the gross expectancy
by the ratio of daily absolute moves, forgoing 6.8%. A daily absolute move and
a bracketed trade's R are different quantities, and only the second is what
the account earns. Simulating **22,847 real bracketed trades** -- random
entries, 1.5xATR brackets, each charged its own day's spread:

| prior day | trades | gross R | cost R | net R |
|---|---|---|---|---|
| cheapest third | 7,616 | -0.0013 | **0.0132** | **-0.0145** |
| middle | 7,615 | -0.0248 | 0.0206 | -0.0455 |
| dearest third | 7,616 | +0.0126 | **0.0424** | -0.0298 |

The cost reduction is confirmed at **68.9%**. The gross R difference between
cheap and dear is **-0.0139 against a standard error of 0.016 -- t = -0.86**,
indistinguishable from zero, which is what random entries must give. So the
gross is held CONSTANT rather than scaled, and the corrected figure is
**-0.0082R to -0.0012R**.

**AND THAT IS STILL NOT AN EDGE. Read this part before acting on any of the
above.** Two reasons, and the first is decisive:

  * **The +0.0064R gross it leans on is not demonstrated.** `trade_autopsy`
    puts the residual after cost at **t = 0.43**. Cutting the cost of a
    strategy whose gross edge is inside noise converges the loss toward zero;
    it does not produce a profit. Note what that implies about the corrected
    arithmetic: the net figure is only a whisker from positive, and the whole
    distance is carried by a gross term that has not been shown to exist.
  * **It is a cost measurement, not a strategy.** Nothing here says when to
    trade, only when trading is dearer. It makes a loser lose more slowly,
    which is what the symbol-set change did and was recorded as.

So the honest summary of twenty-two searches is unchanged in its conclusion
and sharper in its reason: **direction is unforecastable here, cost is the one
quantity with a measured sign, and the cost is now measurably timeable -- by
enough to halve the drag and not nearly enough to cross zero.**

    python tools/spread_timing_search.py --cheap-only

## Regime conditioning, and the one-bar error that manufactured 41% a year

Twenty-first search, and the first that tests no new rule at all. Every search
above judged its candidates UNCONDITIONALLY, and the strongest objection to
that record is structural rather than about any one family: a rule that works
in one regime and loses in another averages to nothing, and twenty
unconditional nulls cannot separate "no edge" from "edge masked by regime".

Kaufman makes that objection with numbers, which is why it was run rather than
argued. He measures a 20-day **efficiency ratio**, applies a plain 40-day
trend, and reports profit factor rising from about **0.7 at ER 0.204 to 3.2 at
ER 0.266** -- *low noise is good for trend following and high noise is not*.
He separately claims trend entries improve when filtered to **low realised
volatility**. Both conditioners, both lookbacks and the predicted direction
were published before this run, so this is confirmatory rather than a search,
and the correction is one search's rather than twenty-one.

**THE FIRST VERSION PRODUCED THE BEST RESULT IN THIS PROJECT'S HISTORY AND IT
WAS A ONE-BAR TIMING ERROR.**

    top bucket  +41.6% annualised    t = +21.63
    Spearman(bucket, mean) = +1.000  -- perfectly monotone across five buckets
    permutation null: top-minus-bottom spread exceeded in 0.0% of 1,000 rounds

Every gate this repository owns was passed, and with room to spare. The cause:
ER at bar t is computed from closes through c[t], so it contains the very
return r[t] that the trend rule earned on bar t. A trend position profits
precisely on bars where price travelled far in one direction, and that is
exactly what raises ER. **The outcome was being bucketed by a variable
containing the outcome.**

| efficiency ratio | top minus bottom | Spearman | top bucket t |
|---|---|---|---|
| contemporaneous | **+28.44bp** | **+1.000** | **+21.63** |
| lagged one bar | -1.29bp | -0.700 | -1.69 |
| lagged two bars | -1.52bp | +0.000 | -2.55 |

One bar of lag removes 100% of a 28.4bp effect.

**The permutation null could not have caught it, and that is the lesson worth
keeping.** Shifting the conditioner destroys exactly the contemporaneous
alignment the artefact depends on, so the null passes whether the bug is
present or absent -- it is not a safeguard against this class at all. The only
check that finds it is a lag test, and `tests/test_regime.py` now reproduces
the tautology in miniature, removes it with one bar of lag, and then shows a
shifted conditioner collapsing too, so the null's blindness is pinned rather
than assumed.

A second defect was caught on the way and is smaller but the same shape. The
rolling denominator was sliced at `[n-1 : len+n-1]`, which shifts the window
FORWARD and pulls future bars into it: measured, ER came out at **1.65 on a
straight line and as high as 10.6 on a random walk**, both impossible for a
ratio bounded by 1. A quantity with a known bound should be checked against
its bound.

**Corrected, regime conditioning does not rescue trend following.**

| bucket | ER (lagged) | realised vol |
|---|---|---|
| lowest | -0.088bp | -0.390bp |
| 1 | -0.276bp | -0.298bp |
| 2 | -0.078bp | -0.267bp |
| 3 | -0.766bp | -1.027bp |
| highest | -1.379bp | -0.623bp |
| Spearman | **-0.700** | -0.500 |
| null p | 23.8% | 42.4% |

**Every bucket of both conditioners is negative.** Kaufman predicts a strongly
POSITIVE relationship for ER and the measured one is mildly negative, with the
shifted-conditioner null putting a |rho| that large in 23.8% of rounds. The
low-volatility claim fares no better: the lowest vol bucket is -0.390bp and
the ordering is -0.500.

So the structural objection is answered on its own terms. The twenty
unconditional nulls above are not hiding a regime -- there is no ER or
volatility state in which this trend grid makes money on these pairs, and the
best-conditioned bucket is still a loss.

    python tools/regime_search.py
    python tools/regime_search.py --lag 0    # reproduces the tautology

## DeMark Sequential: the control beat the treatment, for the second time

Twentieth search, and the last fully-specified candidate from the twelve
books. It is the only one that is a STATE MACHINE rather than a formula --
nine-bar setup, an intersection test, a thirteen-count, and three cancellation
rules -- and Kaufman notes the consequence: **it has no parameter to vary**, so
robustness cannot be shown by sweeping a lookback the way every other search
here does. Cross-market and cross-era are the only evidence available, so both
were computed.

**The internal control is what the run was designed around.** The setup is
nine bars of ordinary momentum, which this repository has refuted several
times. The intersection, the countdown and the cancellation rules are the
elaborate part that is supposed to add something. So both were scored on the
same instruments: the setup alone, and the completed thirteen-count.

629 setups produced 306 signals across seven majors and 21,000 D1 bars, so
48.6% survive to a signal.

| horizon | setup only | t | full sequential | t | countdown adds |
|---|---|---|---|---|---|
| 5 | +0.0718% | +1.43 | **-0.1670%** | **-2.32** | **-0.2388%** |
| 10 | +0.1297% | +2.01 | -0.1450% | -1.49 | -0.2747% |
| 21 | +0.1918% | +2.02 | -0.0182% | -0.12 | -0.2100% |

**The setup is positive at every horizon and the completed machine is negative
at every horizon.** The countdown does not merely fail to add -- it subtracts
0.21 to 0.27 points wherever it is measured, and the difference between the
two carries a Welch t of **-2.72**. That is `volume_search`'s result for the
second time: **the control beat the treatment, and it is only visible because
the control was in the run.** Six of the earlier searches had no equivalent,
and their nulls are weaker for it -- they establish that a family did not work,
not that the thing being added subtracted.

**The inverted signal is the closest this project has come to a survivor, and
it still does not clear.** Fading the thirteen-count is -0.1670% at horizon 5
over 306 signals, which inverted is 21x the 0.0079% round trip. It passes
gates the other nineteen searches failed:

| gate | result |
|---|---|
| permutation null (circularly shifted prices) | **beats it** -- \|t\| >= 2.32 in 2.2% |
| consistency by symbol | 5 of 7 negative |
| sign across eras | negative in **all four** |
| **Bonferroni over 6 arms** | **FAILS** -- needs 2.64, has 2.32 |
| **era magnitude** | **FAILS** -- see below |

The era split is what retires it, and it is the same shape that retired
`donchian_fade_55`, the H4 exit grid and the calendar effect:

    2015-04 to 2017-12   n=76   -0.4143%   t=-2.87
    2018-01 to 2021-02   n=77   -0.0955%   t=-0.82
    2021-03 to 2024-01   n=76   -0.0294%   t=-0.16
    2024-02 to 2026-08   n=77   -0.1302%   t=-1.03

All four eras share the sign, which is more than most candidates here managed,
but the magnitude is carried by the first and eras two and three are
indistinguishable from zero. A candidate held up by one era is a regime, not
an edge, and that judgement is not being softened because this one came closer
than the others.

**What the result actually says about the system.** DeMark Sequential is an
EXHAUSTION system: the thirteen-count is supposed to mark a move that is
spent, and price is supposed to reverse. Measured on this venue over this
window, price CONTINUED instead. The premise is not merely unsupported, it
points the wrong way -- while the nine bars of plain momentum underneath it
pointed the right way and were then thrown away by the machinery built on top.

**One implementation ambiguity, stated rather than buried.** The rules do not
settle what a fresh setup does to a countdown already running. This restarts
it, per the specification as written, and the consequence is visible and
pinned in the tests: on a perfectly monotone decline a new setup completes
every nine bars, so the countdown resets forever and no signal is ever
emitted. That is the rule rather than a defect -- on real data setups are
irregular and 48.6% reach a count -- but a different reading of recycling
would produce a different signal set, and the result should be read knowing
that.

    python tools/demark_search.py
    python tools/demark_search.py --timeframe H4

## Round numbers, and the rounding bug that invented a finding

Nineteenth search, and the only candidate in this project with an external
citation behind it. Kaufman points at Carol Osler, *Support for Resistance:
Technical Analysis and Intraday Exchange Rates* (FRBNY Economic Policy Review,
July 2000), which found that support and resistance levels published by six
trading firms predicted intraday price interruptions and stayed informative
for about five days. The folklore version is that stop orders cluster at the
big figure, so price pierces it and snaps back. **The two books on the shelf
disagree on the sign** -- one trades the reversal, the other trades the
round-figure breakout -- so the test is two-sided.

**A rounding artefact in the first version produced a finding, and removing it
reversed the answer. That is the most useful thing here.** Converting a price
to whole pips with `np.rint` invokes numpy's banker's rounding: a value exactly
on half a pip goes to the nearest EVEN pip. About a tenth of this broker's
quotes sit exactly on a half pip, so every one of them was pushed onto an even
digit. Measured across 350,000 H1 bars, the last whole-pip digit came out
**1.07x expected on even digits and 0.93x on odd, identically in all seven
majors**, and the digit profiles correlated **+0.607** across pairs. That is
exactly what a real, shared microstructure effect looks like, and it was the
rounding mode.

| | with `np.rint` | through integer tenths |
|---|---|---|
| even / odd last pip digit | **1.07 / 0.93** | 1.0014 / 0.9986 |
| cross-pair profile correlation | **+0.607** | +0.084 |
| chi-square vs uniform | 3,861 | 510 |
| **digit 00** | **1.059x, rank 34 of 100** | **0.950x, rank 97 of 100** |

The cross-pair correlation is the tell. A per-pair price history cannot
correlate at +0.607 across seven different instruments; only something in the
code can. Corrected, it falls to +0.084 and the residual non-uniformity is
each pair's own visited range, which is what it should have been all along.

**Corrected, round numbers are UNDER-visited, not over.** Extremes land
exactly on the round level 0.950x as often as expected, ranking **97th of 100
positions**, and within three pips either side the figure is 0.947x. The half
level is 0.984x at rank 74. No clustering test needs a permutation here,
because the null is already inside the data: the mass at position *d* is
precisely what the mass at "00" would be if the round number sat at *d*.

**The tradable arm is decided by its controls, and the round level is the
WORST of the eight.** A pierce is a bar crossing a level from either side,
scored by the forward return signed against the pierce, so a positive number
is the snap-back the folklore predicts:

| grid offset | pierces | mean reversal | t |
|---|---|---|---|
| **+0 pips (ROUND)** | 48,688 | **+0.0001%** | **+0.08** |
| +13 pips | 51,212 | +0.0024% | +3.82 |
| +37 pips | 52,483 | +0.0021% | +3.37 |
| +23 pips | 51,599 | +0.0016% | +2.58 |
| +83 pips | 51,703 | +0.0016% | +2.52 |
| +50 pips | 49,560 | +0.0012% | +1.90 |
| +61 pips | 50,530 | +0.0008% | +1.31 |
| +7 pips | 50,129 | +0.0008% | +1.27 |

**All seven arbitrary offsets beat the round level.** Whatever small reversal
exists after crossing a price level is generic short-horizon mean reversion
that has nothing to do with roundness -- and at its best, +0.0024%, it is
still a quarter of the 0.0099% round trip it would have to pay.

**What this does not claim.** Osler measured levels PUBLISHED BY DEALERS on
intraday data, not round numbers on hourly bars, and this project has neither
her data nor those publications. This refutes the round-number folklore on
this venue at H1; it does not touch her result. The distinction is the same
one recorded for Kaufman: a claim tested outside its own window and its own
instrument has been narrowed, not overturned.

    python tools/round_number_search.py
    python tools/round_number_search.py --spacing 50

## Did the high come before the low? The one thing OHLC does not record

Eighteenth search, and the only one whose feature no book on the shelf could
have computed. Two bars with **identical** open, high, low and close can have
taken opposite paths: one rose to its high and then sold off, the other fell
to its low and then rallied. OHLC cannot separate them. A finer series can,
and there are H1 bars underneath every D1 bar here, so the ordering is
recoverable for nothing.

**Measured before any hypothesis: the ambiguity is 0.62%.** Only 91 of 14,623
days across the seven majors put their high and low inside the SAME H1 bar.
H1 resolves a D1 path cleanly; H4 would not, since four sub-bars is too
coarse, so this runs at D1 and that is a measurement rather than a preference.

**The confound is the whole problem and it is severe.** Where a bar closes in
its own range very nearly determines which extreme came first:

| close-in-range decile | 0 | 3 | 5 | 7 | 9 |
|---|---|---|---|---|---|
| share high-first | **96.9%** | 69.6% | 36.8% | 14.4% | **3.7%** |

It is 89.3% high-first on down bars against 12.2% on up bars. So the RAW
feature is close to a restatement of candle shape, and `shape_search` already
refuted candle shape across 28 candidates. **An unconditional test here would
have rediscovered that result and reported it as new.**

So the question is narrowed to the only part that is genuinely new: does the
ordering carry anything BEYOND what close-in-range already says? Bars are
stratified into close-in-range deciles and compared only WITHIN a stratum --
between bars that closed in the same place but travelled there differently.

**The stratification is visibly doing the work.**

| | difference | t |
|---|---|---|
| unconditional | +0.0074% | +0.83 |
| **stratified** | **+0.0044%** | **+0.36** |

Controlling for close-in-range removes about 40% of the apparent effect, which
is the confound being taken out rather than a signal being found.

**The null is permutation WITHIN strata**, and that design is what makes the
answer mean anything. A global shuffle would destroy the close-in-range
relationship as well as the path information, so it would test whether
close-in-range predicts returns -- a question already answered. Shuffling
inside each decile holds `P(high-first | decile)` EXACTLY fixed and destroys
only the ordering. Over 2,000 rounds it puts **|t| >= 0.36 in 73.7%**, with
the null's own |t| reaching 3.56.

**And the point estimate is less than half the cost anyway**: +0.0044%
against a round trip of 0.0099%.

**The verdict does not depend on where the thin strata are cut**, which was
checked rather than assumed. The bottom decile is judged on only 51 minority
observations and carries the largest single difference (-0.1647%), so the
floor was varied:

    minimum per class    40      100      200      300
    deciles used          9        8        6        4
    stratified t      +0.36    +0.60    +0.32    +0.11

Every setting is far from significance and every point estimate stays below
cost. The top decile, at 22 against 1,380, is reported and NOT judged -- a
t-statistic on twenty-two observations is not evidence, and pooling it would
have let the emptiest stratum move the answer.

**The defect found was in the test, and it is the same one the gotobi test
made.** It asserted that a planted effect would be recovered to within a
tenth of a basis point. The estimator returns the planted effect PLUS whatever
difference that particular noise draw already had between the two classes, so
the check was demanding that sampling error be removed. Asserted against the
same draw the identity holds exactly. Twice now a test here has been written
as though an unbiased estimator were an exact one.

    python tools/path_order_search.py
    python tools/path_order_search.py --horizon 2

## Gotobi: the one calendar claim with a mechanism, and its own controls

Seventeenth search, and the first whose hypothesis names in advance which
controls must come back empty.

Japanese importers settle on days ending in 5 and 0 -- the 5th, 10th, 15th,
20th, 25th and 30th, *gotobi* -- and must buy dollars by the Tokyo fix at
**10:00 JST**. The claim (Archer, `Getting Started in Currency Trading`) is
that the excess dollar demand lifts USDJPY into the fix.

**This is not the calendar family already refuted here, and the difference is
the reason it was worth a run.** Day-of-week and turn-of-month died when
Tuesday and Wednesday CONTROLS scored +86.0 and +83.2 against Thursday's
+86.6: going long anything in that window earned about 86 points, so the
effect was directional drift seen from inside a grid. That search had no way
to say which controls SHOULD be empty. This one does, because the mechanism is
specific -- Japanese importers buying dollars says nothing whatever about
EURUSD.

**The clock is the trap and it is this repository's recurring one.** MT5
stamps a bar with the SERVER's wall clock rendered as a UTC epoch, and this
broker is UTC+3; JST is UTC+9. So 10:00 JST is 01:00 UTC is **server hour 4**.
Reading the local hour on this UTC+5:30 machine would have aimed the test five
and a half hours away and produced an equally confident null about the wrong
four hours.

**Every arm comes back empty, and the pair controls are what settle it**
(`reports/gotobi_search.json`, 402 gotobi dates, 2018-09-05 to 2026-09-23):

| arm | gotobi % | other % | diff % | t |
|---|---|---|---|---|
| **USDJPY into the fix** | +0.0058 | +0.0087 | **-0.0029** | -0.27 |
| USDJPY 12->16 (hour control) | -0.0151 | +0.0013 | -0.0164 | -0.90 |
| EURUSD (pair control) | +0.0185 | +0.0143 | **+0.0043** | +0.66 |
| GBPUSD (pair control) | +0.0215 | +0.0161 | **+0.0054** | +0.65 |
| AUDUSD (pair control) | +0.0358 | +0.0306 | +0.0052 | +0.33 |
| USDCHF (pair control) | +0.0194 | +0.0277 | -0.0083 | -1.17 |

**The sign is wrong on the one pair the mechanism names.** USDJPY rises LESS
on settlement days than on other days, and it is the only negative difference
among the three pairs that should show nothing. A settlement-flow effect that
appears more strongly in EURUSD and GBPUSD than in USDJPY is not a settlement
effect; it is the same "positive across unrelated variants" signature that the
`time_120` exit, the H4 exit grid and the day-of-week search all produced.

Nothing else clears either. The permutation null, shuffling which dates carry
the label while holding the count and the window fixed, puts **|t| >= 0.27 in
78.0% of 2,000 relabellings**; the null's own |t| reaches 3.18. And the
measured difference is **-0.0029% against a round-trip cost of 0.0045%** -- so
even with the sign reversed the effect is smaller than the spread it would
have to cross.

**Power was established before the null was believed.** The detector finds a
planted 0.02% effect at t = +3.24 and returns +0.61 on nothing. But the honest
statement is the one made before the run rather than after: at 402 dates, one
standard error of a four-hour USDJPY return is about 0.0075%, so an effect must
reach roughly **0.015%** to be measurable and **0.0045%** to be worth trading.
Those two numbers are close enough that a real but small gotobi effect and no
effect at all are **not fully separable here** -- which is the same shape as
the carry result and the 6,600-trade figure in the cost note, and is why this
is recorded as "does not clear" rather than "does not exist".

**The defect found was in the test, not the tool, and it is worth keeping.**
The first version built a genuine 01:00 UTC timestamp and asserted that
`server_hour` would read 4. It reads 1 -- correctly, because an MT5 stamp is
the SERVER clock rendered as a UTC epoch, so a real bar at server hour 4
carries an epoch whose hour is 4, while the epoch of the true 01:00 UTC
instant carries 1. Constructing the second kind and expecting the first is
exactly the confusion the whole window depends on not making.

    python tools/gotobi_search.py
    python tools/gotobi_search.py --window 0 4

## Kaufman's 28-80 day trend claim, tested at D1 on his own protocol

One published claim contradicts the nulls in this file with numbers attached,
so it was run rather than dismissed. Kaufman's 17-market study ranks **EURUSD
the single most robust trending market, 87% of tests profitable**, with
linear-regression slope at 93% and N-day breakout at 90%, over calculation
periods of **28-80 days**. `book_rules_search` tested regression slope at H1
with n=50 and n=200, where 200 bars is about eight days: **his hypothesis was
never in the grid.** That was a gap in what was run, not a disagreement about
what was found.

**The protocol is his, not this repository's, and that distinction decides
whether the number means anything.** His systems are ALWAYS IN THE MARKET and
reverse on the signal, with no stop and no target. `rule_search` brackets every
trade at 1.5xATR, and the exit searches already showed a bracket can flip a
trend result's sign -- so running his entries through it would have tested a
third strategy belonging to neither of us. `tools/kaufman_replication.py` is
his simulator: one position, always on, reversed on a flip, marked to market,
the spread charged on each crossing and twice on a reversal.

Two of his own rules were adopted at the same time, and this repository was
following neither. **The common start date**: every candidate begins where the
LONGEST wind-up ends, or a 28-day rule silently trades 52 bars the 80-day rule
spent warming up. Measured at H1 the spread was 1.00% and harmless; at D1 an
80-bar wind-up is ~3% of the history. **Geometric spacing**: 28, 35, 44, 55,
69, 80, because a 75-day and an 80-day average are nearly the same hypothesis
and arithmetic spacing fills the correction's denominator with near-duplicates.

**The claim does not survive, and it is not close.**

| symbol | % of grid profitable | best t | best cell |
|---|---|---|---|
| **EURUSD** | **13.3%** (Kaufman: 87%) | +0.90 | slope 35 |
| GBPUSD | 33.3% | +0.91 | slope 44 |
| **USDJPY** | **93.3%** | +1.80 | breakout 44 |
| USDCAD | 13.3% | +0.27 | slope 28 |
| AUDUSD | 3.3% | +0.15 | slope 28 |
| USDCHF | 0.0% | -0.02 | slope 35 |
| NZDUSD | 0.0% | -0.05 | lwma 35 |

Pooled, **22.4% of 210 tests are profitable at a best t of +1.80**, against a
Bonferroni threshold of 3.675 (`reports/kaufman_replication.json`).

**The null is the part that settles it.** Circularly shifting each signal --
identical trade count, identical holding structure, alignment with returns
destroyed -- gives **49.4% of the grid profitable (max 69.0%) at a best t of
+2.10 (max +3.19)**. So the real grid is profitable LESS than half as often as
a shuffled one, and the shuffle scores a higher best t. That is the sixth time
in this file a null has outscored the real candidates, and the first time the
real result has been this far BELOW chance.

**Cost is not the explanation, and that is new.** Gross and net are nearly
identical: EURUSD is 13.3% profitable either way, AUDUSD 3.3% either way, and
only USDCHF and NZDUSD move at all (3.3% to 0.0%, 6.7% to 0.0%). At D1 with a
28-80 day hold there are few reversals, so the spread barely bites and the
GROSS return is already bad. **Every other search in this file found cost to be
the dominant effect; here the direction itself is wrong.** These pairs
mean-reverted at these horizons over this window, which is why an uncorrelated
signal beat a trend signal.

**USDJPY is the single exception and it is one era.** Its +55.43% on the best
cell is **72% from 2020-12 to 2023-11** -- the yen depreciation -- with the
2018-2020 era negative, and the grid share swinging 76.7%, 6.7%, 100.0%, 76.7%
across four eras. That is a regime seen from inside a grid, the same diagnosis
that retired `donchian_fade_55`, the H4 exit grid and the calendar effect. Its
best t is +1.80 against a 3.675 threshold and a null reaching +3.19.

**What this does NOT establish, which matters as much as what it does.** The
window here is **2015-03-06 to 2026-09-23** and Kaufman's study ran 1990-2011:
**they do not overlap at all.** This cannot refute his measurement on his data,
and it is not offered as doing so. What it shows is that the claim does not
hold in the decade AFTER it was published. His own book predicts exactly that
-- he documents noise rising over time across all regions and states that low
noise favours trend-following and high noise does not -- so the honest reading
is a decayed effect rather than a wrong one, and that is the same shape as the
crypto search's monotonic decline to negative.

**One calibration worth keeping, because it reframes his standard.** His
acceptance rule is that ~70% of tests over a reasonable range should be
profitable. Measured on a pure random walk, this grid already returns **56.7%
profitable at a best t of +0.92**. So "70% of tests profitable" sits about
thirteen points above what noise delivers, and is a much weaker filter than it
sounds -- which is consistent with Chapter 21 offering it in place of the
multiple-comparison correction it declines to make.

    python tools/kaufman_replication.py

## Every two- and three-bar shape there is, with an exact correction

Fifteenth search, and the only one here whose candidate list was not CHOSEN.
Every other search in this file picked its grid -- 41 rules, 28 shapes, 16
bookshelf families -- and computed Bonferroni over the list somebody wrote
down. That correction is honest about the trials RUN and silent about the
trials AVAILABLE, so it is a floor on the real multiplicity rather than the
real multiplicity.

Pugh's encoding (Archer, `Getting Started in Currency Trading`) labels a bar
by its high and low against the previous bar's: BULL, BEAR, OUTSIDE, INSIDE.
Four labels, no parameters, so the two-bar family is exactly 16 and the
three-bar family exactly 64. **Those are not selections, they are the complete
families**, and correcting across 80 is therefore exact.

| | |
|---|---|
| candidates | 80 tested, **78 with enough trades** |
| best in-sample | **+1.48** (`bear_out_bear`) against a null of **+1.35** |
| Bonferroni threshold | 3.414 |
| best out of sample | +0.49; date-clustered 0.24; symbol-clustered 0.64 |
| cleared 1.96 out of sample | **0 of 80** against **2.0 expected by chance** |

The in-sample margin over the null is 0.13, well inside the 0.5 fence this file
sets. Because the family is complete, the null is a closed statement rather
than another open-ended family coming back empty: **no two- or three-bar
configuration of highs and lows carries a tradable directional bias at this
cost.** That also disposes of the Nofri congestion rule and the four "bathtub"
conditionals from the same books, which are specific members of this family.

**The exactness claim needed one qualification, and it was measured.** The
family is complete but NOT uniformly powered: `out_out_out` occurs 72 times
across seven majors and `in_in_in` 113, against 23,184 for `bull_bull`. Those
two were exactly the two of 80 that failed the trade minimum. The correction is
exact over the family; the evidence inside it is very uneven.

**The defect found was a misclassification, not an arithmetic error.** Phrased
as "higher high and higher low", a bar whose high and low EQUAL its
predecessor's satisfies neither test, lands in the not-higher/not-higher corner
and is labelled BEAR. A bar identical to the one before it is not bearish, it
is contained. Measured: **2,597 of 139,993 H1 bars (1.855%) carry an equal high
or low**, so this moved about 2,600 bars into the wrong class. Re-phrased as
"strictly higher high" and "strictly lower low", the same two booleans send a
flat bar to INSIDE, which is what containment means.

    python tools/pugh_search.py

## What Kaufman supplies that this repository does not, and the reverse

Twelve books, 7.1 million characters, read in full. Kaufman's `Trading Systems
and Methods` is the reference work, and **Chapter 21 states this file's thesis
in its own words and then does not act on it.** The section is called *The
Significance of "Significant"*. It poses the multiple-comparison problem
exactly -- asking whether it is not the cumulative number of tests across all
strategies that should count -- and supplies **no adjustment whatsoever**,
substituting a heuristic that 70% of tests over a declared range be profitable.
The permutation null is absent: Monte Carlo appears twice, once to sample the
PARAMETER space and once for synthetic data, and he **abandoned** the latter
because shuffling separates a bull market from the reversal that follows it.

So of this project's five gates he supplies exactly one -- the walk-forward,
and that one properly: an 8-step loop, two named failure modes (short-term
bias, and feedback from repeating the procedure), an expected ~50%
out-of-sample decay, and a hard rule that nothing may be fixed once the holdout
has been touched.

**Three things he has that this repository should take.**

  * **Kurtosis as an overfitting screen.** On the strategy's OWN daily returns,
    `K > 7-8` means "an overwhelming number of profitable trades of similar
    size, which is not likely to happen in real trading". It is independent of
    the permutation null and catches a class of fit a t-statistic will not.
  * **The common start date.** Every candidate must begin where the LONGEST
    wind-up ends, or a fast rule silently trades bars the slow one spent
    warming up and the two are no longer judged on the same sample. Measured
    here: the warm-up spread across `book_rules_search` is 0 to 199 bars,
    **1.00% of a 20,000-bar sample** -- real, and an order of magnitude smaller
    than the 6% his daily-data example implies. It manufactured nothing, since
    the worst offender `linreg_ride_200` scored -3.46. **It matters about three
    times more at D1**, where an 80-bar wind-up is 3% of a 2,600-bar history,
    so it has to be fixed before any D1 replication rather than after.
  * **Geometric parameter spacing.** 5, 10, 20, 40, 80, 160 rather than every
    fifth day. A 75-day and an 80-day average are nearly the same strategy, so
    arithmetic spacing fills the Bonferroni denominator with near-duplicates
    and misstates how many independent hypotheses were really tried.

**And one claim of his that contradicts a null here, with numbers attached.**
His 17-market study ranks **EURUSD the single most robust trending market, 87%
of tests profitable**, with linear-regression slope at 93%, over calculation
periods of **28-80 DAYS**. `book_rules_search` tested regression slope at n=50
and n=200 on H1 bars, where 200 bars is about eight days -- **his range was
never tested here.** That is a gap in what was run rather than a disagreement
about results, and it is the next thing to measure. His own caveats to carry
into that run: the profit factor is computed on closed trades only, the $40
round turn is below his own FX slippage estimate, there is no permutation null,
and there is no correction across 40 periods x 5 methods x 17 markets.

His two-MA crossover is his WORST method at 54%, which agrees with the MA-cross
null here rather than contradicting it.

## The bookshelf, read in full, and the four rules that were left

Twelve books, 7.1 million characters, read end to end on 2026-09-23 and mapped
against every search above. **Almost all of it is ground already refuted here**
-- RSI, MA crosses, Donchian, Bollinger, momentum, single-bar candle shape,
carry, calendar, hour filters. Four families came back both genuinely untested
AND mechanically unambiguous, and those two conditions are the whole filter: a
rule this repository cannot state without inventing a definition is a rule it
would be testing on its own authorship rather than the book's.

`tools/book_rules_search.py` runs them through `rule_search`'s pipeline -- same
permutation null, Bonferroni, era blocks, walk-forward, date clustering,
measured spread -- over 19,851 H1 bars per symbol across the seven majors
(`reports/book_rules_search.json`). Sixteen candidates, each family with its
inverse.

**Nothing clears anything.** Best in-sample t is **+0.31**
(`three_methods_ride`) against a 2.955 Bonferroni threshold and a permutation
null averaging **+0.38** -- the shuffle timed these rules better than the rules
did, for the fifth time in this file. Out of sample the best candidate is
**-2.39** at -53.7 points. **Zero of 16** cleared 1.96 out of sample against 0.4
expected by chance, the best is -3.63 clustered by symbol with 1 of 7 pairs
positive, and its era blocks run -29.3, +20.6, +12.1, -49.7.

Note also that the "best" candidate has **106 out-of-sample trades**. It leads
the table on the thinnest evidence in it, which is what a leaderboard does when
nothing has an edge.

**The gross/net split is the finding, and it is the same one every time.**

| candidate | net | gross | implied cost |
|---|---|---|---|
| `ha_ride` | -12.6 | -7.3 | 5.3 |
| **`ha_fade`** | **-0.1** | **+5.2** | 5.3 |
| `sar_ride_0.02_0.2` | -12.5 | -7.2 | 5.3 |
| **`sar_fade_0.02_0.2`** | **-0.2** | **+5.1** | 5.3 |

Every trend-following variant is significantly NEGATIVE in sample -- `ha_ride`
at **t = -6.83**, `linreg_ride_50` at -5.62, `sar_ride_0.01_0.1` at -5.49,
`sar_ride_0.02_0.2` at -5.29 -- and each inverse pays the identical 5.3 points.
Flipping a rule reverses the timing and changes nothing about the cost, which
is why the fades cluster just under break-even instead of mirroring into
profit. That is `supertrend_search`'s result reproduced on four families that
share none of its construction, and it is once again the only effect here large
enough to measure.

**Two rules were deliberately NOT tested, and the reasons are the useful part.**

  * **Pivot points.** The level is (H+L+C)/3 of the PREVIOUS CALENDAR DAY, held
    fixed through the next day. `rule_search`'s signal contract passes o/h/l/c
    and no timestamps, so a calendar-day pivot cannot be computed inside it and
    a rolling 24-bar substitute is a different rule. Untested with the reason
    stated beats tested as something else.
  * **Engulfing, Harami and the Star patterns.** Each needs "in a definable
    trend" or "well into the body". Quantifying those would be authoring the
    rule, and the result would measure this repository's threshold rather than
    the book's pattern. The beginners' guide concedes the point itself: it
    states that the Hanging Man and the Bullish Hammer are the SAME single bar
    and that the prior-trend context is the entire signal.

**The defect caught on the way is the silent-rule shape again.**
`three_methods` fired **zero times in 3,000 synthetic bars** while looking
perfectly healthy. A signal stuck at zero produces "no trades", reads as a
null, and is indistinguishable in the report from a rule that was tried and
failed. `tests/test_book_rules.py` now requires every candidate to fire at
least 20 times on 20,000 realistic bars, and tightening one threshold to an
impossible value turns it red by name.

**And one measured curiosity that changes how the candle result is read.**
`soldiers` fires once in 3,000 random-walk bars and **21,089 times** across
seven real pairs. The reason is that an FX bar opens almost exactly on the
previous close, so "opens inside the previous body" is nearly free -- and in FX
the pattern degenerates towards three consecutive up-closes, which is momentum
at n=3. Its null is therefore a weaker statement than it looks: it is mostly a
re-refutation of ground already covered, not an independent test of the
candlestick literature.

    python tools/book_rules_search.py
    python tools/book_rules_search.py --timeframe D1

## Spread reversion between two majors, and why the control decided it

The twelfth search, and the first two-leg trade in this file.
`cross_search.py` is the closest thing to it and is a different mechanism: it
RANKS the seven foreign currencies and holds the top n against the bottom n,
so its signal is relative strength and its weights are +-1/n. A pairs trade
fits a HEDGE RATIO between two specific series and bets their fitted spread is
stationary. Ranking needs no stationarity claim; this one does, and that claim
is what is being tested. AUDUSD against USDCAD is the cleanest story available
-- two commodity currencies against one dollar, so the dollar cancels.

**A pair crosses the spread FOUR times a round trip**, both legs in and both
out, where every other search in this file paid two. A pair is not a cheaper
way to trade, it is a twice-as-expensive one that has to earn the difference
back before anything else.

| arm | best in-sample | out | cleared 1.96 OOS | median OOS Sharpe |
|---|---|---|---|---|
| real pairs | **+2.30** (AUDUSD/USDCAD) | +0.32 | 0 of 42 (1.1 expected) | -0.097 |
| **synthetic control** | **+2.17** | -1.68 | 0 of 42 | -0.244 |

42 candidates, 21 pairs x two lookbacks, 19,990 H1 bars on a common window,
Bonferroni threshold 3.241 (`reports/pairs_search.json`).

**The shuffle is NOT the control that matters here, and that is the finding.**
Two independent random walks regress on each other with a significant slope
most of the time -- the Granger-Newbold spurious regression -- so a fitted
spread that looks tradable is what the null DOES in this family, not evidence
against it. The decisive control is therefore a SYNTHETIC pair: two
independent random walks carrying the real series' own volatilities, traded by
identical code. It scores **+2.17 against the real +2.30**. Nothing separates
them. Without that arm this would have read as a near-miss worth pursuing, and
every gate short of it would have agreed.

**The engine was verified before its null was believed**, as in
`cross_search.py` and unlike the nine searches before it. A planted
cointegrated pair -- a random walk plus a strongly mean-reverting stationary
error -- is found at **t = +18.57**. Independent walks score **+0.41**. So an
effect of the size claimed would not have been missed.

**The lookahead check here is worth copying, because the obvious version of it
would have passed a bug.** Fitting beta over the whole sample is the classic
trap: the spread is then constructed to be mean-zero across exactly the period
being traded, so it reverts by construction. The test does not inspect the
slice arithmetic, it TRUNCATES -- the signal at bar t must be identical whether
or not the data after t exists -- and that fires on any peek regardless of
where it hides.

**Measured, that lookahead scored +1.82 in sample against the honest +2.30 --
LOWER.** This one did not manufacture an edge, where `cross_search`'s
off-by-one scored 2.47 and cleared both Bonferroni and its permutation null. A
test that only caught bugs when they happened to be profitable would be
useless, which is the reason the check is an identity about information and
not a comparison of t-statistics.

    python tools/pairs_search.py
    python tools/pairs_search.py --self-test

## Pooling all seven majors: the power objection, answered

Every model verdict above carries the same available rebuttal, and it is a fair
one. The per-symbol datasets are 4,926 rows against 22 features -- **224 rows a
feature** -- where demonstrating the 1.50-point spread hurdle at 80% power needs
**8,711**. A null from an underpowered test is not evidence of no effect; it is
evidence of not having looked hard enough. That objection has stood since the
first trained models. `tools/pooled_walk_forward.py` removes it: pooling the
seven majors gives **34,482 rows over 4,928 distinct timestamps -- 1,567 rows a
feature, four times the requirement.**

**The unit question is answered before the pooling rather than after it.**
`DatasetConfig` takes one symbol deliberately and its docstring says why, and
the metals error above is what happens when that is waved at. Every feature used
here is dimensionless -- returns and log returns, body/range/wick as
percentages, distances in ATR or standard deviations, RSI, hour of day, a
rollover flag -- so a USDJPY row and a EURUSD row are the same quantity. Price
and points are not, which is exactly the pooling that produced the +4236
palladium figure. The label is `bracket_outcome`, WIN or LOSS, path dependent
and already scale free.

**The fold boundary is a TIME, not an index**, and that is not a refinement.
Seven symbols share the same hourly stamps, so 34,482 rows sorted by time come
in groups of seven, and an index cut lands mid-group -- putting a bar's own
siblings in training while its row is tested. The scaler is fitted per fold on
that fold's own past, for the reason the corrected per-symbol walk-forward
documents above.

**With four times the required power, nothing clears the hurdle, and the real
labels score WORSE than their own shuffle.**

| fold | train | test | real margin | shuffled control |
|---|---:|---:|---:|---:|
| 1 (from 2026-01-25) | 5,740 | 5,746 | **-4.19** | -1.04 |
| 2 (from 2026-03-13) | 11,486 | 5,748 | **-2.19** | -0.64 |
| 3 (from 2026-04-30) | 17,234 | 5,747 | -0.16 | -0.42 |
| 4 (from 2026-06-17) | 22,981 | 5,747 | -0.40 | +0.31 |
| 5 (from 2026-08-05) | 28,728 | 5,754 | +0.17 | -0.30 |
| **mean** | | | **-1.35** | **-0.42** |

Zero of five folds clear 1.50 in either arm, one of five is positive in each,
and the real arm beats its control in **2 of 5** folds. Do not read "worse than
the control" as a finding in itself -- five folds cannot separate -1.35 from
-0.42, and a model reliably ANTI-predicting would be an edge inverted. The
supportable statement is the weaker and more useful one: **at four times the
power the per-symbol results were criticised for lacking, the pooled model does
not separate from its own shuffle, and neither arm approaches the spread
hurdle.**

**The convergence toward zero is in the CONTROL too, which is what makes it
readable.** The real margins run -4.19, -2.19, -0.16, -0.40, +0.17 as the
training set grows from 5,740 to 28,728 rows, and read alone that looks like a
model learning. The shuffled arm does the same thing -- -1.04, -0.64, -0.42,
+0.31, -0.30 -- on labels with nothing in them. More training data moves a
fitted logistic toward the majority-class prior whether or not there is signal,
so the trend is a property of sample size and not of the market. Controls have
dissolved apparent findings repeatedly in this file -- the volume weighting, the
Tuesday calendar control, the GBPUSD shuffle that beat its own model -- but this
is the first time one has dissolved a TREND rather than a level.

**The defect found on the way is the usual shape.** `time_fold_edges` clamped
its final edge to the last timestamp, and the test block is half-open, so every
row at that stamp fell in no block at all -- not trained on, not tested on,
silently gone. The arithmetic is what caught it: fold 5 trained on 28,728 and
tested 5,747 against 34,482 pooled, and the missing **7 rows are one timestamp
times seven symbols**. It changed no verdict: re-run on the fixed code every
real-arm margin is identical to two decimal places and one control margin moved
by 0.01. That is precisely why nothing would have noticed it. The
final edge is now an open bound, `in_block()` exists so a caller cannot write
the clamp back by hand, and the tool now PRINTS its own row accounting so the
next such truncation announces itself instead of waiting to be summed by hand.

`time_fold_edges` had no test at all, despite being the single thing that makes
a pooled result believable. It now has ten, and reverting the clamp turns four
of them red at 2,793 rows of 2,800 -- the same signature as the real run.

**The ROWS LOST warning is itself proven rather than trusted.** A guard that
should never fire and never has is indistinguishable from one that is broken,
which is this repository's dominant defect class. So the accounting is a
function, `account_rows`, and a test hands it the CLAMPED edge list the defect
produced and requires the shortfall -- **7 rows**, the same number the real run
lost. The alarm is known to work before it is ever needed.

    python tools/pooled_walk_forward.py
    python tools/pooled_walk_forward.py --shuffle-labels

**Read this as closing the "not enough data" question rather than as a twelfth
null.** The per-symbol models were genuinely underpowered and that was the right
criticism of them. Given four times the data they needed, the answer did not
change.

## Before quoting the live paper-trading record

`track_record.py` merges each MT5 read into `data/track_record.jsonl` keyed by
position_id, so the sample accumulates instead of expiring with the broker's
history window (`reports/track_record.json`). Re-running it is idempotent.

The record is **882 trades and -102.78 USD** on a 100,000 demo deposit,
2026-08-24 to 2026-09-17 (re-merged 2026-09-19), and it must be quoted by
regime, because two incompatible bracket geometries are in it:

| reward:risk | trades | net | win rate | pooled t | by date | threshold |
|---|---|---|---|---|---|---|
| 0.2 (sl 3.0xATR, tp 0.5xATR) | 36 | +6.20 | 92% | 2.81 | 2.26 | 4.303 |
| 1.0 (sl 1.5xATR, tp 1.5xATR) | 832 | **-105.69** | 81% | **-2.11** | **-2.58** | 2.131 |

**The 1.0 row is the first live result this project has produced that reaches
significance, and it is a loss.** It clears the bar on all three statistics --
pooled -2.11, date-clustered -2.58 against a 2.131 threshold, symbol-clustered
-2.58 against 2.447 -- with 2 of 7 pairs positive over 16 entry dates. Read it
beside the 81% win rate, because the two together are the whole finding: the
harvest loop books winners and holds losers, so a high win rate is the
mechanism rather than an edge, and the losses have now landed. Nothing here
tested a rule; what is significant is the cost of trading this way, which is
the same effect the `random` rule showed at t = -3.60 and `body_ride_0.6` at
-4.65.

The 0.2 row still must NOT be read as its mirror image. Its pooled 2.81 and
its symbol-clustered 5.14 look decisive and are not: date clustering puts it
at 2.26 against a 4.303 threshold, because 36 trades fall on **three** entry
dates. Three days is not a sample, and the paragraph below says what made
those three days look so good.

On its first 14 trades that row scored a pooled t of **9.33**, and that
remains the most instructive number this project has produced, because it
was meaningless and looked decisive. `data/highwin_session*.log` record the
rule that generated it: `rule=random`. A random entry with a 6:1 adverse
bracket wins about six times in seven by construction, so the 93% win rate,
the profit factor of 5.3 and the 13-win streak were arithmetic rather than
evidence - the losses had not landed yet. Thirteen of those fourteen wins
opened between 11:05 and 13:32 on one day across six pairs, every one on the
same side of the dollar, which is the shared-move clustering the FX searches
already document.

It has since decayed from 9.33 to 2.81 on 22 more trades, without anything
changing but the sample. That decay is the same shape as the R-multiple's
below, and it is why a live t-stat is quoted with its trade count or not at
all.

Net currency cannot be pooled across those two rows. A trade's size is set by
its stop distance, and 5x separates them - structurally the metals-points
error, and `track_record.py` raises the same class of data gap at a 2x fence.
The R-multiple pools, because dividing by the money at risk removes the
regime: **-0.0247R over 863 trades, pooled t -1.76, date-clustered -1.57
against a 2.131 threshold, symbol-clustered -2.34 against 2.447**, 2 of 7
pairs positive. Pooled across both regimes it does NOT reach significance;
about 213 more trades would, at this effect size. (863 of 882, because 14
trades predate the order log and have no bracket to divide by.)

**The sequence is the lesson, and this section is the worked example.** It
said, until 2026-09-19: "+0.023R over 27 trades, pooled t 0.26 ... reaches
1.96 after about 1,500 more trades." Every figure in that sentence was
correct when written. The sample then grew from 27 to 863 and the mean went
from +0.023R to -0.025R -- the sign flipped, the early positive was noise,
and the trades still needed fell from ~1,500 to 213 because the effect being
measured got bigger rather than because anything improved. Earlier still, on
20 trades, this account scored +4.60 at a pooled t of 3.27.

So: +3.27, then +0.26, then -1.76, on one account whose rules did not change.
**Do not quote a live t-stat without saying how many trades it rests on**, and
do not treat a stale one as a starting point -- re-run `track_record.py
--merge` and read the number that comes out.

Check whose trades a merge is about to take. `mt5_account.closed_trades` pairs
every deal on the ACCOUNT, not every deal this tool opened, so anything else
trading the same login lands in the ledger looking identical - and until the
entry deal's magic was recorded, on 2026-09-07, nothing on the row could
separate them. Measured that day: 258 closed trades in 30 days, `{0: 1,
770315: 256, 770316: 1}` by magic. The untagged one was AUDCAD opened by hand
in the terminal for -0.17, larger than any trade the platform made, and the
next merge would have absorbed it into a 252-trade sample as one of this
tool's own.

`merge()` now defaults to this tool's own 770315 and prints the account census
beside what it took; `--all-magics` restores the old behaviour explicitly. The
fix is NOT retroactive and the difference matters: the platform's broker
adapter shared 770315 until that evening, so nine of the ten trades it made at
this venue carry the harness's tag and no filter can separate them. Those are
excluded by position id with `--exclude`, taken from the platform's own
`positions` table. The 252 rows already on file carry no magic at all, so the
existing record's provenance rests on the fact that nothing else traded this
login before that day - not on anything in the file.

## Before branching on an MT5 retcode

`tools/mt5_retcodes.py` holds the table, and it is seeded ONLY from codes this
repository has actually observed. The manual lists dozens more; a row for one
of those would be a figure nobody here measured. An unseen code classifies as
`UNCLASSIFIED` and is surfaced as its raw integer rather than guessed at.

Eight codes have evidence across 2,180 ledger rows: 10009 DONE (1,964),
10018 market closed (82), 10031 no connection (85), 10016 invalid stops (10),
10030 unsupported filling (1), 10017 trade disabled (1), 10025 no changes (1),
and 10036, seen once with an empty comment and left deliberately unexplained.

Two fields rather than one, and the second is the one callers actually use.
`transmitted` asks whether the request reached the server; **`booked` asks
whether anything exists at the venue because of it**, which is what decides
whether a resend can open a SECOND position. Six of the eight are
transmitted-yes and booked-no, which is the safe combination, so collapsing
them loses the distinction. `retry` is five-valued for the same reason: four
codes mean "resending THIS request is futile, a corrected one is fine", and a
boolean licenses either a pointless loop or a missed recovery.

**The action changes the answer for 10031.** On a close it is retryable now —
the position is still there and closing it twice is not a second position. On
an open it is not, because the order may have landed.

**10016 at this broker is a clock, not a geometry fault.** All eight order
refusals fall in the single minute 21:00–21:01 UTC, and *every* order attempt
in that minute was refused — 8 of 8, against 2 of 1,141 at every other
minute. 21:00 UTC is 00:00 server time, which is rollover, where the M1 range
is already measured as exceeding the 1.5×ATR bracket 14.3% of the time. The
venue is rejecting stops it cannot honour inside a band that wide. The
brackets in those rows straddle their entries correctly, so the request was
never the problem.

That is a validity argument, not a cost one, and the two do not trade off —
the rollover-hour filter was retired on cost grounds and this is separate. A
refused order is not an expensive trade, it is no trade at all, and eight
passes spent being refused observed nothing.

**10025 is not a failed repair, and treating it as one cost a position.**
`NO_CHANGES` means the venue already held the levels being asked for, so the
bracket was correct. `place()` treated anything that was not DONE as a failed
repair and fired the emergency close: EURUSD, 2026-09-07, the only 10025 in
the ledger, closed for no reason. `MOOT` is its own class for exactly this.

## Read the fill, never the quote

One live trade was a loss fixed at order time, and the order log hid it for
three days by recording the requested price as the entry. `place()` computed
sl/tp from the quote it read before `order_send` and transmitted them as
ABSOLUTE levels; position 10200315596 was an NZDUSD sell quoted at 0.59752
with tp 0.59638, and it filled at 0.59473 - 279 points away, against a worst
case of 3 points across the other 26 trades. That put both exits above a
short's entry, so every branch was a loss; it closed on its own take-profit
for -165 points, -1.65 USD, -1.45R, and the retcode still said DONE.

`bracket_is_sane()` now checks that the stop and target straddle the actual
fill, and `place()` re-anchors the bracket on the fill when they do not,
flagging the trade as `bracket_repaired` so analysis can drop it. Repair
rather than close: closing would only ever fire on adverse slippage and would
bias the record the tool exists to measure. A modify that fails closes the
position instead, because holding it has no profitable branch.
`place()` now records `fill_price` and `slippage_points` on every order, and
`track_record.py` re-derives the same check retroactively, so trades opened
before the fix are still flagged (`bracket_inverted_at_fill`).

The general form is the one this repository keeps rediscovering: a figure the
tool chose is not a figure the server confirmed, and logging the first as
though it were the second makes the discrepancy invisible rather than absent.

The cause is neither slippage nor a stale quote, and it took the bar data to
settle. The deal stamp renders to server 00:04:15 and the broker is UTC+3, so
the fill landed at 21:04:15 UTC against an order logged at 21:04:14 UTC - one
second, no delay to go stale in. Both prices were live: the M1 bar covering
that minute runs high 0.59752, low 0.59470, a 282-point range. The tool read
the top of that band and the fill came from the bottom of it.

Server 00:00 is rollover, and the rollover window is where quote bands stop
fitting inside a bracket. Measured over 20,000 M1 bars per major, the share of
bars whose range exceeds the 1.5xATR bracket is 0.9% in server hour 00 against
roughly 0.0% in every other hour, and inside that hour it is 14.3% at minute
00, 6.1% at 01, 8.2% at 05, decaying to 0.0% by minute 11 and 0.2% across
00:12-00:59. The feed is otherwise unremarkable - median M1 range is 8 points
and only 0.1% of bars exceed 114 - so this is a narrow window, not a bad feed.

That sharpens the hour-filter result rather than overturning it. The earlier
finding stands on its own terms: skipping the rollover hour cannot pay for
itself, because it carries 1.0% of entries and the effect there is a 4x
spread. But a bracket inverted at the fill is not an expensive trade, it is a
broken observation - the trade's outcome carries no information about the rule
that opened it. Cost arguments average out and validity arguments do not, so
the two do not trade off against each other. n is 1 in 27 live trades here;
the mechanism is established from the bar data, the frequency is not.

## Before changing the lot size

`lot_for_risk()` sizes a position so its stop costs a fixed sum, and
`--risk-usd` on `mt5_paper.py` and `take_profit.py` turns it on. It is OFF by
default: every figure in the live record above was taken at a flat 0.01 lot,
and a default that silently restated them would make the history unreadable.

The reason to size is that a flat lot is seven different bets. A 1.5xATR stop
is 131 points on EURUSD and 98 on NZDUSD, and a point is not worth the same
money in either, so 0.01 lots everywhere risked 1.67 on USDCHF against 0.98 on
NZDUSD - a 1.7x spread, structurally the metals-points error wearing a lot
size. It is what makes the R-multiple a measured quantity rather than a
derived one.

It does not improve expectancy and cannot, which is the part worth saying out
loud because it is the thing most often asked of it. Volume is a positive
multiplier on the per-trade result: it scales +0.021R and -1.00R by the same
factor, and every t-statistic in `track_record.py` is scale-invariant, so no
lot schedule moves a result from "not significant" to "significant". Bigger
lots make the currency figure bigger in whichever direction it was already
going.

A symbol whose tick value or tick size is missing is REFUSED rather than sized
from a default, on swap.py's reasoning - a lot guessed from an absent tick
value is a real order for the wrong amount. The volume step rounds down, never
up, so rounding cannot risk more than the budget; when the broker's minimum lot
exceeds the budget the tool reports a gap instead of pretending the sizing
held.

The float floor is the defect this found, and it is the repository's recurring
shape again. `math.floor(raw / step)` on a step count built from a float ATR
turns a budget worth exactly five steps into 4.999999999 and then into four -
so a 5-step order and a 4-step order send identical volume, with nothing in the
record to say which was intended. Round the ratio before flooring. The
arithmetic was right at every step and the number that reached the server was
still wrong, which is the same class as reading the quote instead of the fill.

## Before setting a consecutive-loss cap

`--max-consecutive-losses N` pauses NEW entries after N losing trades in a
row. It is OFF by default, on the same reasoning as `--risk-usd` and
`--cost-swap`: every figure in the live record was taken without it.

**It pauses; it does not halt.** A halt suppresses the `--flat-by` flush, so
a streak that halted would leave the positions it produced unmanaged - the
abandoned book the flush exists to prevent, reached by a different road. Open
positions keep being harvested and the wind-down still runs.

Choose N from the arithmetic rather than from a round number. The venue record
over 476 trades runs an 80.5% win rate, so a loss is p=0.195 and three in a row
is p=0.0074 - about **3.5 occurrences in six days**, a real pause several times
a week. Five is p=0.00028, roughly one per 3,500 trades, against a longest
observed run of **7**. So 3 reacts to ordinary variance and 5 reacts to an
outlier, and neither is wrong as long as which one you asked for is deliberate.

A streak is counted per closed POSITION, not per deal - one position produces
an entry deal and an exit deal, and an open position already has an entry deal
sitting in history at profit 0. A break-even breaks the streak: a trade that
cost nothing is not evidence the rule is failing.

## Before running an overnight session

`run_overnight.py` wraps `take_profit.py` and is the thing that actually
trades. Three of its behaviours are defaults rather than options, and one of
them books losses.

**The session ends flat.** `--flat-by` is passed for the same hour the session
stops at, so whatever is still open then is closed at what it is worth, losses
included. Over the final `--relax-over` minutes (45 by default) the profit
floor decays to zero so each position closes at the best moment offered rather
than all at the deadline, and nothing new opens inside that window.

This is the harvest loop's own doing and not tidiness. Closing at the first
sign of profit books winners and holds losers, so the positions still open at
the deadline ARE the losing tail - measured 2026-09-07, 7 positions with 6
underwater at -9.25 floating. Leaving them carried that tail into the next
session's `--max-positions` count and another night of swap.

**A risk halt is not the deadline.** `--max-daily-loss` firing at 22:00 stops
trading and does NOT flush; the deadline is the deadline. The flush also runs
AFTER the loop rather than inside it, because with `--flat-by` set to the stop
hour the loop breaks one interval early and no pass ever begins after the
deadline - the in-loop branch alone would have closed nothing and reported a
clean finish.

**It holds the machine awake, and on this host the thing that decides
whether that matters is the CHARGER.** A session closes nothing while the
laptop is asleep. Without the hold
the deadline arrives with the process suspended and the log simply stops
mid-evening, every position open, no error to explain it. It holds the SYSTEM
and, on S0, the display too; it releases in a `finally`, and it does not
defeat closing the lid - which is not an idle timeout, so a closed lid still
suspends the session.

Measured across the 38 session logs on this machine with
`tools/session_gaps.py`, which reads the ladder of `pass N` lines and names
every gap:

| session | span | gaps | lost | hold in force |
|---|---|---|---|---|
| 2026-09-07 23:06 | 6.67h | 0 | - | system only |
| 2026-09-10 22:46 | 7.21h | 0 | - | system only |
| 2026-09-11 22:50 | 7.16h | 0 | - | system only |
| 2026-09-14 23:16 | 6.66h | **4** | **239 min** | system only |
| 2026-09-15 20:10 | 8.93h | **1** | **112 min** | **system AND screen (S0 fix)** |

**THE S0 FIX DOES NOT WORK, AND THE CHARGER IS NOT THE ANSWER. Measured
2026-09-21 on a LIVE session.** This section said the opposite for one day
and every word of that is retracted below, because the machine did the thing
it was documented as unable to do.

`overnight-20260921-191852` was on mains, with the execution power request
and the screen hold both in force. Its last pass was **20:30:33**; it resumed
at **21:11:38** and printed

    THE MACHINE SLEPT: 41 minutes passed between passes, not 20s ...
    It was ON MAINS -- the execution power request did NOT hold

The Windows System log says the same thing from the other side, and it is the
evidence that settles this rather than the harness's own inference:

| time | event | message |
|---|---|---|
| 20:30:45 | **506** | The system is entering Modern Standby |
| 21:11:39 | **507** | The system is exiting Modern Standby |

And at that moment `powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE`
reported `Current AC Power Setting Index: 0x00000000` -- AC sleep **is** set
to never, and the host entered Modern Standby anyway, on mains, with the hold
held. Seven positions were open and unmanaged for 41 minutes.

So the three claims made here on 2026-09-20 are refuted:

  * "on mains this host cannot sleep at all" -- **false.** It just did, with
    the idle timeout at never.
  * "the S0 fix is not disproven, and cannot be tested on AC" -- **it is now
    disproven.** It was tested by the machine rather than by us, and the hold
    did not prevent the transition.
  * "the operational rule is the charger, not the code" -- **false.** The
    charger does not prevent this. `preflight.py` FAILING on `on_ac_power` is
    still right, because battery is strictly worse, but it is no longer
    sufficient and must not be read as the control.

**What is NOT established is the mechanism.** `standby-timeout-ac = 0`
governs the IDLE transition, and something else put the host into S0 -- lid,
an OEM policy, a maintenance window, a user action. Nothing here identifies
which, and the earlier Armoury Crate finding (the `never` is re-applied
across all four schemes and survives `powercfg /change`) is about the setting
holding, not about what bypassed it. Do not fill that in from memory.

This also undercuts the attribution in the table above. The 239-minute and
112-minute gaps were recorded as "almost certainly on battery" on the
reasoning that a mains session could not sleep. That reasoning is now known
to be wrong, so those two remain unexplained rather than explained -- the
power source at the moment of the sleep was not recorded for either, which is
exactly why the banner now re-reads it AT the sleep. Tonight's line is the
first time that re-read has actually answered the question, and the answer
was the unwelcome one.

**The operational consequence.** A session on mains can lose 41 minutes
and nothing prevents it. What survives the gap is the harness: it DETECTED
the sleep, said so, re-established the venue and harvested on the next pass.
Treat detection and recovery as the control, not prevention, and do not read
an unbroken ladder as evidence the hold works -- it is evidence the host
happened not to sleep.

**What a blind window actually costs, which is less than "unmanaged"
suggests and is not zero.** `mt5_paper.place` sends `sl` and `tp` WITH the
order, as absolute levels at `ORDER_TIME_GTC` (`tools/mt5_paper.py:538`), so
the bracket lives at the VENUE and is enforced while the harness is asleep.
A sleeping session is not a naked book. Tonight measured it: across the 41
minutes, balance did not move at all (99,927.63 both sides, so no stop or
target was touched) and equity went 99,919.98 to 99,919.29 -- **69 cents on
seven positions.**

Three things are genuinely lost, and only the third is expensive:

  * **Harvests.** The loop cannot close at the profit floor, so a position
    that would have been booked at +0.50 can give it back. Bounded below by
    the stop.
  * **Entries.** None open. At a measured negative expectancy this is a
    saving rather than a cost, which is worth saying plainly.
  * **The deadline.** This is the one that hurt. A sleep spanning 06:00
    means the flush does not run when it should, and
    `overnight-20260915-201059` is the worked example: it woke near the
    deadline, was refused on all seven closes with retcode 10031, and did
    not go flat until 08:13. `--flat-by`'s retry budget was made
    wall-clock-based on 2026-09-18 for exactly this, and a mid-flush sleep
    now restarts the budget rather than spending it -- untested against a
    real mid-flush sleep.

So the risk to price is not "what can the market do to seven open positions
in 41 minutes" -- the bracket answers that. It is "what happens when the
blind window overlaps the flush", and that is a much narrower question.

`--paper` is the safe way to run any of this: it sends no orders, so a
sleep costs an abandoned book nothing, and the weekend guard exempts it for
the same reason it exempts `--harvest-only`.

**A second session is refused.** The guard is a `psutil` scan of local
processes and cannot see another machine; `psutil` absent, it prints one line
and proceeds unguarded. Two laptops on one account share the balance, the
margin and the daily loss limit. See `SECOND_MACHINE.md`.

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
touching `tools/rule_backtest.py`, `tools/bracket_sweep.py`, the order
construction or history windows in `tools/mt5_paper.py` and
`tools/mt5_account.py`, the ledger's provenance filters in
`tools/track_record.py`, or the wind-down and keep-awake logic in
`tools/take_profit.py`, and `python tests/test_swap.py` after touching the
unit conversion, the night count or the plausibility fence in `tools/swap.py`.
The bracket-sanity and fill-recording checks are in `test_rule_backtest.py`
too, since they are order construction.
Run `python tests/test_crash_report.py` after touching
`tools/crash_report.py`, `tools/console_guard.py`, the PASS_HOOK and
STOP_REQUESTED wiring in `tools/take_profit.py` and `tools/run_overnight.py`,
or `run_overnight.weekend_deadline` and `run_overnight.finishing_status` --
the venue-week guard and the status a finished session records,
and `python tests/test_risk_gate.py` after touching `tools/risk_gate.py`, the
`gate` parameter on `mt5_paper.place`, the close recorder in
`mt5_paper.close_own`, or `APPROVED_UPSTREAM` in `app/brokers/mt5.py`.
Run `python tests/test_cli.py` after touching `tools/cli.py` or `build_exe.py`
-- the command table is read by both, and both ways of breaking it are silent:
a command naming a module that does not exist is fine until someone types it,
and a module missing from the table is simply absent from the executable with
no build error.
All ten run in CI on every push, against Python 3.10, 3.12 and 3.14.

On 3.10 the risk engine cannot be imported at all -- `app/risk/engine.py` uses
`StrEnum` and `datetime.UTC`, both 3.11 -- so `risk_gate` reports itself
unavailable there and **refuses every opening order**. That is the intended
behaviour and the reason the test runs on 3.10 rather than skipping it: a fence
that disappears on the interpreter that cannot load it is not a fence. A close
is never refused on any interpreter.

`PROJECT_STATE.json` is **generated, not hand-written**. Run
`python tools/project_state.py --write` rather than editing it, and
`python tests/test_project_state.py` after touching the generator. The
hand-written version drifted from the database on six counts at once -- it
reported 0 datasets against 1 READY dataset of 638 rows, 0 training runs
against 3, and repeated the claim that `market_bars` was empty when the table
held 705 bars. Each of those was true when written, which is the whole problem.

The same phrasing trap is worth avoiding in prose. "`market_bars` is empty" is
a claim about a mutable table and went stale on the first ingestion, in 69
lines across 35 files. State the REQUIREMENT instead -- correlation needs a
common window across two or more instruments -- because that stays true until
the thing that actually matters changes.

The MT5 server clock is not the local clock. Bound a history query with
`mt5_paper.history_end()` and `server_now()`, never `datetime.now()` — a local
upper bound drops every deal the server stamped later, returns no error, and
reads as "no trades" instead of a fault.

`server_now()` is right for bounding and wrong for day boundaries, and the
distinction is not obvious. MT5 encodes a stamp as the server's wall clock
rendered as a UTC epoch, and the Python package reads a naive query bound
through the LOCAL zone - so `datetime.fromtimestamp(tick.time)` cancels the
offset on both sides and the query is correct. Calling `.replace(hour=0)` on
it does not cancel: it lands on midnight of the local-rendered clock, so on
this UTC+5:30 machine `--max-daily-loss` counted from server 18:30 the
previous day, and the same code on a UTC machine counted from midnight. Use
`server_day_start()` for the day; a risk limit whose day moves with the
operator's timezone only fails on someone else's laptop.

The disk cache sweeps entries older than 7 days on first write of each
process. `python tools/market.py --prune` runs it on demand.
