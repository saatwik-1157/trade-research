# First live trade checklist

The one-page form. The reasoning is in `FIRST_LIVE_TRADE_RUNBOOK.md`; this is
what gets initialled.

**Status on this deployment: cannot be started.** `python -m app.live.preflight`
reports `NOT_READY_FOR_LIVE`, 34 blockers.

---

    Date ______________  Operator ______________________________

    IDENTITY                                              read from the terminal
    [ ] account number ............... ____________________
    [ ] broker ....................... ____________________
    [ ] server ....................... ____________________
    [ ] account type is REAL ......... ____________________
    [ ] currency / balance / equity .. ____________________
    [ ] free margin / leverage ....... ____________________
    [ ] matches LIVE_EXPECTED_ACCOUNT_ID and LIVE_EXPECTED_SERVER
    [ ] account is on LIVE_ALLOWED_ACCOUNT_IDS

    ENVIRONMENT
    [ ] ENVIRONMENT=production
    [ ] TRADING_MODE=live
    [ ] LIVE_TRADING=true
    [ ] ENABLE_LIVE_TRADING_CONFIRMATION=YES_I_UNDERSTAND
    [ ] every LIVE_GATES entry built and verified

    STRATEGY                          strategy ______________ version _________
    [ ] status is approved / active / live
    [ ] configuration fingerprint recorded ... ____________________
    [ ] symbols are the approved symbols, and only those
    [ ] timeframe is the approved timeframe
    [ ] a stop is defined
    [ ] a target is defined

    AI                    [ ] seat disabled     OR
    [ ] model ____________ version _______ fingerprint ______________
    [ ] approved   [ ] deterministic fallback defined

    RISK AND CAPITAL                                    none of these changed today
    [ ] approved capital ............. ____________________
    [ ] risk budget .................. ____________________
    [ ] max daily loss ............... ____________________
    [ ] max drawdown ................. ____________________
    [ ] max exposure ................. ____________________
    [ ] max position size ............ ____________________
    [ ] max open positions ........... ____________________
    [ ] max daily orders ............. ____________________
    [ ] risk limits configured for the account
    [ ] kill switches reachable, none engaged

    MARKET                                       at the venue's current quote
    [ ] every symbol maps correctly
    [ ] bid and ask present, timestamp fresh (< 60s)
    [ ] spread inside limit .......... ____________________
    [ ] market open, session appropriate

    BROKER STATE
    [ ] no unresolved order
    [ ] no order in UNKNOWN
    [ ] no unexpected position at the venue
    [ ] no unexpected pending order
    [ ] reconciliation clean

    PLATFORM
    [ ] database, Redis, workers healthy
    [ ] monitoring collecting
    [ ] recovery available, safe mode clear
    [ ] audit logging active
    [ ] step-up re-authentication in force
    [ ] MT5 terminal running, Algo Trading ON

    PREFLIGHT
    [ ] python -m app.live.preflight --strict  -> READY_FOR_LIVE
        run on the Windows host, exit code 0
        report saved to ______________________

    ACTIVATION                                     two signed acts, two reasons
    [ ] ARMED     by ______________ reason ____________________ at ______
    [ ] ACTIVE    by ______________ reason ____________________ at ______

    THE TRADE                                      wait for it; never force one
    [ ] signal arrived through the normal pipeline
    [ ] risk approved (decision id ____________________)
    [ ] size from the sizer, not chosen ____________________
    [ ] order submitted through the OMS
    [ ] broker order id ______________ MT5 ticket ______________
    [ ] executed qty ________ price ________ requested ________
    [ ] slippage ________
    [ ] position exists at the venue AND internally, matching
    [ ] stop set AT THE VENUE ________
    [ ] target set AT THE VENUE ________

    AFTER
    [ ] journal entry present
    [ ] portfolio updated
    [ ] analytics attribute it correctly
    [ ] monitoring shows the position
    [ ] session report written

---

**Stop conditions — any one ends the session:**
account mismatch · unexpected position · unknown order · reconciliation failure ·
stale data · MT5 or broker disconnect · risk breach · monitoring failure ·
worker failure · anything not understood.

**Never:** force a trade · fake a signal · inject an order · place a test order ·
bypass risk, sizing, the OMS or the adapter · increase risk after a loss ·
increase risk after a win · change a parameter mid-session.
