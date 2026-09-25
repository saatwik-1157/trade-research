# Portfolio management and exposure (L30)

Built 2026-09-04. What is held, what it is worth, and how sure we are of the
answer.

---

## 1. The one sentence

**The Portfolio Engine aggregates; it does not own, and it does not decide.**

Section 2 of the brief is explicit that this is not a replacement for the risk
engine, and §63 is the strict version of the same rule: it may never place an
order, modify a position, bypass sizing, bypass the OMS or enable live trading.
Both are structural rather than aspirational — `app/portfolio/` imports no order
manager, no sizer, no risk decision and no broker write path, and
`tests/test_portfolio.py` parses every module with `ast` to keep it that way.

```
       broker (L10)          paper engine (L16)        positions (L21)
            |                        |                       |
            +------------+-----------+-----------+-----------+
                         v                       v
                   AccountState              PositionView
                (source + freshness)      (joined to L11 specs)
                         |                       |
                         +-----------+-----------+
                                     v
                              PortfolioView
                    exposure · P&L · drawdown · margin
                       health · reconciliation
                                     |
                +--------------------+--------------------+
                v                    v                    v
          REST (14 GETs)     4 realtime events      to_risk_state()
                                                           |
                                                           v
                                                   RiskEngine DECIDES
```

The arrow into the risk engine points one way. Risk reads portfolio; portfolio
cannot reach risk, and the handoff is a plain mapping rather than the dataclass
precisely so that direction is enforced by the import graph.

---

## 2. The modules

| Module | What it holds |
|---|---|
| `state.py` | `AccountState`, `Freshness`, `PortfolioHealth`, `Source`, `Reconciliation`, `health_of` |
| `exposure.py` | `PositionView`, `notional_of`, `mark_to_market`, `Bucket`, `compute`, `open_risk`, `correlation_note` |
| `pnl.py` | `PnL`, `unrealized_of`, `Drawdown`, `from_curve`, `margin_utilisation` |
| `events.py` | `changes_between` — what one refresh implies, and nothing else |
| `service.py` | `PortfolioService`, `PortfolioView`, the account resolvers, `NOT_SUPPLIED` |
| `app/api/v1/portfolio.py` | 14 GET routes; no other verb exists |

Nothing new was added to the schema. `portfolio_snapshots` has existed since
L05 and is the equity history the drawdown reads, so **L30 ships no migration** —
§34 says not to duplicate what exists, and the table already had every column
this level writes.

---

## 3. Gross and net are different numbers

Sections 11 and 12. Long 50,000 and short 30,000 is **80,000 gross** and
**+20,000 net**, and every aggregate carries both:

```python
@property
def gross(self): return self.long_value + self.short_value   # absolute
@property
def net(self):   return self.long_value - self.short_value   # directional
```

A single "exposure" figure would understate a hedged book by more than half, and
which of the two it meant would depend on who wrote the line. A flat book —
equal long and short — is `net = 0` with `gross > 0`, and that is the case a
one-number display renders as no exposure at all.

---

## 4. Notional refuses rather than guesses

Section 13 warns that `quantity × contract size × price` does not work
identically for every asset, and this repository has the scar. `CLAUDE.md`
records the metals search that produced a +4,236-point "result" which was
arithmetic rather than a finding: median H1 ATR is 160 points in silver against
9,386 in palladium, so pooling adds numbers that are not the same quantity.

So `notional_of` requires the measured `contract_size` from L11's spec table and
**returns a refusal when there is none**:

```python
Notional(None, computable=False, reason="XAUUSD has no measured contract size…")
```

The refusal is counted, not dropped. `Bucket.uncomputable` and
`ExposureReport.uncomputable` carry the positions the total excludes, because a
total that silently omitted three positions would be wrong in a way nobody could
see.

`mark_to_market` goes through `app.symbols.precision.value_per_price_unit` —
the platform's single conversion. L18 moved that out of the paper portfolio
because position sizing had grown its own copy, and a third here would undo the
extraction.

---

## 5. Currency comes from metadata, never from the name

Section 14. A `EURUSD` long is EUR long and USD short — but only because the
symbol row says its base is EUR and its quote is USD. Splitting a six-letter
ticker in half works for the majors and is wrong for everything else, which is
the class of error §14 names.

When any held symbol lacks a recorded base or quote, **the whole breakdown is
withheld** and `by_currency` serialises as `null` with a note saying why. A
partial currency map reads as a complete one.

---

## 6. The trading day, and the bug it nearly reintroduced

Section 21 asks that the day boundary be defined explicitly. It already was:
L17's `app.risk.state.day_start`, and `CLAUDE.md` records at length why the
obvious alternative is wrong — MT5 renders a server stamp through the LOCAL
zone, so `.replace(hour=0)` lands on midnight of the operator's clock and a
daily loss limit counts from 18:30 the previous evening on a UTC+5:30 machine.

L30 reuses that function and **found the seam anyway**. `day_start` works in
aware UTC (the risk engine hands it `datetime.now(UTC)`); this engine works in
naive UTC, because that is what the database stores. Passing a naive value
straight through would have had `.astimezone(UTC)` read it as local time — the
identical bug, one conversion away. The service converts explicitly in both
directions and says so in a comment, and a parsed test asserts that no module in
`app/portfolio/` calls `.replace(hour=…)` at all.

---

## 7. The peak only ever rises

Section 22. `Drawdown.observe` has no branch that lowers `peak_equity`:

```python
if self.peak_equity is None or equity > self.peak_equity:
    self.peak_equity = equity
```

A peak recomputed from a rolling window would fall as the window passed an old
high, and the drawdown would shrink without the account recovering. The peak is
read from `portfolio_snapshots`, which is the whole recorded history rather than
a window, so a caller cannot cause a reset by asking for less data.

`recovered` is `None` when either figure is missing. "Not recovered" and "we
cannot tell" are different, and only the first is a reason to trade smaller.

---

## 8. Never fabricate

Section 59, and it shapes every type here.

| Situation | What is reported |
|---|---|
| No broker adapter connected | every figure `None`, `unavailable_reason` names it |
| Broker read raised | `unavailable`, with the exception type |
| Position has no live quote | unmarked — never valued at its entry price |
| Any position unmarked | unrealized is `None` for the WHOLE account |
| Contract size missing | notional uncomputable, counted, excluded from totals |
| Position has no stop | risk-to-stop is `None`, never `0` |
| No reconciliation ran | `checked=False, agrees=False` |
| Balance or equity absent | **no snapshot row is written** |

Two of these are worth stating twice.

**Unrealized is all-or-nothing.** A partial total is a number a reader treats as
the whole. The symbols that could not be marked are named beside it.

**Zero risk would mean the position cannot lose.** An unstopped position is
precisely the one whose loss has no floor, so reporting it as `0` inverts the
fact.

---

## 9. Freshness is part of the state

Sections 31 and 44. `Freshness` is computed from the age of the underlying
reading — `as_of` is when the SOURCE was read, not when the object was built —
and three values exist because "never read" and "read a while ago" are different
facts about the connection.

`PortfolioHealth` is a **precedence, never a score**:

```
ERROR > RECONCILIATION_REQUIRED > STALE > WARNING > HEALTHY
```

A disagreement about what is held outranks an old figure for something we agree
on. A weighted composite could be tuned until it hid whichever check mattered,
which is the same reasoning L26's verdict and L29's health state already record.

Every state carries its reasons, so a dashboard can say *why* rather than showing
a coloured dot somebody has to interpret.

---

## 10. Reconciliation compares; it never repairs

Sections 30 and 63. L21's `PositionReconciler.sweep` settles and repairs. This
engine reads the broker's positions, compares them to the platform's, and
reports the difference — because §63 forbids it from modifying a position, and
repair belongs behind L21's own route where an operator asks for it.

A paper account reports "no external venue to reconcile against", which is the
truth: the paper engine is itself the source of truth for it.

---

## 11. The risk handoff, and what this engine does not own

`to_risk_state()` returns the fields `app.risk.engine.PortfolioState` reads, as a
mapping. **Every value may be `None`**, and L17 already treats a `None` a limit
needs as a veto rather than an assumption — so a stale or unreadable portfolio
makes trading MORE conservative, never less. That is the correct direction and it
is worth stating, because the intuitive fear is the opposite.

Nine of the risk engine's fields are not portfolio facts, and rather than
omitting them silently the service declares them:

```python
NOT_SUPPLIED = {
    "market_open":            "market data (L08)…",
    "symbol_tradable":        "the symbol registry (L11) and the broker adapter (L10).",
    "market_data_age_seconds":"market data (L08). This engine measures the age of the ACCOUNT reading…",
    "strategy_enabled":       "the strategy registry (L12).",
    "bot_state":              "the Bot Manager (L22).",
    "trades_last_minute":     "the trade journal (L19)…",
    "trades_last_hour":       "the trade journal (L19).",
    "last_trade_at":          "the trade journal (L19).",
    "margin_required":        "position sizing (L18) and the broker…",
}
```

This is L28's `DECLINED` pattern. A test asserts every `PortfolioState` field is
either supplied or named here, so adding a field to the risk engine forces a
decision instead of quietly producing a `None` that L17 reads as a veto.

---

## 12. Realtime: what changed, and nothing else

Section 32. Four account-scoped event types, added to L07's catalogue —
never a second realtime system, and no new scope, because a portfolio event
carries the figures the account channel already carries and `channels.py`
already authorizes it.

| Type | Fires when |
|---|---|
| `PORTFOLIO_UPDATED` | balance, equity, margin, unrealized, realized-today or FRESHNESS moved |
| `EXPOSURE_UPDATED` | gross or net changed |
| `DRAWDOWN_ALERT` | a display threshold (5%, 10%, 20%) was crossed — or recovered |
| `PORTFOLIO_HEALTH_CHANGED` | the health state moved |

`changes_between` compares against the PREVIOUS view. A figure that is the same
as last time is not an event, and a drawdown past 10% for an hour raises one
alert rather than 3,600. Freshness is inside the money tuple deliberately: a view
going stale changes nothing about the numbers and everything about how they
should be read.

The drawdown steps are a **display convention, not risk limits**, and the payload
says so. The risk engine holds the limits that stop a trade.

---

## 13. The API

Fourteen routes, every one a GET, all behind `Permission.view_portfolio`.

```
GET /v1/portfolio                  the whole view
GET /v1/portfolio/summary          headline figures (replaces the L30 501 stub)
GET /v1/portfolio/account          balance/equity as their OWNER reports them
GET /v1/portfolio/positions        open positions, marked where a quote exists
GET /v1/portfolio/exposure         gross, net, every grouping, open risk
GET /v1/portfolio/by-symbol
GET /v1/portfolio/by-strategy
GET /v1/portfolio/by-bot
GET /v1/portfolio/pnl
GET /v1/portfolio/drawdown
GET /v1/portfolio/margin
GET /v1/portfolio/reconciliation
GET /v1/portfolio/history          recorded snapshots, paginated
GET /v1/portfolio/risk-state       what the risk engine reads, and what it does not
```

Authorization is scoped **in the query** — `WHERE user_id = :me` — not after it.
A missing account and somebody else's give the identical 404, because
distinguishing them makes the route a membership oracle; `channels.py` records
the same reasoning for the same reason.

No credential is served, and there is no field to redact: `AccountState` has no
`login`, `password`, `api_key` or `token`, and `from_broker_account` copies the
broker's balance and equity and nothing else. A test asserts the router and every
portfolio module reference none of those names.

---

## 14. Frontend

`/portfolio` — one account at a time, chosen from a picker that lists paper and
broker accounts with their environment on the row. §41: the two are never merged
into a figure.

* **A dash is not a zero.** `money()` returns `undefined` for a `null`, so
  `StatTile` renders its own em dash and labels it `"Margin used: no data"` for a
  screen reader. A second literal dash would have looked identical and been
  invisible to that label.
* **Gross and net sit side by side**, in the totals and in every grouping table.
* **Health reasons render above the figures** when the view is not healthy.
* **An unchecked reconciliation shows `NOT CHECKED`**, never `AGREES`.
* **The currency note replaces the table** when the breakdown is withheld.
* **No chart.** §38 in spirit and §59 in letter: `market_bars` covers one instrument on this
  deployment, so an equity sparkline would be drawn from nothing.

Seven vitest cases cover exactly those behaviours.

---

## 15. What L30 did not build, and why

**No correlation engine.** §27 asks for correlated-exposure analysis *if
sufficient market data exists* and says not to fake one. `market_bars` covers one instrument,
so `correlation_note()` reports it as unavailable and points at the currency
breakdown as the one real proxy this platform can offer for the common-factor
risk §27 describes — several USD-long positions showing up as one large USD
figure. That is the same shared-dollar-move clustering the FX rule searches
already document.

**No currency conversion.** §19 in L30's numbering asks for exposure in the
account currency where conversion is needed. Every account here is
single-currency and no FX rate source is wired, so the breakdown is reported per
currency rather than converted at a rate nobody measured. `CLAUDE.md`'s swap
lesson is the precedent: a unit the tool cannot convert is reported as a gap, not
charged at an invented rate.

**No migration.** `portfolio_snapshots` already had every column.
