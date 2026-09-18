# PROJECT_STATUS.md

Generated 2026-09-18 from the repository, the crash journal, the session logs,
the ledger and a live read of the venue. Updated overnight on 2026-09-19; see
[What changed overnight on 2026-09-19](#what-changed-overnight-on-2026-09-19)
and [What changed on 2026-09-18](#what-changed-on-2026-09-18).

**Current Status:** `HARNESS_FENCED` → `SOAK_REQUIRED`, and the soak is not
producing clean evidence because the host keeps falling asleep.

> **RESOLVED 2026-09-18 16:10: the account is FLAT.** It had been unreadable
> since 09-16 08:46, when session `20260916-082522` lost the venue mid-flight,
> gave up by design, and recorded the book as UNKNOWN rather than flat — seven
> positions open at its last readable pass. The terminal now reports **0 open
> positions, equity equal to balance at 99,919.45**. Those seven closed
> themselves server-side across the two days nothing was watching, which is
> luck rather than a wind-down, and is the third time this month the venue has
> finished a job the harness could not.

Four sessions have run since the last update. One reached its deadline and
flushed. One missed its deadline by 2h35m and abandoned seven positions. One
was killed by the operator. One lost the terminal and stopped. The fence held
in all four — no session sent an opening order without an `Approval`, and no
session claimed to be flat when it could not prove it.

What is left before `PAPER_READY` is still evidence rather than construction.
The new finding is that the evidence is being spoiled by the machine, not by
the code: **Windows modern standby is putting the host to sleep mid-session,
and the fix that was supposed to prevent it does not work.**

| | |
|---|---|
| **Trading Mode** | `paper` — `live_trading: false`, 12 live blockers, `assert_demo` in code |
| **Account** | `5055473926 @ MetaQuotes-Demo` **[DEMO]** · **FLAT** as of 09-18 16:10 — 0 open positions, 0 pending, balance 99,919.45 USD, equity the same. 632 closed trades on the venue, 09-04 to 09-17, net −80.55 |
| **Running processes** | none trading. MT5 terminal open and idle |
| **Preflight** | `tools/preflight.py`, 11 checks, read-only. Re-run 2026-09-19 02:0x: **10 of 11 green, one FAIL**, and the FAIL is the guard working — a 06:00 deadline lands on the venue's Saturday. **The battery finding is cleared**: `on_ac_power` reports mains, so the execution power request has no documented expiry. Terminal connected, algo trading on, account flat |
| **Operator entry point** | `run.bat` — double-click menu, 17 options, the three that send orders each behind a typed YES |
| **CI** | **GREEN.** All seven jobs on `fc17aff` and again on `53f33c8` (2026-09-19). The last failing step was `Run mypy`, red since `d5d47c9` because the local gate had only been run over `app/` while CI runs it over `app/` and `tests/`. See [CI](#ci-was-red-for-five-days-and-only-half-of-it-was-new) |
| **TradingView Status** | gateway built (hmac, age check, idempotency, body cap). **Never exercised end to end** — no broker account registered |
| **MT5 Status** | one adapter, one `order_send` on the platform path; 4 on the harness path, all four behind the Risk Engine — the opening order needs an `Approval`, the three closing ones are recorded and never refused |
| **Risk Status** | 33+ veto codes — weekly loss, consecutive losses and correlation added at P4. RiskEngine is the final veto on **both** paths as of P2 |
| **Windows Build Status** | **exists.** `dist/trade-research/` onedir, rebuilt 09-15 09:36, plus a legacy onefile `dist/trade-research.exe` from 09-12. Built by `build_exe.py`. *(This row said "none" until 09-18; it was stale.)* |
| **Stability** | **three sessions have now reached a deadline; one of the three flushed clean.** The limiting factor is no longer the code path — it is the host sleeping. See [Modern standby](#modern-standby-is-eating-the-soak) |
| **Tests** | **toolkit lane green: all ten files pass** (re-run 2026-09-19). **Backend suite green at 3242 passed, 12 skipped in 19:02** on the same date — up from 2,976 on 09-12, the difference being the Tier-1 work below. `ruff` + `mypy` are clean over `backend/` — **and only `backend/`**: `.github/workflows/tests.yml` runs both with `working-directory: backend`, so `tools/` has never been lint- or type-gated and carries pre-existing findings in both |

## What changed overnight on 2026-09-19

Four items off the Tier-1 wiring list, each one the same shape of defect:
**a mechanism that was built, tested, and never given a caller.** None of
them is new capability; all four are seats that were left empty.

| | what had no caller | commit |
|---|---|---|
| **3** | `app/brokers/validation.py::validate_order` — written for one call and invoked by nothing in `app/`, so a volume the venue would refuse was discovered by the venue refusing it | `fc17aff` |
| **4** | `OrderManager.reconcile` — the only exit from `unknown`, reachable only by a human POSTing to `/v1/orders/{id}/reconcile` | `24f44ad` |
| **6** | `webhook_events` — a complete write path since L09 with no GET over it, so "did our alert arrive, and why did it not act" needed a database client | `53f33c8` |
| **5** | `PositionMonitor` and all nine exit policies — `grep PositionMonitor app/` found the class, its module and a docstring, and no construction site | `542114d` |

Three defects were found by adversarially reviewing item 3's own diff before
it was pushed, and each fix was confirmed by reverting it and watching the
matching test go red:

* `_venue_spec` re-read the venue's **entire** symbol table on every attempt
  for a symbol it does not list — on MT5 a full `symbols_get()` per signal —
  while its docstring and its test both claimed one refresh.
* Moving the spec check ahead of `place_order` silently removed the only
  end-to-end cover of `submit`'s `except BrokerError` branch. That branch
  could be deleted with the whole suite still green.
* `MT5Adapter.get_symbols` turned `symbols_get() -> None` into `[]`, so an
  unreadable terminal read as "this venue lists no symbols" — erasing the
  exact distinction the two new refusal codes draw, and caching it for five
  minutes.

Item 5 carried two more of its own. The feed quote path in the close route
**had never worked**: `service.get_quote(db, code)` against a method that
takes `(db, internal_symbol, provider)` is a TypeError, swallowed by a bare
`except Exception`, and the result's quote was then read at `.bid` rather
than `.quote.bid`. Either defect alone made the fallback dead, so a position
with no reachable venue could not be closed at all and the refusal read as
"no usable quote" rather than as a bug. And nothing in `app/` had ever set
`MarketState.atr`, so both stop movers would have refused on every tick — a
configured deployment silently doing nothing.

**Every new worker and every new exit defaults to OFF**, and for the exits
that is a measurement rather than caution: a 3.0 ATR trail measured a median
out-of-sample expectancy of −217 at D1 against −58 for the fixed 1.5×1.5
bracket, across a 16×8 grid in which all eight exits were negative.

`tools/project_state.py` was asking only the nginx proxy on `:8080` for the
API's health. nginx is not in the default compose set, so regenerating
`PROJECT_STATE.json` wrote `api_reachable: false` and nulled `trading_mode`,
`live_trading` and `live_blockers` — the three fields the file exists for —
while the API was up and healthy on `:8000`. It now tries the proxy first,
falls back to the direct port, and records which door answered.

**Nothing traded.** No session was started, no order was sent, and the
account is unchanged at 0 open positions and 99,919.45.

## What changed on 2026-09-18

The day divides into the morning's defect work — recorded in the sections
below — and an evening spent measuring things and finding that the
measurements were themselves wrong.

**The one gate this project ever passed was noise in the gate.**
`reports/rule_search.json` records `beats_permutation_null: True` for
`boll_fade_50_2.0`, and `CLAUDE.md` quoted it as the single thing that ever
cleared anything here. It does not clear it. `--null-rounds` defaulted to 3,
and three draws cannot estimate "the best of N under no skill" well enough to
judge against: measured on identical data with only the round count changed,
the null's best came out at 1.57 over 3 rounds and 0.92 over 25. Settled at 20
rounds on the 41-candidate baseline, the null is **1.00** and the candidate
scores **0.98**. The default is now 10, and every figure measured before today
was taken at 3. The search now fails **all three** gates rather than two of
three, which makes the result stronger, not weaker.

**Two new searches, both null, and one of them with a control.**

- *Supertrend* (`tools/supertrend_search.py`), the only concrete strategy in
  three public trading repositories. Not a rename — its band ratchets, so the
  flip level carries state from every bar since the last flip, which nothing
  searched here does. The source rule scores **−2.24 in sample and −4.17 out**,
  loses in all seven pairs, and is positive in one era of four. Per-symbol win
  rates run 44–48% against the 50.5–52.8% the spread demands.
- *Volume* (`tools/volume_search.py`), the last untested input, on crypto D1
  where the exchange reports real traded size. **Two of the six candidates
  ignore volume and are the point**: at both quantiles the arm that ignores it
  beats both arms that use it. The finding is not "volume fails" but "the
  control beat the treatment", which is only visible because the control was
  in the run — six of the seven earlier searches had no equivalent.

**Two tools that make the thing operable.** `run.bat` is a double-click menu
over everything; `tools/preflight.py` answers the question `--dry-run` cannot,
because that refuses early on a weekend deadline and then reports nothing
else. The preflight found on its first run that this machine is on battery,
which is the one condition under which the standby fix below cannot work.

## CI was red for five days, and only half of it was new

The last green run is `4305cd7` on 09-13. Everything since has been red, and
the cause is two unrelated defects that happened to land together.

The first arrived with the eight commits that sat unpushed from 09-15 to
09-18: the `keep_awake` tests bound the context manager's yield to a single
name and compared a **tuple to `True`**, which fails on every platform. That
was fixed early on 09-18 while unpacking those tuples for the power-request
work, before anyone had looked at CI.

The second was added on top and is worth recording as a method failure rather
than a typo. A new test asserted that a session with a deadline requests
`PowerRequestExecutionRequired`. True on Windows; **false by design on Linux**,
where `power_request()` returns at `os.name != "nt"` before taking anything.
Stubbing `ctypes` does not help — the platform check runs first, which is the
point of it. It passed locally on the only platform it could pass on, and the
matrix is Linux.

The test now splits: the three request types on Windows, and off Windows the
documented no-op — nothing taken, and the session still *runs* rather than
claiming a hold it does not have. Verified by flipping `os.name` to `posix`
**after** the imports, because `ctypes` branches on it at import time and
flipping first breaks the import instead of the test.

## Modern standby is eating the soak

This is the finding of the last four days, and it outranks everything else on
the list.

`889526a` (09-15 09:34) was meant to close it: hold the screen awake, not just
the system, because this machine only has S0 standby. The session that ran
after it prints the new banner — *holding the machine AND THE SCREEN awake* —
**and slept anyway, three times.**

**Session `20260914-231624`, on the build before the fix:**

| From | To | Gap | Should have been |
|---|---|---|---|
| pass 300 (00:56:16) | pass 400 (04:28:17) | 3h32m | 33m |
| pass 484 (05:03:18) | pass 485 (05:56:06) | 52m48s | 20s |
| pass 485 (05:56:06) | flush (08:35:24) | 2h39m | deadline was 06:00 |

485 passes in a 9h20m window that was budgeted for roughly 1,200. About six and
a half hours went to sleep. The `--flat-by 06:00` flush finally fired at 08:35
— **2h35m late, onto a connection that had died with the machine.** All six
retries landed inside 50 seconds, all returned 10031, and the session gave up
with seven positions open.

**Session `20260915-201059`, on the build with the fix:**

| From | To | Gap | Flagged? |
|---|---|---|---|
| pass 1270 (03:14:39) | pass 1271 (05:06:48) | 112m | **yes** — `THE MACHINE SLEPT` |
| pass 1271 (05:06:48) | flush (06:06:48) | 60m | no |
| flush attempt 2 (06:06:58) | attempt 3 (08:12:55) | 2h06m | no — it had said *retrying in 10s* |

Roughly five hours lost, on the build that claims to prevent exactly this. Two
things follow, and they are separate defects:

1. **The S0 keep-awake does not keep the machine awake.** The banner is
   printed, the call is presumably made, and the host sleeps regardless. Until
   this is understood, no overnight session is evidence of anything except how
   the laptop's power policy behaves.
2. **The sleep detector only watches the pass loop.** Both the deadline branch
   and the flush retry loop can lose hours in silence. A retry that promises
   10 seconds and delivers 126 minutes should say so, and the retry budget
   should be spent in wall-clock time rather than in attempts — six attempts
   inside 50 seconds is not six chances at a reconnect, it is one.

### What changed on 2026-09-18

Both numbered defects above are now fixed in code, and the first one had a
root cause neither previous attempt had found.

**The machine was not necessarily sleeping out from under the session — the
session was being paused.** Under Modern Standby the Desktop Activity
Moderator suspends desktop applications, which Microsoft states without
hedging: *"Windows prevents desktop applications from running during any part
of modern standby after the DAM phase completes."* A harvest loop is a desktop
application. No `ES_` flag addresses the DAM, because the execution-state
flags map only to `PowerRequestSystemRequired` and
`PowerRequestDisplayRequired`. The request that does —
`PowerRequestExecutionRequired`, *"the calling process continues to run
instead of being suspended or terminated by process lifetime management
mechanisms"* — has no `ES_` constant at all and is reachable only through
`PowerCreateRequest`/`PowerSetRequest`. That is why both earlier fixes printed
a confident banner, got a non-zero previous state back, and slept anyway.

Verified on this machine rather than assumed, which is the step both earlier
fixes skipped: `PowerCreateRequest` returns a live handle and system,
execution and display are all GRANTED. `powercfg /requests` needs elevation so
it could not be read back — that is a gap in the evidence, not a claim.

Two limits are now printed at startup instead of discovered at 06:00. Power
requests are terminated on user-initiated sleep (lid, power button, Start
menu), so closing the lid still ends a session. And on Modern Standby **on
battery**, system and execution requests are terminated 5 minutes after the
sleep timeout expires — so an overnight run on DC cannot be relied on however
this is written, and a session with a deadline that starts on battery now says
so in capitals.

**This is not closed until a full-length session runs without a gap.** Two
fixes have already been declared working here on the strength of an API
return value. The banner now names which hold is in force so the next log can
be read against its own claim.

The detector working at all is new and it is the reason this section can be
written. It was added in the same commit as the fix that failed.

## The sessions since 09-12

**`20260914-223823` — `abandoned`, 33 minutes, 101 passes.** Killed by the
operator and acknowledged as such in the journal. Left 7 open; a replacement
session started five minutes later and inherited them.

**`20260914-231624` — `ended_not_flat`, 485 passes, realised −4.11.** Covered
above. Missed its deadline by 2h35m, flushed into a dead connection, left 7
positions at the venue. The log ends with the right instruction: *close them by
hand, or start a wind-down session*.

**`20260915-201059` — `completed`, 1,271 passes, realised −18.48.** The good
one, and it is worth being precise about why it succeeded. The flush was
refused on all seven positions at 06:06 with 10031, retried twice, then the
machine slept for two hours. When it woke at 08:12 the connection came back,
four positions closed at 08:13:15, and the other three were already gone —
stopped out server-side while nobody was watching. The session recorded
`still open 0` and `completed`, which is true. **It reached flat by accident of
timing, not because the flush worked.** Counting it as deadline evidence would
be overcounting.

**`20260916-082522` — `ended_not_flat`, exit code 3, 65 passes.** Started 08:25,
twelve minutes after the previous session finally finished flushing, with the
terminal in whatever state that left it. Ran 55 clean passes, then:

```
[08:43:49] PASS FAILED (1/10): VenueUnreadable: positions_get returned None; the open book is unknown
...
[08:46:49] giving up after 10 consecutive failures; the terminal is not answering and a flush would fail too
could not count open positions (VenueUnreadable); treat the account as UNKNOWN, not flat
```

**This is the disconnect fix working on a surface it was not written for.** The
original defect was `account_info()` returning `None` and being swallowed as a
formatting error; this is `positions_get()` returning `None`, and the harness
named it, refused to trade blind, declined to attempt a flush it knew would
fail, and refused to record the account as flat. Failing closed is the whole
design and it did it unprompted.

Two rough edges it exposed: `could not read the closing balance
(AttributeError)` is an exception class leaking into operator-facing output,
and the summary reports `passes 65` while the journal records `"passes": 55` —
the two counters disagree about failed passes.

## What the abandoned books cost

Balance moved twice while nothing was managing the account:

| Window | Balance | Move |
|---|---|---|
| 09-12 close → 09-14 23:16 start | 99,978.88 → 99,964.42 | **−14.46** (the 7 weekend positions) |
| 09-14 08:36 end → 09-15 20:10 start | 99,960.31 → 99,932.79 | **−27.52** (the 7 abandoned positions) |

−41.98 across the two windows. The 09-14 session realised −4.11 while it was
running and then handed −27.52 to the market on its way out. These outcomes are
in the ledger — they are not extra losses hiding somewhere — but the harness
neither chose nor observed them, and they are most of what the account gave up
this week.

## The ledger

**882 trades** (`data/track_record.jsonl`), merged 2026-09-18 16:1x — current.
The 09-16 session's trades are in: 13 new of 630 pulled, with 2 excluded that
this tool did not open.

Account `5055473926`: **630 trades, net −80.41**, 09-04 to 09-17. The venue
reports the account **flat** — 0 open positions, equity equal to balance — so
the seven that were unreadable on 09-16 closed themselves server-side over the
two days nothing was watching. The older 252 stay `unrecorded` at −22.37.

| | n | mean R | t by date | t by symbol |
|---|---|---|---|---|
| Pooled R | 863 | −0.0247 | −1.57 (thr 2.131) | −2.34 (thr 2.447) |
| Regime 1.0 | 832 | −0.127 | **−2.58 (thr 2.131) ✗** | **−2.58 (thr 2.447) ✗** |
| Account 5055473926 | 630 | −0.1276 | −2.14 (thr 2.306) | −1.93 (thr 2.447) |

**The previous version of this row was wrong within thirteen trades, and that
is the most useful thing on this page.** On 09-18 it recorded the pooled
symbol-clustered t at −2.53 against a 2.447 threshold and called the result
"significant under symbol clustering". One merge later, at n=863, the same
statistic is **−2.34 and does not cross**. Nothing changed but the sample.

`CLAUDE.md` already contains this exact warning, from the 20-trade era: *"On
20 trades this account scored +4.60 at a pooled t of 3.27; seven trades later
it was +3.24 at 1.15. Nothing changed except that the sample grew. Do not
quote a live t-stat without saying how many trades it rests on."* The lesson
was written down and then not applied to the very next reading.

So the supportable statement is narrower than last time, not wider:

- **Pooled, the result is not significant under any clustering.** 213 more
  trades are needed for the pooled t to reach 1.96 at this effect size.
- **In regime 1.0 — 832 of the 863, so nearly all of it — it is significantly
  negative under both date and symbol clustering.** That is the reading to
  quote, and it is the expected direction: the spread is paid either way, and
  `cost-hurdle` has said so from the beginning.

Standing caveats from the report: two bracket regimes (reward:risk 0.2 to 1.0),
so quote `by_regime` or the R-multiple and never the pooled net; two sources
merged, so quote `by_account`; R available for 863 of 882; 1 bracket inverted
at fill; max absolute slippage 14.0 points.

## Completed

- Full architecture audit → `docs/ARCHITECTURE_AUDIT.md` (A–Q)
- Crash root-cause analysis → `EMERGENCY_STABILITY_AUDIT.md`
- Loss classification over 476 venue-read trades → `LOSS_ROOT_CAUSE_REPORT.md`
- **Disconnect defect fixed** on `account_info()`, and **proven on 09-16 to
  generalise** to `positions_get()` — see the session above
- **L83 opportunity research core** → `app/research/opportunity.py`, 27 tests
- **L86 allocation validation** → `app/portfolio/allocation.py`, 24 tests
- **P4 risk vetoes**: weekly loss, consecutive losses, correlation. A MISSING
  input fails each check rather than passing it
- **Windows build** — `build_exe.py`, onedir by default because onefile cost
  five seconds a command. Frozen-mode root resolution fixed across four more
  modules, with tests
- **Sleep detection** — a session now says when the machine slept under it.
  Only in the pass loop; see the defect above
- **Crash journal now holds 9 sessions**, and its statuses are load-bearing:
  `completed`, `ended_not_flat`, `abandoned` and an unreadable account are four
  different things and the journal distinguishes all four
- **The watchdog is launched** — `run_overnight.py` starts it with `--adopt`
  for every full session, in Python rather than the batch file because the
  frozen executable never touches `start-trading.bat`. Full sessions only: a
  `--harvest-only` wind-down is deliberately unsupervised, since restarting
  one that died would re-enter the book it was emptying
- **Eighth and last input searched.** Volume was the only thing left that is
  not a function of OHLC, and `crypto_market.py` had been downloading it and
  discarding column 5 since the universe was added. Searched, null, control
  beat the treatment
- **`run.bat`** — the operator entry point, 17 options
- **`tools/preflight.py`** — 11 checks, read-only, and the only thing in the
  toolkit that would have said "on battery" before 06:00

## In Progress

Nothing is running and the account is flat, so for the first time this month
there is no unattended book. What is open is **evidence, not construction**:
Criticals 1 and 2 are fixed in code and neither has survived a night yet.

The next session is **Sunday 20 September**, for a Monday 06:00 deadline. A
Friday-night run was refused by the venue-week guard when it was dry-run at
16:12 today, which is the guard working: *"the next 06:00 deadline inside the
venue's week is Mon 21 Sep."*

## Blocked

- **Base rates, expectation gaps, channel and guidance intelligence** — no data
  source exists. Not stubbed; they return `insufficient_data` naming the gap.
- **Path B end-to-end demo validation** — `accounts_broker: 0`, so the platform
  path refuses with `no_venue`.
- **Overnight soak evidence** — blocked on the host, not the code. Every
  full-length run since 09-12 has lost hours to modern standby.

## Critical Issues

1. ~~**Modern standby sleeps through sessions, and the fix does not hold.**~~
   **Root cause found and fixed 2026-09-18, pending a live soak.** The two
   earlier fixes both reached for `SetThreadExecutionState`, which cannot
   express the request that matters: the Desktop Activity Moderator SUSPENDS
   desktop applications under Modern Standby, and only
   `PowerRequestExecutionRequired` — which has no `ES_` constant — exempts a
   process from it. The session now takes a real power request, verified
   GRANTED on this machine. **Not closed until a full-length session runs
   without a gap**, because that is the evidence the last two fixes lacked.
   Detail in [Modern standby](#modern-standby-is-eating-the-soak).
2. ~~**The flush retry budget is counted in attempts, not wall-clock.**~~
   **Fixed 2026-09-18.** Attempts are a floor, the budget is 10 minutes of
   wall-clock, and a mid-flush sleep restarts the budget rather than spending
   it (capped at 3 restarts). The flush also no longer believes a retcode:
   after every close is accepted it re-reads the book, and a DONE that left
   the position open keeps the loop running instead of reporting flat.
3. **Two independent paths to a broker order.** *Reduced 2026-09-11, not
   closed.* The harness imports `app.risk.engine` through `tools/risk_gate.py`,
   and `place()` refuses a live order carrying no risk decision —
   `RISK_GATE_MISSING`, nothing sent. What still differs from Path B: no OMS,
   so no `orders` row, no `intent_id`, no reconciliation; the platform's kill
   switches are deliberately not read; weekly loss and correlated exposure have
   no data source on this path and report `not_enforced`.
4. ~~**Foreign keys are unenforced across the test suite.**~~ **Closed, and
   this row was wrong on 09-12 and again on 09-18** — it was carried forward
   twice without being checked. `backend/tests/conftest.py:46` registers an
   `event.listens_for(Engine, "connect")` listener that sets
   `PRAGMA foreign_keys=ON`, keyed on the driver module so asyncpg in the
   Postgres job is untouched. Its own comment gives the reason: one listener
   rather than 121 edits, because the suite builds 121 engines across 51 files
   and a rule repeated 121 times is one that gets missed the 122nd time. It
   reaches every session the tests open, including ones added later.
5. ~~**No session reaches its deadline.**~~ **Closed 2026-09-11**, and the
   record since is 1 clean flush in 3 attempts — and the clean one got there by
   luck. Reopening this would be the wrong call; the deadline branch works. The
   host is what fails.

## High Issues

- ~~**Nothing starts `tools/watchdog.py`.**~~ **Closed 2026-09-18.**
  `run_overnight.py` now starts it with `--adopt` for every full session —
  in Python rather than in `start-trading.bat`, because the frozen executable
  never touches the batch file and the hour is already parsed here. **Full
  sessions only:** a `--harvest-only` wind-down is deliberately unsupervised,
  since restarting one that died would re-enter the book it was emptying.
  `--no-watchdog` opts out. Verified by starting a real one and watching it
  adopt rather than launch a rival, which is the check the P1b work never had.
- ~~**A refused escape close was recorded as a close that happened.**~~
  **Fixed 2026-09-18.** When a bracket repair fails, both exits are against
  the position and holding it is a guaranteed loss, so `place()` fires one
  escape close — and `tools/mt5_paper.py` **discarded its result**, setting
  `bracket_repair_failed_closed = True` unconditionally. A close the venue
  refused was written down as a rescue, on the single order whose whole
  purpose is escaping a loss with no good branch. The flag now means what it
  says, the retcode is recorded, and a failure prints a close-it-by-hand line.
- ~~**An unanswered `order_send` was recorded as a venue refusal.**~~
  **Fixed 2026-09-18.** The call was unguarded and `status` read
  `"SENT" if done else "REJECTED"`, so a raised IPC error or a `None` from a
  departed terminal became `REJECTED`. That is the opposite claim, and it is
  the one that decides whether a retry is safe: a refusal transmitted nothing,
  an exception may have reached the server and a retry can open a second
  position. Those cases now record `UNKNOWN` and keep the error text.
- `SIGNAL_CREATED` has no consumer; `ExecutionWorker` polls instead.
- Two pass counters disagree on failure (`passes 65` in the summary,
  `"passes": 55` in the journal).
- An `AttributeError` reaches operator-facing output as its class name.
- **MT5 has no client-supplied order id.** `magic` is the ownership filter and
  `comment` is routinely overwritten by the broker, so an `intent_id` cannot
  be carried on an order and provable idempotency is not achievable on this
  platform. Read-back reconciliation is the only honest retry-safety story
  here; any design that assumes idempotency keys is wrong about MT5.
- ~~RiskEngine lacks weekly-loss, consecutive-loss and correlation vetoes.~~
  **Closed at P4.**
- ~~No watchdog across workers; no crash journal; no restart-loop limiting.~~
  **Closed at P1b for the harness** (modulo the wiring above). Still absent
  across the `app/` workers.

## What is NOT wrong

Stated because three were assumed: no live trading and none reachable without
ten deliberate code changes; **no AI in the path that traded** (`--rule random`,
no model consulted); no martingale or size escalation; and the fence held
through four sessions including two that ended badly. **Every bad outcome this
week came from the host or the venue, not from the risk path.**

## Next Recommended Action

**1.** ~~Start the terminal and read the account.~~ **Done 09-18 16:10** — the
account is flat, 0 positions, and the ledger has been merged to 882 trades.

**2. Plug the machine in, then run the preflight, and start nothing until it
says READY.** Double-click `run.bat` and choose 17, or:

```
python tools/preflight.py --until-hour 6
```

~~It is currently NOT_READY on two counts~~ — **one count, as of
2026-09-19.** The battery finding is cleared: preflight reports `on_ac_power:
on mains`, so the execution power request no longer has the five-minute
expiry Microsoft applies on DC, and the soak can produce evidence worth
having. What remains is the calendar, and it is not a fault: a 06:00 deadline
lands on the venue's Saturday, where every close is refused with 10018. The
dry run names the next one that works — **Mon 21 Sep 06:00, so start the
evening before, Sun 20 Sep.**

**3. Run one full-length session and read its timestamps.** The sleep fix and
the wall-clock flush budget are in, but **the only thing that closes Critical
1 is a night with no gap in the pass log.** Two fixes have already been
declared working on the strength of an API return value; this one is not
believed until a session proves it. Run it **on mains** — on battery the
power request lapses by design and the run is worthless as evidence.

**4.** ~~Decide whether the launcher starts a watchdog.~~ **Done 2026-09-18** —
see High Issues. Full sessions are supervised; wind-downs are not.

**5.** ~~Then the MT5 retcode table.~~ **Done 2026-09-18** (`6e3ee22`) —
`tools/mt5_retcodes.py`, seeded only from the eight codes this repository has
actually observed across 2,180 ledger rows. An unseen code classifies as
`UNCLASSIFIED` and is surfaced as its raw integer rather than guessed at. It
carries two fields rather than one, and the second is the one callers use:
`transmitted` asks whether the request reached the server, `booked` asks
whether anything exists at the venue because of it. Finding a 10025 that had
been treated as a failed repair cost one position.

**6. Then a bounded, verified reconnect on `VenueUnreadable`** — Critical 4.
Verify by identity, not by the call answering: re-read `account_info()` and
assert the login matches the one captured at session start, because
`positions_get` answering while `account_info` returns `None` is the partial
disconnect this project has already hit. Bound it on the time left to
`flat_at`, never on an attempt count.

Do not train a model. `ModelTrainingNeedAssessment` = **DO_NOT_TRAIN**: six
search families across five universes have already failed to clear their own
permutation nulls, and in the bracket regime holding 832 of the 863 live
trades the measured result is significantly negative under both clusterings.
A model would search the same space with more parameters.

The pooled figure is deliberately not quoted there. It crossed its threshold
on 09-18 and stopped crossing it thirteen trades later, which is the strongest
argument on this page for not reading a live t-stat as a verdict — including
when it says what you expected.

## Working tree

Clean, on `main`, and in step with `origin/main`. **Local `main` was rewritten
on 2026-09-18** — every hash below `28fbc1b` changed and the remote-tracking
refs were dropped — and the rewritten history has since been published, so the
divergence that needed a force-push is resolved. Anyone who cloned before
09-18 has the old hashes and will see a divergence on their next pull; that is
a consequence of the rewrite and is worth telling them rather than letting
them discover it.

**`.gitattributes` now pins `*.bat`, `*.cmd` and `*.ps1` to `eol=crlf`**, and
that is load-bearing rather than cosmetic. `run.bat` was written LF-only, and
cmd.exe reads a batch file by seeking through it: with bare LF its label
resolution is unreliable, `goto :menu` silently did not jump, and the menu ran
a live ledger merge three times on an empty stdin while its own guard
correctly reported that no input had arrived. No error, nothing in the output.
Verified fixed by doing an actual fresh clone and checking the bytes.

The ledger and the reports are gitignored, so ledger movement shows up in
`data/track_record.jsonl` and `reports/track_record.json` rather than in the
diff.

`PROJECT_STATE.json` is still stamped 2026-09-07 and was again deliberately not
regenerated: it measures the running deployment, and neither `tr-postgres` nor
the API is up. Run `python tools/project_state.py --write` with the stack
running rather than editing it by hand.
