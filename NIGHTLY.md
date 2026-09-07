# Nightly run

Operational checklist for the overnight harvest session. The reasoning behind
the settings is in `tools/run_overnight.py`; the reasoning behind not believing
the balance is in `CLAUDE.md`.

## Pre-flight

Two switches, neither of which persists. Both have silently blocked a run.

1. **MT5 Algo Trading toggle.** Toolbar button must be green, or
   Tools -> Options -> Expert Advisors -> Allow Algorithmic Trading.
   It has been observed off on a terminal that had been running for two hours.
   With it off the session dies in about one second:
   `REFUSED: Algorithmic trading is disabled in the terminal`, exit code 1.
   A double-clicked launcher looks like it "closes instantly" - that is this,
   not a broken .bat.

2. **Claude Code permission mode.** Shift+Tab out of auto mode. Auto mode's
   classifier refuses a backgrounded `--live` launch and the verdict does not
   respond to retrying. The mode reverts on its own, so do this immediately
   before asking, not earlier.

Check the toggle without launching anything:

```
python -c "import MetaTrader5 as m; m.initialize(); print(m.terminal_info().trade_allowed)"
```

`True` means ready.

## Start

```
python tools/run_overnight.py
```

Or `Win+R` -> `C:\Users\Asus\Desktop\start-trading.bat`, which wraps the same
command and does not depend on the permission mode.

Stops at 06:00 local regardless of start time - it is not a fixed-length run,
so a late start is a shorter sample, not a later finish. `--until-hour N`
moves the stop; `--dry-run` prints the command without sending orders.

## It ends flat

The session holds nothing past its stop hour. Whatever is still open at 06:00
is closed at what it is worth, **losses included**, and the profit floor decays
from 0.50 to zero over the final 45 minutes so each position closes at the best
moment it is offered rather than all at once. Nothing new is opened inside that
window.

This is not tidiness. The harvest loop closes at the first sign of profit,
which books winners and holds losers - so the positions still open at the
deadline are, by construction, the losing tail. On 2026-09-07 the loop ended
holding 7 positions, 6 of them underwater, at -9.25 floating. Leaving them
carries that tail into the weekend, into the next session's `--max-positions`
count, and into another night of swap.

`--relax-over 90` starts easing the floor earlier; `--relax-over 0` turns the
ramp off and flushes at the hour.

## Winding an existing session down

```
python tools/run_overnight.py --harvest-only
```

Closes positions and opens NOTHING new. Use it when you want the account flat
by 06:00 and no further entries until you start a session yourself. Stop the
running session first - a second one is refused while the first is alive.

Starting a second session while one is running is refused, naming the pid and
start time of the one already up. Two harvest loops on one account compete for
the same seven position slots and double the risk per pass, and the launcher
gives no sign that a session is already live. `--force` starts one anyway;
`--paper` and `--dry-run` are not refused, since neither sends an order.

Everything the session prints is copied to `logs/overnight-<timestamp>.log`,
flushed on every line, because the console window closes with the run. What is
in there is the refusals, the skipped symbols and the reason the loop stopped -
the trades themselves are MetaTrader's record and are never at risk. The
directory is gitignored: a session log names the account and its balance.

## Confirm it took

The first pass should open positions within ~20 seconds. If it does not, read
the output rather than assuming.

- MetaTrader Toolbox (Ctrl+T) -> Trade tab: up to 7 positions turning over.
  A static list for 10+ minutes means the loop died.
- `python tools/mt5_account.py` - balance, equity, open count. Read-only.

Equity below balance right after a start is the spread on fresh positions, not
a loss.

## Morning

```
python tools/track_record.py --merge
```

Idempotent; merges by position_id so re-running never double-counts.

**Read the R-multiple and the date-clustered t. Not the balance, not the win
rate.** Harvesting at 0.50 USD against a full 1.5xATR stop caps winners and
lets losers run, which manufactures a win rate above 90% and a rising balance
whether or not the entries have any skill. The rule is `random` precisely so
that this is unmistakable. A win rate produced by bracket geometry is not a
measurement.

As of 2026-09-01 the record is +0.025R over 192 trades, pooled t 1.38,
date-clustered t -0.79. The null is holding.

## If it dies overnight

Positions are safe. Stop and target are server-side on every order, so a dead
loop leaves them protected - what stops is the harvesting and the new entries.
Restart with the same command; `track_record.py --merge` picks the trades up
either way.
