# Running this on another laptop

Two separate questions, and the second one is the one that can cost money.

## The hazard, first

**Never run a session on two machines against the same MT5 account.**

`run_overnight.py` refuses to start while another order-sending session is
alive. That check is a `psutil` scan of **local** processes — it cannot see
another laptop, and there is no shared state it could look at, because
MetaTrader offers a session no place to leave a heartbeat that the other would
read.

Two harvest loops on one account do not politely take turns. They compete for
the same `--max-positions 7` slots, each opens up to its own limit, and the risk
per pass doubles. Worse, neither log records the other's trades as anything
unusual, so the record afterwards looks like one session that traded twice as
hard.

Three ways to be safe, in order of preference:

1. **A separate demo account on the other laptop.** Free, and the clean answer.
   Each machine then has its own balance, its own positions and its own ledger.
2. **One at a time.** Stop the session on this machine before starting the other
   — `Stop-Process -Id <pid>`, which closes nothing and leaves positions with
   their brackets intact.
3. **Different `MAGIC` per machine** is *not* on this list. It stops the two
   sessions harvesting each other's positions, and does nothing about the
   account balance, the margin or the daily loss limit, which are shared.

If both machines must trade the same account, that is a broker-side problem
this repository cannot solve, and pretending otherwise with a lock file would be
worse than the honest refusal above.

## What the other machine needs

**MetaTrader 5**, installed, running, and logged in to the demo account.

**Algo Trading ON.** The toolbar toggle. Off means every order is refused the
instant it is sent and the session exits with code 1 with nothing useful in the
log — check this before debugging anything else.

**Python 3.10 or newer.** Development is on 3.14; CI runs 3.10, 3.12 and 3.14.

```
pip install -r requirements.txt
pip install MetaTrader5 psutil
```

`psutil` is optional in the sense that nothing crashes without it, and that is
exactly why it is easy to miss: absent, the single-session guard prints one line
and proceeds unguarded.

## What does NOT travel with the repository

`.gitignore` excludes `data/` and `reports/` deliberately — they carry a login
number, a server name, a balance and a record of live activity.

So a clone starts with an **empty ledger**. That is usually right: a second
machine trading a second account is a second sample, and merging the two would
pool accounts, which `track_record.py` now raises a data gap for.

If you genuinely want the accumulated history on the other machine, copy
`data/track_record.jsonl` by hand. Re-running `--merge` there is idempotent and
will not duplicate a position already on file.

## Starting it

```
start-trading.bat                 # until 06:00, live
start-trading.bat --harvest-only  # wind down: close, open nothing new
```

The `.bat` at the repository root is portable — it `cd`s to its own directory,
so it works from wherever the clone sits and forwards any arguments to
`run_overnight.py`. The Desktop shortcut on the original machine hard-codes
`C:\Users\Asus\trade-research` and does not travel.

## The platform is a different question

`backend/` is not this. It needs Docker, Postgres and Redis, it does not trade
on its own, and it cannot reach MetaTrader from its container at all — the image
is Linux and the `MetaTrader5` package is Windows-only. Driving a venue through
it means running a Windows-local uvicorn and re-registering the venue and bot by
hand, because those registries live in the process.

Put the harness on the second laptop. Leave the platform on one machine.
