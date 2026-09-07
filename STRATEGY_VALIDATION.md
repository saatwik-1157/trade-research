# STRATEGY_VALIDATION.md

The gates a compiled TradingView strategy passes before anything runs it, what
each one proves, and what VALIDATED does not mean. Brief §13, §14, §15, §32.

---

## 1. The thing this document exists to prevent

A TradingView strategy arrives with a number attached. The Strategy Tester
reports net profit, a profit factor and an equity curve, and the number is
usually good — that is generally why someone brings the strategy.

**Those figures are a claim, not evidence.** TradingView's tester, at its
defaults, charges no spread and no commission; `process_orders_on_close` and
`calc_on_every_tick` change fill timing; and `request.security` repaints. The
one effect this repository has ever measured at significance is cost drag: at
this broker's median recorded spreads, `rsi_reversion` loses 1.50 points a
trade and `sma_cross` 6.54, and the only t-statistic to clear 1.96 across the
whole research programme was the `random` rule *losing* at −3.60.

So the gate ladder is not "reproduce the good number". It is: recompute the
strategy's behaviour under this platform's cost model, and then ask whether
what remains separates from a shuffled version of itself. The answer for
everything measured here so far has been no, across 41 candidates, 10 rule
families, 8 exit structures, 4 universes and 3 timeframes.

Anything that says otherwise about a newly imported strategy is interesting
precisely because it would be the first.

## 2. The ladder

Each gate has a state in brief §32's lifecycle. A gate cannot be skipped and
none can be reordered: the orchestrator refuses a transition whose predecessor
did not pass (TRADINGVIEW_ARCHITECTURE.md §4).

| Gate | State reached | What it proves | Reuses |
|---|---|---|---|
| **G1 Parse** | PARSED | the source is inside the declared subset; every unsupported construct is listed | A1 |
| **G2 Normalize** | NORMALIZED | symbol resolves through L11, timeframe is one of L08's eight, every indicator parameter is in range | `validate_for_trading`, `ParameterSpec` |
| **G3 Compile** | COMPILED | the definition passes `parse_definition`, units are comparable, and the preservation report has no undocumented change | L13 unchanged |
| **G4 Generated tests** | VALIDATED | the compiled strategy fires where the spec says it should and nowhere else | A4 generator, pytest |
| **G5 Backtest** | BACKTESTED | it runs over history under a stated cost model, and the three leakage tests hold | L14 `simulate()` |
| **G6 Statistical** | BACKTESTED (annotated) | whether the result separates from its own permutation null, across era blocks and a walk-forward | L26 / `tools/rule_search.py` |
| **G7 Replay** | REPLAYED | the incremental path produces the same trades as the batch path | L15 |
| **G8 Paper** | PAPER | it survives real data, real staleness and the risk engine, with virtual money | L16, L17 |
| **G9 Operator review** | APPROVED | **a human read the report** | — |

Autonomy runs G1 through G8 unattended. It stops at G9, permanently and by
design (§5 below).

## 3. What each gate actually checks

**G4 — generated tests (brief §13).** For every compiled strategy, A4 writes a
test module covering: each indicator against a fixture series; each entry
condition at a bar where it must fire and the adjacent bar where it must not;
each short condition; each exit condition; the bracket geometry through
`bracket_is_sane`; a bar series shorter than `warmup()` returning NO_SIGNAL; a
gap and a stale bar; NaN and zero-volume bars; and the same bar processed twice
producing one signal. A failure is diagnosed to parser, compiler, strategy
logic, existing platform code, or the test itself, and the *actual* cause is
fixed. A test is never weakened to pass — standing rule 4 and brief §13 say the
same thing.

**G5 — backtest (brief §14).** `app/backtest` already computes total trades,
net P&L, win and loss rate, profit factor, expectancy, maximum drawdown,
average win and loss, risk/reward, exposure, trade duration and streaks, and
already returns NOT_AVAILABLE below 20 trades rather than a figure computed
from four. Sharpe and Sortino are the two the brief names that the metrics
module does not yet produce; they are added here with their assumptions stated
(annualization factor, risk-free rate, and the fact that both are close to
meaningless on fewer than ~100 trades).

Leakage is checked by construction, not by inspection: L14 has three
look-ahead tests over the prefix-walk signal builder, L15's engine is proved
trade-for-trade identical to `simulate()`, and `Candles.closed` drops the
forming bar before a strategy sees it. The costs charged are the platform's
own — median recorded spread, not a live quote, because a single live quote
understates this broker by 3–8× and bars recording zero spread are unrecorded
rather than free.

**G6 — the statistical gate, and why it is separate from G5.** A profitable
backtest is the weakest of the three signals available. G6 runs, over the
compiled strategy's own signals:

- a **permutation null** — shuffle the strategy's own signals, preserving trade
  count and buy/sell mix, so the null pays the same spread and the comparison
  isolates timing rather than exposure;
- **era blocks** — the result cut into sequential periods, because a single
  out-of-sample split puts the same recent era in every holdout and calling it
  three confirmations is one observation counted three times;
- a **walk-forward** re-ranking on only the eras before each test era;
- **date clustering** — one shared dollar move opens correlated trades in
  several symbols at once, and the date-clustered t is the one to report;
- a **unit check** — if median ATR in points spans more than 5× across the
  symbols pooled, the pooled figure is not a quantity and only `per_symbol` may
  be quoted.

`tools/rule_search.py` implements all five and they are on by default. G6 does
not gate on "significant"; it gates on **reported**. A strategy with a null it
cannot beat still reaches PAPER — it just reaches it carrying that sentence.

**G7 — replay.** Not a repeat of G5. It proves the incremental path and the
batch path agree, which is what makes a paper result comparable to a backtest
result. When L15 was built this test caught three real divergences (rounding,
the end-of-data close, unmatched financing), which is the reason it is a gate
rather than a formality.

**G8 — paper.** Real data, virtual money, the full pipeline: data → strategy →
AI seat → risk → sizing → OMS → fill → position → trade. What paper adds over
replay is everything historical data cannot contain: staleness, feed gaps, the
broker clock, restart recovery, and the risk engine's 26 checks against a live
account state.

**Reconciliation against TradingView's own numbers.** When the user supplies
the Strategy Tester "List of Trades" CSV, `tools/tv_import.py` parses it and
G5 reports the divergence: trade count, direction agreement, and expectancy gap
per trade. A large gap is not a failure — it is usually TradingView's zero
spread meeting this platform's measured one, which is the single most useful
number the whole import produces. It is reported, never reconciled away.

## 4. The validation report

One artifact per strategy version, written to `reports/tv_validation_<hash>.json`
and summarized at the API. It carries every gate's verdict, the preservation
report, the statistical output including the null's best score, the TradingView
divergence, the cost model, the data window, and the reproducibility
fingerprint L14 already computes. A gate that did not run says
`not_run`, never `passed`.

## 5. What VALIDATED does not mean

- **Not "approved to trade".** It means the compilation is faithful and the
  measurements have been taken. Every strategy stays `research_only`; the tier
  ladder is `research_only → paper_approved → live_approved` and raising a tier
  is a deliberate act backed by a report, which the registry's
  `StrategyNotAvailable` message already says in those words.
- **Not "profitable".** The report may say the strategy lost, and it still
  passes G1–G8 by having been measured honestly.
- **Not a route to live.** LIVE stays refused while any of the ten `LIVE_GATES`
  is false — seven are — and `TRADING_MODE=paper`, `LIVE_TRADING=false` are the
  defaults every level has preserved. No validation result changes any of them,
  which is brief §38 and §40, and also the reason `provider_for` takes the mode
  as its only parameter.
- **Not permanent.** L29's model monitoring already escalates without ever
  promoting or replacing; a deployed strategy's live behaviour is compared to
  its validated behaviour, and divergence flags it. Nothing auto-retires a
  strategy either — that is also an operator act.
