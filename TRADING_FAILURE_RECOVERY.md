# Trading failure and recovery

What breaks, what the platform does about it, and what it deliberately does not
do about it.

## The sequence

```
  FAILURE -> STOP NEW ORDERS -> ALERT -> RECONNECT -> RECONCILE
          -> VALIDATE SAFETY -> RESUME ONLY IF SAFE
```

`RecoveryManager.run_startup` (`app/recovery/manager.py`) walks it. **There is
no path from "the process started" to "orders may be sent"** — a step that finds
something needing a person closes the safe-mode latch with that condition as its
reason.

If the startup sequence itself raises, `main.py` latches
`SafeModeReason.recovery_failed` with "the startup recovery sequence itself
failed; nothing was verified". A recovery routine that can crash must not leave
the platform believing it was checked.

## Safe mode

A latch that is never closed without a `SafeModeReason` and a detail, both
recorded. `describe()` returns every reason currently holding it shut, because
a safe mode that said only "safe mode is on" would be a way of not saying what
went wrong.

**Blocks:** new orders, new bot starts, automated execution, strategy signal
execution.

**Deliberately does not block:** monitoring, reconciliation, position
visibility, risk evaluation, diagnostics, admin inspection, recovery actions.
A platform in safe mode is one you can still look at and still reconcile —
which is the point, because reconciling is how you get out.

**It is a second refusal, never a replacement for one.** It is checked *before*
risk and adds a refusal; it never approves anything, never releases a kill
switch and never widens a limit.

**It is process state, deliberately.** A stored flag is one somebody forgets to
clear, and a stored flag set by a process that has since died is a platform
refusing to trade for a reason that no longer exists. The cost is that it is
per-process; with one API process today that is correct, and a second needs a
shared latch — a row and a lease, not a flag.

Releasing it requires an actor and a reason, and — with `STEP_UP_REQUIRED=true`
— step-up re-authentication.

## Failure modes

| Failure | Response | Never |
|---|---|---|
| MT5 / broker disconnect | stop new orders, latch with reason, alert, reconnect, reconcile, resume only if safe | resume blindly on reconnect |
| Stale market data | no new entries; existing positions managed under the safest available policy | trade on a price that is gone |
| Reconciliation mismatch | stop new entries, latch, leave the finding for a person | overwrite one side with the other |
| Position at the venue we do not know about | **flag it** | close it, modify it, or adopt it as a strategy position |
| Position we believe open that the venue does not hold | mark closed; leave `realized_pnl` alone, event says the exit price is unknown | invent an exit price |
| Order in `unknown` | park; settle only by asking the venue | retry, ever |
| Order `failed` (never reached the venue) | safe to re-send | conflate with `rejected` |
| Order `rejected` by the venue | record the reason; do not re-send | loop |
| Risk engine unavailable | fail closed — no engine, no approval, no order | proceed unchecked |
| Database unavailable | fail closed — state must be durable before a venue call | send and hope to log later |
| Redis unavailable | events degrade; the chosen bus is logged and reported by `/health` | silently fall back |
| Notification delivery down | trading continues; the delivery is recorded FAILED | make order safety depend on Discord |
| Worker crash | supervisor sees the stale heartbeat and marks the run crashed | trust the database row over the heartbeat |
| Kill switch engaged | new entries stop; position management continues per policy | auto-release |

## Why nothing is repaired automatically

`app/brokers/reconcile.py` **reports and never repairs.** Every function returns
a finding; not one writes, closes, opens or cancels anything.

The reason is the two instincts it guards against. On discovering a position the
platform does not know about, the instinct is to close it — that closes a trade
somebody may have opened by hand. On discovering an internal position the venue
does not have, the instinct is to re-send it — that is exactly how a crash
between send and log becomes two positions.

`app/positions/reconciler.py` applies the only two corrections that are
unambiguously safe and leaves everything else for a person:

1. A position the venue does not hold becomes `closed` — the venue is
   authoritative for whether a position exists.
2. …and `realized_pnl` is left alone, because the price it closed at is not
   knowable from that fact.

**The venue is authoritative for broker-side state.** Where the two disagree
about *why* a position exists — which strategy, which intent — only our record
knows and the venue cannot be asked. That asymmetry is why a mismatch is never
resolved by overwriting one side wholesale.

## Bot recovery

A crashed bot is restarted only when **all** of these hold:

- no kill switch engaged, globally or for its account or strategy;
- the bot is not disabled;
- the account has no unresolved order;
- the run is `crashed`, not `halted`.

A kill switch is a decision somebody made, and recovering out of it
automatically would be a bot-level bypass of it. Anything else leaves the run
`crashed` with the reason recorded — the honest state: it needs a person.

**A restart resumes; it does not re-run.** The execution worker reads signals in
`new` only, and a signal already carried to a decision has left that state.

## Restart and deployment

- A new process starts with the activation machine at `DISABLED`. No restart,
  redeploy or container bounce reaches `ACTIVE`.
- `RECOVERY_STARTUP_CHECKS=true` runs the sequence at startup. A deployment that
  turns it off starts without having checked anything, and the recovery status
  says so in as many words.
- Order state is durable before the venue is called, so a crash between send and
  response leaves a `submitting` row on disk. Without that row a crashed submit
  is indistinguishable from a submit that never happened — and that distinction
  is the whole of "reconcile" versus "safe to send".

## Browser independence

The engine does not depend on a browser, a dashboard or a TradingView tab.
Execution runs in `ExecutionWorker`, a supervised loop with a heartbeat; the UI
controls and observes the trading system and is not the trading system. Closing
the browser changes nothing about what the workers do.
