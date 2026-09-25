# PORTFOLIO_INTELLIGENCE_ARCHITECTURE.md

Level 53, 2026-09-06. **This is an audit, not a build.** Most of what L53 asks
for already existed; the level's value was finding that it was never connected
to anything that enforces it.

---

## What already existed (KEEP)

| Component | Where | State |
|---|---|---|
| Exposure: gross/net, by symbol/strategy/bot/currency | `app/portfolio/exposure.py` | **Complete** |
| Notional from measured contract terms | `exposure.notional_of` | **Complete** — refuses rather than guessing |
| Concentration | `ExposureReport.concentration` | **Complete** |
| Open risk aggregation | `exposure.open_risk` | **Complete** |
| P&L, realised/unrealised | `app/portfolio/pnl.py` | **Complete** |
| Drawdown, peak equity | `PortfolioService.drawdown_for` | **Complete** |
| Margin utilisation | `pnl_engine.margin_utilisation` | **Complete** |
| Account state, freshness | `app/portfolio/state.py` | **Complete** |
| Analytics: equity curve, metrics, windows | `app/analytics/` | **Complete** |
| Risk reservations (race guard) | `RiskService._reservations` | **In-memory only** |

**Nothing here was rebuilt.** The exposure module in particular is better than
what L53 asks for: it already refuses to compute a notional without a
`ContractSpec`, and its docstring cites this repository's own +4,236-point
metals error as the reason.

## Correlation — already honest

`exposure.correlation_note()` reports correlation as **unavailable**, names why
(`market_bars` covers one instrument), and offers the currency breakdown as the one real
proxy for common-factor risk. That is exactly what L53 Phase 8 asks for and it
predates this level. **No correlation engine was built**, because building one
without market data would be fabricating the thing the brief forbids.

---

## The defect this level found

**Portfolio risk was computed, displayed, and never enforced.**

`PortfolioService.to_risk_state()` returns precisely the fields
`app.risk.PortfolioState` reads — equity, balance, margin, open positions, open
symbols, realised P&L, trades today, peak equity, exposure by currency, largest
position. **Its only caller was `GET /v1/portfolio/...`, a route that displays
it.**

Both execution paths passed one field:

```python
PortfolioState(equity=self.equity)      # app/execution/pipeline.py:476
PortfolioState(equity=body.equity)      # app/api/v1/orders.py:287
```

So every portfolio-level limit was unenforceable — not because the numbers were
wrong, but because **the engine was never told them.**

### Reproduced on the running deployment

The deployment holds an **open EURUSD short**. `one_position_per_symbol` is
`True` in `RiskLimits()`, which is exactly what `app/main.py` builds. A
TradingView alert for EURUSD **buy** was routed, approved and turned into an
order.

Isolated, same engine and same order:

```
PortfolioState(equity=None)                 -> approve
PortfolioState(open_symbols={'EURUSD'})     -> veto: EURUSD already open
```

### Why this one and not the others — the type-level cause

Every field on `PortfolioState` is `| None` and defaults to None, and the
engine's rule is *"a None that a limit needs produces a veto rather than an
assumption"*. That rule holds, and it is verified:

| Limit configured against an empty portfolio | Verdict |
|---|---|
| `max_open_positions` | **veto** |
| `max_daily_loss` | **halt** |
| `max_drawdown_pct` | **veto** |
| `max_trades_per_day` | **veto** |
| `one_position_per_symbol` (default) | **approve** |

`open_symbols` is `frozenset[str]` defaulting to `frozenset()`. **It has no
value meaning "unknown"**, so an empty set reads as the positive claim
*"nothing is open"*.

The only aggregate control enabled by default is therefore the only one that
fails **open**. Every other limit in this engine fails closed.

---

## The fix (ADD, minimal)

`app/execution/portfolio.py` — a snapshot provider, mirroring
`app/execution/store.py`. It is the wire between two things that already
existed:

```
PortfolioService.build() -> to_risk_state() -> PortfolioState -> RiskEngine
```

* Wired at the one place the deployed pipeline is built, and a test reads
  `app/main.py` to assert it — because "built and never wired" is the defect
  this level found and would be the defect the fix repeated.
* **No cache.** The portfolio engine aggregates; it does not own. A cache here
  would be a second answer to "what is open" that could disagree at exactly the
  wrong moment.
* **Failure returns `{}`**, giving an all-None state — the conservative
  direction, since a None a limit needs vetoes. It never falls back to a stale
  snapshot: a stale portfolio that permits an order is worse than no order.
* **Non-paper accounts return `{}`** and say so. A broker account's equity is
  the *broker's* figure and reading it means a venue call; putting a network
  round-trip between the risk decision and the order is a design decision
  nobody has taken, so it is recorded rather than assumed.

**Regression:** `tests/test_portfolio_risk.py`, 15 tests, including the pre-fix
behaviour pinned (`test_without_the_portfolio_the_same_signal_is_approved`) and
the type-level cause asserted directly.

---

## What was NOT built, and why

| L53 asks for | Why not |
|---|---|
| Cross-strategy correlation (Phase 5, 8) | `market_bars` covers one instrument. Already reported as unavailable, honestly, by code that predates this level. |
| Regime intelligence (Phase 12) | Needs the same market data. No regime classifier can be fitted to no bars. |
| Portfolio stress tests against a venue (Phase 20) | No venue has ever been connected. |
| Signal collision detection (Phase 6) | Two strategies have ever produced signals here, and the automated path had never produced an order until today. There is nothing to collide. |
| A second risk engine | Forbidden, and unnecessary — the existing engine takes all of this as input. |

**`open_symbols` giving up its empty-set default** is the deeper fix and is
**not** made here. Changing it to `frozenset[str] | None = None` would make
`one_position_per_symbol` veto everywhere it is not supplied — the safe
direction, and a behaviour change across the paper engine, replay and every
caller that passes nothing. It is recorded as a recommendation requiring
approval rather than made at the end of a long session on the execution core.

---

## Source of truth (Phase 3) — unchanged and correct

Each figure names the system to go and ask; the portfolio engine aggregates and
does not own:

| Figure | Owner |
|---|---|
| Paper balance, equity | `paper_accounts` — the paper engine writes it |
| Broker balance, equity | the broker's own `get_account()`, never recomputed |
| Positions | `positions`, reconciled against the venue |
| Orders | the OMS, `orders` |
| Realised P&L | `trades` |
| Instruments | `symbols` + `symbol_mappings` contract terms |

**No competing portfolio state was created.**
