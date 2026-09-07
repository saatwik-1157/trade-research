# PORTFOLIO_RISK_REPORT.md

Level 53, 2026-09-06. **Measured against the running deployment.** Where a
figure does not exist, this says so instead of estimating it.

---

## The account

| | |
|---|---|
| Open positions | **1** — EURUSD short, 0.01 |
| Orders on record | 271 |
| Trades on record | 252 |
| Executions | 209 |
| Trading mode | `paper` · `live_trading=false` |

252 of those trades and 270 of those orders were **imported from real MT5 demo
ledgers**. One order was created today by the automated path — the first ever —
and it is `failed`, because the simulator has no quote.

## Aggregate exposure

**One position, so aggregation is arithmetic rather than analysis.** Gross and
net are the same figure at n=1 and the number is not interesting; what matters
is that the machinery producing it is correct and now feeds risk.

Reported honestly by `exposure.compute`:

* gross and net are separate figures and never conflated
* notional comes from measured contract terms, and a symbol without them is
  reported **uncomputable**, never zero
* currency exposure comes from symbol metadata, not from splitting a ticker

## Correlation

**Not available, and not estimated.** `market_bars` covers one instrument, so there is no
common window across instruments. `exposure.correlation_note()` says exactly
this and offers the currency breakdown as the one real proxy for common-factor
risk.

This is not a gap opened by this level — it is the pre-existing honest answer,
and it predates L53.

## Drawdown

Computable from 252 real trades and **is** computed
(`PortfolioService.drawdown_for`, peak equity from the equity curve). It is not
reproduced here as a headline number, for the reason `CLAUDE.md` documents at
length: the live record contains **two incompatible bracket regimes** (reward:risk
0.2 and 1.0) and pooling currency across them is the same class of error as the
metals-points mistake. The R-multiple pools; the currency does not.

## Capital utilisation

| | |
|---|---|
| Paper account balance | 100,000 |
| Margin used | reported by the account row |
| Capital at risk | **effectively nil** — one 0.01-lot position |

**No recommendation to increase utilisation is made.** Rule 12 forbids
automatic risk increases, and there is no evidence base for a manual one: this
repository's own research finds no strategy here that separates from a coin
flip.

## The risk state the engine actually sees

**This is the finding of the level.** Before today:

```
PortfolioState(equity=None)   # everything else default
```

After: the account's real portfolio, via
`PortfolioService.to_risk_state()` — which already existed and was read only by
a display route.

### What that changes, concretely

The deployment holds an open EURUSD short. `one_position_per_symbol` is on by
default. Before the fix a EURUSD **buy** signal was approved and became an
order. After it, the same signal is vetoed: *"EURUSD already open"*.

### What it does not change

Seventeen of the twenty limits in `RiskLimits()` are **unset** on the deployed
engine, and the account's configured limits are still not loaded on the
automated path — `app/main.py` builds `RiskEngine(RiskLimits())` and the
per-pass replacement its own comment describes was never written. **Supplying
the portfolio makes those limits enforceable; it does not configure them.**

That is the next piece of work and it needs a decision about what the limits
should be, which is not a decision this level should make.

## Portfolio safety states (Phase 14)

`NORMAL / CAUTION / RESTRICTED / HALTED / SAFE_MODE` were **not** created as a
new state machine. Two already exist and are deterministic:

* `SafeMode` — latched, with reasons, blocking new orders at three server-side
  paths.
* `account_state.health_of(...)` — the portfolio view's own health, derived
  from account freshness, reconciliation and margin utilisation.

Adding a third overlapping state machine would give the platform three partial
answers to "should it be trading". The gap worth recording is that
`health_of`'s verdict is **reported and not enforced** — the same shape as the
defect above, and the honest next candidate.

## Unresolved

1. **17 of 20 risk limits are unset** on the automated path, and account limits
   are not loaded there.
2. **`open_symbols` cannot express "unknown"** — the type-level cause of the
   defect above. Fixing it makes `one_position_per_symbol` fail closed
   everywhere; it is a behaviour change requiring approval.
3. **Risk reservations are in-memory** (`RiskService._reservations`), with no
   table. Two concurrent orders are guarded within one process; a restart
   between reserve and fill loses the reservation.
4. **`health_of` is not enforced**, only displayed.
