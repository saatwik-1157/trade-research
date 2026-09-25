# Broker / MT5 integration

## Two connections to one terminal

```
  tools/mt5_paper.connect()          the research harness. Currently running.
        |                            Demo-fenced by assert_demo, in code.
        v
  terminal64.exe  <-- MetaQuotes-Demo, account 5055473926
        ^
        |
  app/brokers/mt5.py MT5Adapter      the platform's adapter. Wraps the toolkit
                                     above rather than reimplementing it.
                                     Registrable since L70b as `mt5_demo`,
                                     on a Windows host, in TRADING_MODE=demo.
```

The adapter and the harness talk to the same terminal through the same
`MetaTrader5` package, and the adapter calls the harness's own functions. It
does not open a second connection of its own design, and it does not restate
the fence.

## The adapter

`backend/app/brokers/mt5.py`, implementing `BrokerAdapter` (`base.py`).

| Method | Notes |
|---|---|
| `connect` / `disconnect` | refuses any account whose mode is not demo |
| `state` | `ConnectionState` |
| `get_account` | balance, equity, margin as the venue reports them |
| `get_symbols` / `get_quote` | contract specs and live bid/ask |
| `get_positions` / `get_orders` | filtered by `MAGIC` |
| `get_order_history` | |
| `place_order` | returns what the venue said: ACCEPTED, REJECTED, or **UNKNOWN** |
| `modify_order` / `cancel_order` | |
| `close_position` | partial close supported |
| `health` | |
| `reconcile` | compares held vs believed; reports, never repairs |

**It ended the repository's worst duplication.** `connect()` previously existed
four times — `mt5_paper`, `mt5_account`, `rule_backtest`, `symbols/sync_mt5`.
This is now the one place the platform opens a terminal.

**What it deliberately did not retype**, because each carries a live defect's
worth of hard-won correctness: the filling-mode bitmask translation, the
bracket sanity check against the actual fill, the step-count rounding, and the
server-clock helpers. All are called on `tools/mt5_paper`.

## The fence

```python
# tools/mt5_paper.py -- called by the adapter, not reimplemented in it
def assert_demo(mt5, live=False):
    ai = mt5.account_info()
    if ai is None:                       raise RefuseToTrade(...)
    if ai.trade_mode != 0:               raise RefuseToTrade(...)   # 0 DEMO
    if live and not terminal.trade_allowed: raise RefuseToTrade(...)
```

`trade_mode`: 0 DEMO, 1 CONTEST, 2 REAL. **Anything that is not 0 refuses,
including a value this build does not recognise.** Failing closed on the
unknown case is the whole point of a fence.

The algo-trading switch is required only for `live=True`, because a dry run
sends nothing, and demanding it earlier would push somebody to enable order
sending just to preview signals.

## Registering the demo venue

`backend/app/api/v1/brokers.py`:

```python
_ADAPTERS = {"simulator": "paper", "mt5_demo": "demo"}
```

For eleven levels this was `{"simulator": "paper"}` and the MT5 adapter had **no
runtime registration path** — it existed, it was demo-fenced, and nothing could
put it in the registry. That is why `accounts_broker` was 0 and why the
platform's execution machinery had never run against a venue it did not also
write.

`mt5_demo` needs no credentials stored here. The terminal is already logged in,
and the adapter reads the account it finds rather than authenticating one.
**That is precisely why it could be added and a live venue still cannot:** a
real account needs a login, a password and a server, and a route that accepted
those would be a route that stores them.

### The command

Two requests, both authenticated as a user holding `manage_brokers`:

```
POST /v1/security/step-up
  {"password": "...", "scope": "BROKER_CREDENTIALS", "subject": "acct-demo"}

POST /v1/brokers/adapters
  {"account_id":     "acct-demo",
   "adapter":        "mt5_demo",
   "expect_account": "5055473926",
   "reason":         "demo venue pilot"}
```

`expect_account` is optional and worth setting. Registering is the moment the
platform commits to an account, and "whichever one the terminal happens to be
logged in to" is not a commitment anybody made — every other signal (the
balance, the symbol list, the window title) looks plausible on both accounts.

The response reports the account **the venue holds**, not the one you typed:

```json
{"account_id":"acct-demo","adapter":"mt5_demo","mode":"demo","state":"connected",
 "venue_account":{"login":"5055473926","server":"MetaQuotes-Demo",
                  "currency":"USD","mode":"demo","trade_allowed":true}}
```

### What it refuses

| Condition | Result | Registered |
|---|---|---|
| `TRADING_MODE` is not `demo` | 409, mode fence | nothing |
| no step-up grant | 401/403 | nothing |
| terminal not running, or MetaTrader5 absent | **503** | nothing |
| `assert_demo` refuses (REAL, CONTEST, unrecognised) | **409** | nothing |
| terminal holds a different account than `expect_account` | **409** | nothing, and **no account is switched** |
| the account already has a venue | 409 | nothing replaced |

Every failure path releases the terminal handle. `MT5Adapter.connect` opens the
terminal and *then* runs the fence, so a refusal would otherwise leak one
connection per rejected attempt — and these are exactly the errors an operator
retries.

### WINDOWS ONLY

**The API container cannot use this adapter.** The `MetaTrader5` package does
not exist for Linux; the image has no such module, and registering `mt5_demo`
there returns 503 and registers nothing. To use the demo venue the API process
must run on the Windows host that holds the terminal.

That is a property of MetaTrader, not of this design, and it is why
`python -m app.live.preflight` also has to be run on the host.

**Running the API on the host, against the same containers.** Keep Postgres and
Redis in Docker and run only the API process natively, in demo mode:

```
cd backend
set TRADING_MODE=demo
uvicorn app.main:app --port 8001
```

Port 8001 rather than 8000 so it does not collide with `tr-api`, and the
default `DATABASE_URL`/`REDIS_URL` already point at the published container
ports (5440 / 6390). Two API processes against one database is a deliberate,
temporary arrangement -- set `WORKERS_ENABLED=false` on one of them, because
`workers_enabled` exists precisely so a second process does not run a second
copy of every background worker.

Do not leave both running unattended. The honest arrangement once the demo venue
is in regular use is one API process, on the host, with the containers providing
only Postgres, Redis and nginx.

### Still fenced

The adapter's mode must equal the platform's `TRADING_MODE`; an account that
already has a venue is never silently replaced; step-up re-authentication is
required; both a security event and an admin audit row are written; and
`_ADAPTERS` has no entry whose mode is `live`, which a test asserts on the
**values** rather than the keys.

Registering a venue sends nothing. `POST /v1/orders` remains the one submission
door, the RiskEngine still mints the only `Approval` the OMS accepts, and
`live_execution_blockers()` is unchanged by the registration — there is a test
for that too.

## What the first real run found

2026-09-07, the first time the adapter was pointed at a terminal. Full record in
`FIRST_DEMO_VENUE_RUN.md`; the part that belongs here:

**`place_order` passed ATR multiples of `0.0` meaning "no bracket".** The
toolkit read that as a bracket of *zero width* -- `price - 0.0 * atr` is
`price` -- and sent `sl == tp == entry`. One attempt was refused outright
(10016 INVALID_STOPS); the next was accepted, and then `bracket_is_sane`
correctly called it insane, the repair returned 10025 NO_CHANGES, and the
toolkit closed the position rather than hold one whose exits are both against
it. Cost: nothing, opened and closed at the same price.

Fixed in `tools/mt5_paper.place()`: a multiple of zero now sends `0.0`, which is
MT5's "not set", and the sanity/repair block is skipped when no bracket was
requested. The harness's own path always passes 1.5/1.5 and is untouched.

After the fix the same order filled and kept its position, with the platform's
absolute levels applied by the follow-up modify -- which is the design this
adapter documents.

**Still open: a broker-mode fill creates no `positions` row.** Only
`app/paper/service.py` constructs one, so `PositionManager` cannot see, manage
or close a position the adapter opened, and reconciliation reports every venue
position as `unexpected_at_broker`. That is the next piece of work and it is
not an adapter defect.

## Errors the adapter handles

connection loss · terminal shutdown · broker rejection · invalid symbol ·
invalid volume · market closed · insufficient margin · requote · execution
error · timeout · duplicate request · **unknown order state**.

The last one is the one that shapes the design: an IPC timeout after
`order_send` looks exactly like a rejection from the caller's side. So an
uncertain outcome becomes `UNKNOWN`, the OMS parks it, and **nothing retries
it**. It exits only through reconciliation, which asks the venue what it
actually holds.

## Broker constraint validation

`app/brokers/validation.py`, run before anything is sent.

**A volume that is not a multiple of the venue's step is refused, not
rounded up.** `lot_for_risk` sizes a position so its stop costs a fixed sum;
silently rounding 0.037 to 0.04 because the step is 0.01 would risk 8% more
than budgeted with nothing in the record saying so. Rounding *down* is safe and
is what the sizing module already does.

Specs are per symbol and per broker, not universal: DE40 has contract size 1
and minimum volume 0.1 where the FX majors have 100,000 and 0.01, both measured
from this broker's terminal.

## Operational notes

- **Algo Trading must be on** in the terminal for any live send: Tools →
  Options → Expert Advisors → Allow Algorithmic Trading, or the toolbar button.
  Off produces `REFUSED: Algorithmic trading is disabled` and exit code 1 in
  about one second, which looks like a launcher problem and is not one. It can
  be off after a fresh terminal start. Read-only calls keep working with it off,
  so a healthy account read is not evidence that trading is enabled.
- **`MAGIC = 770315`** tags every order the harness sends, so it identifies its
  own positions and never modifies or closes one a human opened.
- **The MT5 package is Windows-only.** The API image is Linux, which is why the
  preflight's `account_readable` check must be run on the host.

## What a live venue would need

Not built, and each is separate, reviewed work:

1. An adapter whose `connect()` accepts a real account — which means a fence
   that distinguishes "approved real account" from "any real account", not the
   removal of `assert_demo`.
2. Encrypted credential storage with its own audit trail.
3. An `_ADAPTERS` entry, gated on mode as the simulator entry already is.
4. A live entry in `LIVE_GATES`, flipped in a reviewed pass with its evidence.

The cheap and useful next step is none of those: it is an `mt5_demo` entry
giving the platform a demo venue, which carries no live risk and is what makes
the pipeline testable end to end.
