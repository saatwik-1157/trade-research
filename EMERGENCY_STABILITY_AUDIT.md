# EMERGENCY_STABILITY_AUDIT.md

**Status: `STABILITY_FIX_REQUIRED`**
Audit performed 2026-09-10. Nothing was changed while it ran, and no order was
sent. Every figure below was read from the venue, the logs or the source; a
figure that could not be measured is named as unmeasured rather than estimated.

---

## 0. Safety state at the moment of the audit

Measured before anything else, per the standing rule that an audit must not run
against a system that is still trading.

| | Measured | How |
|---|---|---|
| Order-sending process running | **none** | `Get-CimInstance Win32_Process`: no `python.exe` at all |
| MT5 terminal | running, idle | PID 9232, started 19:40, nothing driving it |
| Account | `5055473926 @ MetaQuotes-Demo` **[DEMO]** | `tools/mt5_account.py` |
| Open positions | **0** | venue |
| Pending orders | **0** | venue |
| Balance / equity | 99,989.49 / 99,989.49 USD | venue |
| `trading_mode` | `paper` | `PROJECT_STATE.json`, generated |
| `live_trading` | `false` | `Settings.live_execution_blockers()` |
| Live blockers | **12** | 10 unbuilt gates + mode + flag |

**No live-trading configuration had to be disabled, because none was enabled.**
`TRADING_MODE=paper` and `LIVE_TRADING=false` were already the state, all ten
`LIVE_GATES` entries are still `False`, and `tools/mt5_paper.assert_demo`
refuses a non-demo login underneath all of it. The account is flat, so the
"inspect before reconciling" step had nothing to reconcile.

---

## 1. There are two independent paths to a broker order

This is the finding that matters most, and it reframes the rest.

```
PATH A -- the research harness            PATH B -- the platform
run_overnight.py                          TradingView webhook
  -> take_profit.py                         -> signal validation
  -> mt5_paper.py                           -> strategy -> AI advisory
  -> mt5.order_send()   <-- 4 call sites    -> RiskEngine (veto)
                                            -> PositionSizer -> OMS
                                            -> brokers/mt5.py
                                            -> mt5.order_send()  <-- 1 call site
```

`tools/take_profit.py`, `tools/run_overnight.py` and `tools/mt5_paper.py`
import **nothing** from `app`. They do not consult the RiskEngine, the OMS, the
signal tables, the symbol mapper or TradingView. Verified by import scan and by
grep for `riskengine|oms|webhook|tradingview` across all three: no hits outside
comments.

**Everything that has traded on this account traded through Path A.** Path B
has never sent an order at this venue outside its own tests.

So the brief's requirement — *TradingView initiates; RiskEngine is the final
veto; no timer may reach MT5* — is not a repair of the existing flow. Path B
is already built that way and already has one `order_send` behind the OMS.
The work is to **retire or fence Path A**, not to rebuild Path B.

`.env` currently sets `EXECUTION_WORKER_ENABLED=true`, so Path B would begin
consuming signals if the platform were brought up. It cannot currently place an
order: no broker account is registered (`accounts_broker: 0`) and an account
with no order manager refuses with `no_venue`. That is a configuration
accident away from mattering, and it is listed as a fix below.

---

## 2. Every session dies early. Eight for eight.

| Log | Requested | Ran | Reached deadline | Flushed |
|---|---|---|---|---|
| 20260910-090520 | 1255 min | **87 min** | no | no |
| 20260909-215354 | 486 min | **59 min** | no | no |
| 20260909-215211 | 488 min | **1 min** | no | no |
| 20260909-195231 | 607 min | **120 min** | no | no |
| 20260909-081102 | — | **144 min** | no | no |
| 20260908-200435 | — | **68 min** | no | no |
| 20260908-150450 | — | **54 min** | no | no |
| 20260908-101248 | — | **45 min** | no | no |

`grep -c "flat-by 06:00: closed"` returns **0 across all eight**.

This is the loss mechanism, and it is worth stating precisely because it is not
the obvious one. The harvest loop closes a position at the first sign of profit
and holds the rest, so **the positions still open at any moment are the losing
tail by construction**. `--flat-by` exists to cap that tail: at the deadline
everything open is closed at what it is worth, losses included. A session that
dies before its deadline never runs that flush, so the tail survives, and the
next session inherits it against its own `--max-positions` count.

Eight sessions in a row skipped the one mechanism that bounds the downside.

---

## 3. Why they die: not what the code already guards against

Three hypotheses were testable from evidence on this machine. Two are refuted.

**Refuted — the machine sleeping.** The keep-awake hold added in `01714fe`
works. Windows `Kernel-Power`/`Power-Troubleshooter` events show no Modern
Standby entry between 09:05 and 10:34 on 2026-09-10, none between 19:52 and
21:52 on 09-09, and none between 21:53 and 22:52. The three most recent deaths
all happened with the machine demonstrably awake. The previously documented
cause is fixed and is no longer the cause.

**Refuted — an unhandled exception.** Across all 27 session logs:
zero `Traceback`, zero `KeyboardInterrupt`, zero clean-exit markers. The
`finally` that releases the keep-awake hold never printed. A Python-level
failure would have left one of those.

**Supported — external termination.** The logs stop mid-pass, between one
20-second tick and the next, with no final line. That is what a killed process
looks like: a closed console window, a `taskkill`, or a session tied to a shell
that went away. It is not a fault inside the program, which is why adding more
exception handling inside the loop would not have prevented a single one of
these eight.

**Unmeasured:** which of those three ended each session. Nothing on this
machine records it. `CRASH_REPORTS/` (brief §32) would; it does not exist yet.

---

## 4. A dead terminal is misread as a failed log line

`tools/take_profit.py:335-342`:

```python
try:
    bal = mt5.account_info()
    print(f"... balance={bal.balance:,.2f} equity={bal.equity:,.2f} ...")
except Exception as exc:   # "a line of log is not worth a session"
    print(f"pass {passes}: could not read the account "
          f"({type(exc).__name__}); the session continues")
```

`mt5.account_info()` returns **`None`** when the terminal connection drops.
`bal.balance` on `None` is an `AttributeError`. So the disconnect signal
arrives wearing the costume of a formatting error, and the handler — written
for a formatting error — keeps the session running.

Observed on 2026-09-10: the message repeats for **23 consecutive passes**, from
pass 240 (10:25:06) to the last line at 10:32:26. In that entire window the
session harvested nothing, opened nothing and halted nothing — zero matches for
`ENTRY FAILED|HARVEST|OPEN|HALTED`. It spun blind for seven and a half minutes.

Two consequences, and the second is the serious one:

1. It cannot manage open positions while disconnected.
2. **`--max-daily-loss` cannot fire.** The daily-loss check reads the account.
   An account that cannot be read is not a loss of zero, but the loop treats
   the gap as though the limit were satisfied, so the one hard risk stop is
   silently inert for as long as the condition lasts.

`run_overnight.py` already has the right behaviour and it is tested — "a dead
terminal is not retried all night", give up after 10 consecutive failures, and
`an uncountable account is UNKNOWN, not flat` (`tests/test_rule_backtest.py`,
passing). That guard sits in the **outer** wrapper. The inner loop in
`take_profit.py` never tells it anything is wrong, so the guard never counts.

This is the repository's own recurring shape again: *a figure the tool could
not obtain, recorded as a figure that was fine.*

---

## 5. What the audit did NOT find

Stated because the brief asks for several of these to be built, and building a
second one is worse than finding the first.

- **No live trading**, and none reachable without ten deliberate code changes.
- **No AI anywhere in the path that traded.** Path A has no model, no
  inference, no advisory step. Losses cannot be attributed to a model that was
  never consulted.
- **No martingale, no size-increase-after-loss, no recovery trades.** Sizing is
  `lot_for_risk()` off the stop distance, scale-invariant, and rounds *down*.
- **No duplicate OMS, RiskEngine, strategy engine or broker connector inside
  the platform.** One `order_send` in `app/brokers/mt5.py`. The duplication is
  Path A versus Path B, not within Path B.
- **No unbounded retry.** `run_overnight.py` gives up after 10 consecutive
  failures; that mechanism is correct and tested, it is simply not reached.

---

## 6. Fixes, in the order the evidence supports

Highest risk first, per §86.

1. **Treat `account_info() is None` as a disconnect, not a log failure.**
   Return a sentinel from the pass, count it in the outer loop's consecutive
   failure budget, and stop the session when the budget is spent. Never let a
   pass that could not read the account be indistinguishable from a clean one.
2. **Make the daily-loss stop fail closed.** An unreadable account must block
   new entries, not permit them.
3. **Make the flush recoverable.** A wind-down that can run *after* a dead
   session — `start-trading.bat --harvest-only` is most of it already — so the
   losing tail is bounded even when the session that opened it is gone.
4. **Detach the session from its console** so closing a window cannot kill it,
   and write `CRASH_REPORTS/` on exit so the next death records its own cause.
5. **Fence Path A.** Either route it through the RiskEngine, or mark it
   explicitly as a measurement harness that must not run unattended.
6. **Leave `EXECUTION_WORKER_ENABLED` false** until Path B has a registered
   broker account and a reviewed decision to consume signals.

Items 1–3 are small and are what stop the bleeding. Items 4–6 are the
architecture work. Nothing here requires a model, a new strategy, or a
parameter change.

---

## 7. Scope

This audit covered: the harness, the session logs, the venue record, the
power log, the live/settings gates, and the order-origination paths in both
halves. It did **not** cover, and these remain unaudited: the platform's
runtime behaviour under load, Redis and Postgres failure modes, the WebSocket
layer, memory growth over a long run, the frontend, or Docker deployment —
because the platform has not been running, and auditing a service from its
source alone would produce assertions rather than measurements.
