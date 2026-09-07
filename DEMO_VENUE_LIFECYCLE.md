# Demo venue: the full position lifecycle

2026-09-07, continuing `FIRST_DEMO_VENUE_RUN.md`. The platform can now open a
position at a real venue, record it, reconcile it, and **close it** — and the
attempt to do that turned up four more defects that only a real venue could
show.

Everything below is copied from the database, the API responses and the
terminal's deal history.

## The lifecycle, end to end

```
POST /v1/orders                     ticket 58328827918, filled 0.01 @ 1.16319
  -> positions row                  side=long qty=0.01 sl=1.16124 tp=1.16524
                                    broker_account_id -> mt5-demo-5055473926
POST /v1/positions/{id}/close       outcome CONFIRMED
                                    status closed, closed_quantity 0.01
                                    needs_reconciliation: false
                                    "closed at the venue as order 1d225b20-..."
```

Verified at the terminal: `58328827918` is CLOSED, exit deal 0.01 @ 1.16315.

## What made it possible

**`broker_accounts` rows now exist** (`app/brokers/accounts.py`). Registering a
venue writes one from what the terminal reported — login, server, currency,
mode — and never from what the caller typed. It holds no credential. Its own
route had said since L05 that creating one waits until an account *can* be
connected; that condition is now met at exactly the moment the row is written.

**The adapter is registered under two keys.** The platform has two identifier
spaces that had never been connected, because no broker position had ever
existed to connect them: the registries are keyed by whatever label an operator
registers with, while `positions.broker_account_id` — and therefore
`PositionView.account_id`, and therefore the key `BrokerExitExecutor` looks the
order manager up by — is the durable `broker_accounts.id`. Registering the same
adapter object under both makes the close path work without changing anything
callers already pass.

**A broker position prices off its own venue** (`_venue_quote` in
`app/api/v1/positions.py`). A broker position is closed AT a broker, so that
broker's book is the authoritative price for it. This does not breach the rule
that the broker book and the normalized feed are "never merged": nothing is
pooled or stored, one transient read decides one close, and the feed remains the
fallback. Before this, a demo close refused every time — the feed holds 705 bars
across two symbols and no live quotes.

## The four defects it found

### 5. A close was never confirmable — FIXED

`close_own` in the toolkit recorded `{ticket, symbol, retcode, status}` and
**not the fill**, though `res.price` and `res.volume` were sitting on the send
result. `MT5Adapter.close_position` therefore returned `accepted` with no price
and no volume, and the OMS — correctly — read that as "the venue left this in
accepted", parked the position `unknown` and asked for reconciliation.

So the close worked and the platform could never say so. Measured: position
58328592839 closed at 1.16350 for +0.33 while the platform recorded it
unresolved.

**Fix:** `close_own` now records `fill_price`, `closed_volume` and
`requested_volume`; the adapter passes them through, and returns `unknown`
rather than `accepted` when they are absent. This is the same lesson `place()`
already carries in its own comment — *"The fill, not the quote."*

### 6. Reconciliation could not write on PostgreSQL — FIXED

```
asyncpg.exceptions.DataError: invalid input for query argument
(can't subtract offset-naive and offset-aware datetimes)
```

Every datetime column in this schema is `timezone=False`, and the repository
states that convention in one place: `app.auth.models.utcnow` is
`datetime.now(UTC).replace(tzinfo=None)`. The reconciler and the position
manager both build aware times for their comparisons — which is right — and then
wrote them straight into columns. **PostgreSQL raises; SQLite accepts.** Found
three times in one afternoon, once anything actually reconciled or closed.

**Fix:** one shared `naive_utc()` (`app/positions/ingest.py`), used at all three
write sites. `_event` had already been doing it by hand at one of them.

### 7. No close order had ever been recorded — FIXED

The close path DOES persist before the venue call — `BrokerExitExecutor` writes
the close order, then asks the venue, then writes again. The mechanism was
right. **It had simply never worked.**

```
OrderNotRecorded: order ... for intent close:c28d97b3-...:0.01
  could not be recorded: symbol '4097df82-dcec-4466-9c88-bef02458eccb'
  does not resolve. Nothing was sent.
```

`orders` contained **zero** rows with a `close:` intent — including for the
closes that succeeded end to end. The executor built its `OrderProposal` from
`PositionView.symbol`, which `view_of` fills with the row's `symbol_id`, and the
order store resolves a tradable CODE. So every close order failed to record, on
every close, and `_record` logged it and proceeded — correctly, because a close
that already happened must not be refused because storage blinked, but it meant
the durability this path was designed to have was never once achieved.

**Why nothing caught it.** `PositionView.symbol` means different things in
different places: `view_of` puts a UUID there, and every hand-built view in the
test suite puts `"EURUSD"` there. The suite agreed with itself; production
disagreed with the suite; nothing compared them. That ambiguity is the third
identifier confusion this exercise has turned up, after `orders.signal_id` and
`positions.broker_account_id`.

**Fix, in two steps.** First an explicit `symbol_code` beside the ambiguous
`symbol`; then, an hour later, the honest version: **`PositionView.symbol` and
`MarketState.symbol` are the tradable CODE, both of them, everywhere.** Two
fields for one idea was the problem restated, not solved, so `symbol_code` is
gone again.

`PositionManager` resolves the code once per symbol, `view_of` leaves `symbol`
EMPTY when a row's symbol does not resolve, and the executor refuses outright on
an empty one — sending a close whose record is guaranteed to fail is how a venue
ends up holding something the database has never heard of. `run_once` keys its
quote dictionary by the code as well, like the market feed that produces it.

That closes the last of the three identifier confusions this exercise turned up.
Proved again after the change: position 58331472838, close order
`close:623070ab-…:0.01000000` recorded `filled` at 1.16229.

Proved on position 58330565300:

```
orders: close:729785da-...:0.01000000  status=filled  qty=0.01  fill=1.16234
```

The one consequence that was real: position 58328777371 closed at the venue for
−0.03 while its row update rolled back. The reconciler settled it, and did so
correctly — but with the close order now recorded, reconciliation has the
evidence rather than only the absence.

### 8. Realised P&L was not money — FIXED

```python
booked = (fill - row.entry_price) * direction * closed_now
```

No contract size, no tick value, no currency conversion. For 0.01 lots of
EURUSD that is `(1.16315 - 1.16319) × 0.01 = -0.0000004`, which a
`NUMERIC(18,4)` column stores as **0.0000**.

The venue said **−0.04 USD**.

So every broker-mode realised P&L the platform records is wrong by the contract
size — for FX, a factor of 100,000 — and rounds to zero. Anything reading
`positions.realized_pnl` inherits it.

**Fixed by asking the venue.** Multiplying by `contract_size` would make EURUSD
right and leave USDJPY, XAUUSD and DE40 wrong, because the profit currency is not
always the account currency. So the figure is no longer derived at all: MetaTrader
books `profit`, `swap` and `commission` per deal in account currency, and the
platform records their sum.

The money now travels a single chain, and a test asserts every link can carry it,
because a field missing at one end would look exactly like a venue that did not
report:

```
tools/mt5_paper.deal_money()   read back from history_deals_get
  -> OrderResult.realized_pnl
  -> FillRecord.realized_pnl     beside commission and swap, not derived from them
  -> CloseOutcome.realized_pnl
  -> positions.realized_pnl
```

**A broker close whose money the venue did not report now books NOTHING.** The
column is left as a gap and the position event records
`realized_pnl_source: "not reported"`. Reporting nothing is recoverable --
reconciliation can read the deal history -- while booking the price difference
would put a wrong number in the column every downstream reader trusts, and it
would look like a real one. Paper is unchanged: the simulator is its own venue
and its arithmetic is self-consistent in the units it quotes.

Proved end to end on 2026-09-07, position 58329837257:

| | |
|---|---|
| venue deal | closed 1.16244 -> 1.16242, **profit -0.02 USD** |
| `positions.realized_pnl` | **-0.0200** |
| event `realized_pnl_source` | `venue` |
| what the old arithmetic would have booked | -0.0000002, stored as 0.0000 |

## What behaved correctly, verified

* **Safe mode latched on a real mismatch.** The startup sequence reconciled
  against the venue, found the position left `unknown` by defect 5, engaged
  `POSITION_MISMATCH` and refused new orders with the reason. First time it has
  fired against a real venue.
* **It could not be released by asserting.** `/v1/recovery/safe-mode/exit`
  re-ran the whole sequence and released only after the condition had actually
  cleared — and required step-up re-authentication to do it.
* **The reconciler closed what the venue did not hold**, and left
  `realized_pnl` NULL rather than inventing an exit price it could not know.
* **Step-up rate limiting fired** (10 per 300s) during the run, which is the
  control working, not a fault.
* **`assert_demo` passed on every connect.** Nothing touched a real account.

## Status

    A position can now be opened at a real venue, recorded, reconciled and
    closed through the platform, with every step confirmed by the venue.

    Realised P&L is the venue's own figure in account currency, or a named
    gap -- never a derivation.

    A close order is now recorded before the venue is asked, which is what the
    path was designed to do and had never done.

    FIXED at L70m: the close order's `broker_order_id` was left empty, because
    `close_own` reported a retcode and a fill but never the ticket of the close
    order it sent. "Reconciliation matches on the position, so nothing is lost
    today" understated it -- that ticket is what an UNKNOWN close is settled
    by, and a parked close with no venue identifier can only be resolved by a
    person reading MetaTrader's history beside the database. It is now recorded
    on every branch, the failing ones included, and the venue's DEAL ticket is
    carried separately as `OrderResult.deal_id`.
