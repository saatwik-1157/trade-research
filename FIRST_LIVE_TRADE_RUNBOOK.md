# First live trade runbook

**This procedure cannot be started on this deployment.** The preflight reports
`NOT_READY_FOR_LIVE` with 34 blockers, and four of them are structural: there is
no live broker adapter, no credential storage, no real account, and the code
fence refuses any account that is not a demo account. See
`LIVE_TRADING_READINESS_REPORT.md`.

It is written so that when those are met, the procedure is already agreed rather
than improvised at the moment it matters.

## Before anything

Read `LIVE_TRADING_SAFETY.md` and the two paragraphs in `CLAUDE.md` about what
this repository has actually measured. The short version, from the repository's
own backtests: no entry rule tested here separates from a coin flip out of
sample, cost drag is the only effect large enough to measure, and an edge the
size of the scatter between these rules would take roughly 6,600 trades — about
four years at the observed rate — to demonstrate at 80% power.

That is not an argument against the procedure. It is the context in which the
first live trade should be sized.

## The 31 steps

**Identity (1–4)**

1. Confirm the account number, read from the terminal, not from a config file.
2. Confirm the broker and the server string.
3. Confirm it is the intended MT5 terminal (path, not just "MT5 is open").
4. Confirm `ENVIRONMENT=production`, `TRADING_MODE=live`, `LIVE_TRADING=true`,
   `ENABLE_LIVE_TRADING_CONFIRMATION=YES_I_UNDERSTAND`.

**Authorisation (5–9)**

5. Confirm exactly which strategy is live-enabled.
6. Confirm its version id and configuration fingerprint.
7. Confirm the AI model id, version and fingerprint, or that the seat is off.
8. Confirm all eight capital limits and the risk limits, and that nobody changed
   them today.
9. Confirm the position size the sizer would produce for this account and stop.

**Market (10–12)**

10. Confirm bid, ask, timestamp and mapping for every enabled symbol.
11. Confirm the spread is inside its limit — at the venue's current quote, not a
    remembered figure. A single live quote can understate this broker by 3–8×
    against its own recorded median.
12. Confirm the market session. The first live session should not open in the
    thinnest hours.

**Broker state (13–15)**

13. Confirm there is no unexpected position at the venue.
14. Confirm there is no unexpected pending order.
15. Run reconciliation and confirm it is clean.

**Platform (16–19)**

16. Confirm monitoring is collecting.
17. Confirm notifications are configured — and remember trading safety does not
    depend on them.
18. Confirm the recovery manager is available and safe mode is clear.
19. Confirm kill switches are reachable at all three scopes.

**Activation (20–21)**

20. `cd backend && python -m app.live.preflight --strict` on the Windows host.
    Only `READY_FOR_LIVE` proceeds.
21. Arm, then explicitly activate — two signed acts, each with a reason.

**The trade (22–26)**

22. **Wait.** Do not generate a signal. Do not bypass the strategy's conditions.
    Do not inject an order. Do not place a "connectivity test" order.
23. When a legitimate signal arrives it goes through the normal path and nothing
    else: strategy → AI → risk → sizing → OMS → adapter → MT5.
24. Verify execution **by asking the venue**: broker order id, MT5 ticket,
    executed quantity, executed price, requested price, slippage, timestamp,
    status. A submission that did not raise is not a fill.
25. Verify the position exists at the venue and internally, with matching volume,
    direction and entry price.
26. Verify the stop and the target are set at the venue — not merely intended.

**After (27–31)**

27. Verify the journal entry.
28. Verify the portfolio updated.
29. Verify analytics attribute the trade correctly.
30. Keep monitoring: equity, margin, exposure, drawdown, connection, heartbeat.
31. On anything unexpected, enter safe mode. Reconcile before doing anything
    else.

## If the order is rejected

Record the reason, the broker response, the strategy, the risk decision and the
order state. Do not retry beyond what the OMS policy allows. **Do not loosen a
risk control and do not change a strategy parameter.** A rejection is
information.

## If the order returns UNKNOWN

Stop new orders. Reconcile against the venue. Determine what it actually holds.
**Do not submit anything for that intent until the original is resolved.** An
IPC timeout after `order_send` looks exactly like a rejection from the caller's
side, and this repository has a recorded instance of a figure the tool chose
being logged as a figure the server confirmed.

## Prohibited throughout

- generating a fake signal
- bypassing strategy conditions
- injecting an order into MT5 by hand
- bypassing risk, sizing, the OMS or the adapter
- a test order in the live account
- increasing risk, leverage, capital or position size
- retraining or replacing the model
- changing strategy parameters mid-session

## After the session

Write the session report: signals, accepted, rejected with reasons, orders,
fills, slippage, latency, positions, P&L, drawdown, exposure, margin, errors,
recovery events, reconciliation events, notifications, health.

**Do not optimise on one trade.** One trade is one sample. If something looks
worth changing, it becomes a research item and goes through governance.
