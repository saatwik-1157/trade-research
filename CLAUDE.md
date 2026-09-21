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

**The operational consequence.** A session on mains can lose 41 minutes with
positions open and nothing prevents it. What survives the gap is the harness:
it DETECTED the sleep, said so, re-established the venue and harvested on the
next pass. Treat detection and recovery as the control, not prevention, and
do not read an unbroken ladder as evidence the hold works -- it is evidence
the host happened not to sleep.

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
