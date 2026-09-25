# RISK_BUDGET_HIERARCHY.md

How a limit at one level restricts the level below it. L54/L55, 2026-09-06.

**Written against the code, not the brief.** Where a level of the hierarchy has
no implementation, this says so rather than describing an intention.

---

## The hierarchy, as it actually exists

```
ACCOUNT      risk_rules / risk configuration -> RiskService.configuration()
   |         EXISTS, and the API order path uses it
PORTFOLIO    PortfolioState -> the RiskEngine's aggregate checks
   |         EXISTS; wired into the automated path at L53
STRATEGY     risk_rules scoped to a strategy
   |         EXISTS, combined by RiskService
BOT          bots.max_positions / max_daily_trades / max_daily_loss /
   |         max_risk_per_trade / cooldown_seconds  -> BotLimits.effective()
SYMBOL       risk_rules scoped to a symbol
   |         EXISTS, combined by RiskService
ORDER        OrderProposal -> RiskEngine.approve()
```

**The combination rule already existed and is the important part.**
`app/bots/limits.py` states it: `effective()` takes the more restrictive of the
bot's figure and the account's, **in every field, in one place**. A bot asking
for 5% when its account permits 2% gets 2% — not because a check rejected it,
but because the arithmetic cannot produce a looser number than either input.

That is why wiring more levels in is safe: **combination can only tighten.**

## What L55 fixed

Until today the automated execution path did not participate in this hierarchy
at all. `app/main.py` built `RiskEngine(RiskLimits())` — the bare default, with
**17 of its 20 limits unset** — and never replaced it. An operator who
configured an account daily-loss cap got it enforced on `POST /v1/orders` and
**not** on the TradingView path.

`app/main.py` has described the intended design since L22:

> The engine's limits are replaced per pass by the worker's caller once a bot
> configuration exists (L22).

**That caller was never written.** `app/execution/limits.py` is it. It resolves
the account's, the strategy's and the symbol's limits through the existing
`RiskService.limits_for` and builds a per-pass engine — **carrying the kill
switches across**, because a per-pass engine built without them would be one
that cannot be halted.

It cannot loosen anything: the resolved set is the combination, and combination
takes the more restrictive.

## What the budget is measured against

| Level | Field | Source |
|---|---|---|
| Account | equity, balance, margin | `paper_accounts` / the broker's own figures |
| Portfolio | open positions, open symbols, exposure by currency, peak equity | `PortfolioService.to_risk_state()` — wired at L53 |
| **Reserved** | risk claimed and not yet settled | `capital_reservations` — **added at L54/L55** |
| Strategy / bot | per-bot limits | `bots` columns, combined by `BotLimits.effective` |
| Order | proposed volume and risk | `OrderProposal` |

## Reservations: the level that did not survive a restart

`RiskService` has reserved budget since L17, in a process-local dict. Correct
for the race it was built for — two concurrent orders evaluated against one
snapshot both seeing the same headroom — and **empty after a restart.** An
approval that reserved and had not filled when the process died released
nothing, and the next process believed the whole budget free.

Same shape as the L45 C-1 defect fixed the same week: a guard whose state does
not outlive the process that made it.

`capital_reservations` (migration 0027) is the durable record.
**It does not replace the dict** — the dict stays as the fast path inside one
process and is rebuilt from the table, because two derivations of one fact
eventually disagree.

* `intent_id` is **UNIQUE**: a retried signal reserves once, the same backstop
  `orders.intent_id` gives orders.
* `risk_amount >= 0` and `exposure >= 0` are CHECK constraints, because a
  negative claim would **add** budget — the one direction this table must never
  permit, and a constraint is a better guarantee than every caller remembering.
* Expired reservations are excluded **by the query**, so a book nobody has
  swept is still correct.

## The invariant above all of it

`conserves_budget(allocations, approved=...)` — **the parts may never exceed
the whole.**

L55's mandatory safety test: 10,000 approved, 15,000 recommended → **REJECTED**,
and rejected *whole* rather than trimmed to fit, because a partially accepted
allocation is an allocation nobody chose.

There is **no tolerance and no rounding**. `Decimal` is exact and a budget that
"nearly" fits does not. An optimizer convinced the portfolio can support more
must ask a human to raise the limit, which is a different action with a
different approval.

Negative shares are refused explicitly: `{a: -5000, b: 15000}` sums to 10,000
and would otherwise pass.

## What is still not enforced

1. **The limits are wired; the limits are not set.** Seventeen of twenty are
   unset by default. Wiring makes them enforceable; it does not configure them,
   and choosing the numbers is an operator decision with evidence requirements
   this platform does not yet have.
2. **`open_symbols` cannot express "unknown"** — it is `frozenset[str]`
   defaulting to `frozenset()`, so `one_position_per_symbol` fails **open**
   while every other limit fails closed. Recorded at L53; fixing it is a
   behaviour change across the paper engine and replay.
3. **Reservations are not yet taken on the execution path.** The table, the
   book and the invariant exist and are tested; the pipeline does not call them
   yet. That wiring is the next step and is deliberately separate — L45 taught
   this project what happens when execution-core changes are stacked in one
   pass.
