# DYNAMIC_RISK_BUDGETING.md

L55, 2026-09-06. **The constraint layer was built. The optimizer was not, and
this document is the reason.**

---

## What L55 asks

> "Given the current portfolio state, available capital, risk constraints,
> strategy performance, correlations, market regime and exposure, how should
> the EXISTING approved risk budget be distributed?"

## Why that question cannot be answered here yet

Every input the allocation model needs is either absent or has been measured as
noise **by this repository's own research**:

| Input L55 Step 5 asks for | State |
|---|---|
| Strategy performance, expectancy, profit factor | `CLAUDE.md`: `rsi_reversion` and `sma_cross` do not separate from a coin flip; at measured spreads both are net negative (−1.50 and −6.54 points/trade) |
| Sharpe / Sortino / Calmar | Computable, but from 27 live trades spanning **two incompatible bracket regimes** (reward:risk 0.2 and 1.0). `CLAUDE.md` is explicit that currency cannot be pooled across them |
| Recent performance | Live record: **+0.023R over 27 trades, pooled t = 0.26, date-clustered −0.80** |
| Correlation | **Unavailable.** `market_bars` covers one instrument; `exposure.correlation_note()` has said so since before this level |
| Market regime | **No regime engine exists.** Step 8 says do not invent a second one — there is not a first one |
| Trade count per strategy | One strategy has ever produced a signal on the automated path, and it produced its first order today |
| Execution quality, slippage | One order exists from the automated path, and it is `failed` |

L55 Step 6 anticipates exactly this — *"do not pretend that insufficient data is
strong evidence"*, *"if insufficient data exists, use baseline allocation"*.
**Followed honestly, every answer the optimizer could give today is "insufficient
data, use baseline."**

An optimizer that distributes capital between strategies on these inputs would
be a machine for turning noise into position sizes. `CLAUDE.md` exists to
prevent precisely that, and its own bracket sweep found the **random** rule
scoring higher in-sample (1.76) than the best real candidate (0.83) — a direct
demonstration of what optimising on this data produces.

## What was built instead

**The half of L55 that is safety rather than optimisation**, because it is
useful before an optimizer exists and essential the moment one does.

### `conserves_budget()` — the parts may never exceed the whole

L55's mandatory safety test, enforced:

```
approved 10,000 · recommended 15,000  ->  REJECTED ("5000 over")
approved 10,000 · recommended 10,000  ->  allowed
approved 10,000 · {a: -5000, b: 15000} -> REJECTED (negative share)
```

* **Rejected whole, never trimmed.** Scaling somebody's proposal down and
  applying it is a decision the caller did not make.
* **No tolerance, no rounding.** `Decimal` is exact; a budget that "nearly"
  fits does not.
* **Negative shares refused explicitly** — `{a: -5000, b: 15000}` sums to
  10,000 and would otherwise pass while creating budget elsewhere.

### `capital_reservations` — budget that survives a restart

See `RISK_BUDGET_HIERARCHY.md`. The gap it closes: reservations were a
process-local dict, so an approval that reserved and had not filled when the
process died released nothing, and the next process believed the budget free.

### `app/execution/limits.py` — the automated path joins the hierarchy

The pipeline evaluated every signal against `RiskLimits()` with 17 of 20 limits
unset, and never loaded the account's configured limits. It does now, through
the existing `RiskService`, carrying the kill switches across. **It can only
tighten** — combination takes the more restrictive at every field.

## The order these must be built in

1. ~~Budget conservation invariant~~ — **done**, and it is what makes any
   future optimizer safe by construction rather than by review.
2. ~~Durable reservations~~ — **done**; an optimizer allocating against a
   budget that a restart resets is worse than none.
3. **Configure the limits.** Seventeen are unset. There is nothing for an
   allocator to allocate *within* until an operator sets them, and choosing the
   numbers needs evidence.
4. **A market-data feed.** Correlation and regime both need it, and so does the
   ATR bracket policy (`SIGNAL_ROUTING.md`). It is the single unlock for the
   largest number of blocked things.
5. **Then** an allocator — starting with the simplest defensible method
   (equal-weight, or inverse-volatility if volatility is measurable), and
   compared against baseline in paper before anything else.

## What is explicitly NOT authorised

```
AUTOMATIC RISK-BUDGET EXPANSION:        NOT AUTHORIZED — and now unenforceable
                                        by construction: conserves_budget()
AUTOMATIC LEVERAGE INCREASE:            NOT AUTHORIZED
AUTOMATIC STRATEGY/MODEL PROMOTION:     NOT AUTHORIZED
AUTOMATIC PORTFOLIO RECONFIGURATION:    NOT AUTHORIZED
```

`TRADING_MODE=paper`, `LIVE_TRADING=false`, verified unchanged.
