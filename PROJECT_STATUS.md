# PROJECT_STATUS.md

Generated 2026-09-18 from the repository, the crash journal, the session logs
and the ledger. **The venue was not read** — no MT5 terminal is running, so
every account figure below is the last one a session managed to observe.

**Current Status:** `HARNESS_FENCED` → `SOAK_REQUIRED`, and the soak is not
producing clean evidence because the host keeps falling asleep.

> **THE ACCOUNT HAS NOT BEEN READ SINCE 2026-09-16 08:46.** Session
> `20260916-082522` lost the venue mid-flight, gave up by design, and recorded
> the book as UNKNOWN rather than flat. Seven positions were open at the last
> pass it could read. Nothing has managed them for two days. **Check the
> terminal before starting anything.**

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
| **Account** | `5055473926 @ MetaQuotes-Demo` **[DEMO]** · **UNKNOWN since 09-16 08:46** — 7 positions open at the last readable pass, closing balance unread. Last observed balance 99,914.31 USD at that session's start |
| **Running processes** | none. **MT5 terminal is not running either**, which is why the book cannot be checked from here |
| **TradingView Status** | gateway built (hmac, age check, idempotency, body cap). **Never exercised end to end** — no broker account registered |
| **MT5 Status** | one adapter, one `order_send` on the platform path; 4 on the harness path, all four behind the Risk Engine — the opening order needs an `Approval`, the three closing ones are recorded and never refused |
| **Risk Status** | 33+ veto codes — weekly loss, consecutive losses and correlation added at P4. RiskEngine is the final veto on **both** paths as of P2 |
| **Windows Build Status** | **exists.** `dist/trade-research/` onedir, rebuilt 09-15 09:36, plus a legacy onefile `dist/trade-research.exe` from 09-12. Built by `build_exe.py`. *(This row said "none" until 09-18; it was stale.)* |
| **Stability** | **three sessions have now reached a deadline; one of the three flushed clean.** The limiting factor is no longer the code path — it is the host sleeping. See [Modern standby](#modern-standby-is-eating-the-soak) |
| **Tests** | **not re-run since 09-12.** Last recorded: 9 toolkit gates (py3.14) · full backend suite 2,976 passed / 0 failed (21m33s) · `ruff` + `mypy` clean, 411 files. Eight commits have landed since, four of which changed project-root resolution |

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

**869 trades** (`data/track_record.jsonl`), merged 2026-09-16 08:18 — **stale by
one session**, because `20260916-082522`'s 13 opens and 6 harvests closed after
that merge and have never been pulled in. Run `python tools/track_record.py
--merge` once the terminal is up.

Account `5055473926`: **617 trades, net −85.55**, 09-04 to 09-16, win rate
78.1%. The older 252 stay `unrecorded` at −22.37.

The R-multiple read has moved, and not in the project's favour:

| | n | mean R | t pooled | t by date | t by symbol |
|---|---|---|---|---|---|
| Pooled | 850 | **−0.0265** | −1.87 | −1.75 (thr 2.131) | **−2.53 (thr 2.447) ✗** |
| Regime 1.0 | 819 | −0.1353 | −2.23 | **−2.57 (thr 2.131) ✗** | **−2.80 (thr 2.447) ✗** |

At the 09-12 update this was n=741, mean −0.014R, t=−0.92 — indistinguishable
from zero. It is now mean −0.0265R with the symbol-clustered t crossing its
threshold, and in the dominant bracket regime both clustered tests cross.
Pooled and date-clustered still do not, and 89 more trades are needed for the
pooled t to reach 1.96, so the honest statement is narrow:

**This is no longer "no measurable edge". It is a negative result that is
significant under symbol clustering and not yet under the others.** Which is
the expected direction — the spread has to be paid either way, and
`cost-hurdle` has said so from the beginning.

Standing caveats from the report: two bracket regimes (reward:risk 0.2 to 1.0),
so quote `by_regime` or the R-multiple and never the pooled net; two sources
merged, so quote `by_account`; R available for 850 of 869; 1 bracket inverted
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

## In Progress

Nothing is running. The open item is not work, it is an unattended book:
**7 positions believed open at `5055473926`, unmanaged since 09-16 08:46.**

## Blocked

- **Base rates, expectation gaps, channel and guidance intelligence** — no data
  source exists. Not stubbed; they return `insufficient_data` naming the gap.
- **Path B end-to-end demo validation** — `accounts_broker: 0`, so the platform
  path refuses with `no_venue`.
- **Overnight soak evidence** — blocked on the host, not the code. Every
  full-length run since 09-12 has lost hours to modern standby.

## Critical Issues

1. **Modern standby sleeps through sessions, and the fix does not hold.**
   *New 2026-09-18.* Promoted above the two-paths finding because it has now
   caused an actual abandoned book, twice, and because it invalidates the soak
   the project is waiting on. Detail in
   [Modern standby](#modern-standby-is-eating-the-soak).
2. **The flush retry budget is counted in attempts, not wall-clock.** Six
   retries inside 50 seconds after a two-hour sleep is one reconnect attempt
   wearing six hats. On 09-14 it cost seven positions.
3. **Two independent paths to a broker order.** *Reduced 2026-09-11, not
   closed.* The harness imports `app.risk.engine` through `tools/risk_gate.py`,
   and `place()` refuses a live order carrying no risk decision —
   `RISK_GATE_MISSING`, nothing sent. What still differs from Path B: no OMS,
   so no `orders` row, no `intent_id`, no reconciliation; the platform's kill
   switches are deliberately not read; weekly loss and correlated exposure have
   no data source on this path and report `not_enforced`.
4. **Foreign keys are unenforced across the test suite.** SQLite runs with the
   pragma off; only `orders.signal_id` is covered. 2,976 tests are weaker
   evidence than the count suggests.
5. ~~**No session reaches its deadline.**~~ **Closed 2026-09-11**, and the
   record since is 1 clean flush in 3 attempts — and the clean one got there by
   luck. Reopening this would be the wrong call; the deadline branch works. The
   host is what fails.

## High Issues

- **Nothing starts `tools/watchdog.py`.** Unchanged, and now with evidence: the
  one launch attempt in the logs (09-14 23:16) failed with
  `unknown command 'S:\PROJECTS\trade-research\tools\run_overnight.py'`, which
  is what prompted `76dbc70` a minute later. The frozen-mode bug is fixed;
  **the wiring still does not exist** — `grep -i watchdog start-trading.bat
  tools/run_overnight.py` returns nothing. A supervisor nobody launches would
  have restarted three of the last four sessions.
- `SIGNAL_CREATED` has no consumer; `ExecutionWorker` polls instead.
- Two pass counters disagree on failure (`passes 65` in the summary,
  `"passes": 55` in the journal).
- An `AttributeError` reaches operator-facing output as its class name.
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

**1. Start the terminal and read the account.** It is UNKNOWN, has been for two
days, and nothing below matters until it is known:

```
dist/trade-research/trade-research.exe account
```

**2. Close whatever is there**, watching rather than scheduling:
`start-trading.bat --harvest-only`. Then `python tools/track_record.py --merge`
to pull in the 09-16 session's trades, which the ledger has never seen.

**3. Fix the sleep before running another soak.** Find out why the S0
keep-awake does not hold — a session that loses five hours is not evidence, and
three of the last four produced none. Extend the sleep detector to the deadline
and flush paths, and make the flush retry budget wall-clock.

**4. Then decide whether the launcher starts a watchdog.** It is the last loose
end from P1b and it stopped being a pure design question this week: three of
the last four sessions ended in a state a supervisor would have acted on.

**5. Re-run the suite.** It has not run since 09-12 and eight commits have
landed, four of them touching project-root resolution.

**6. Then P5** — foreign keys and a Postgres-backed integration job, Critical
#4 and the highest test-integrity win left.

Do not train a model. `ModelTrainingNeedAssessment` = **DO_NOT_TRAIN**: six
search families across five universes have already failed to clear their own
permutation nulls, and the live ledger has now gone from "no edge" to
"significantly negative under symbol clustering". A model would search the same
space with more parameters.

## Working tree

Clean, on `main`, **8 commits ahead of `origin/main` and unpushed** — the
modern-standby work, frozen-root resolution across four modules, the watchdog
frozen-mode fix, and the perf-audit docs section. The ledger and the reports
are gitignored, so ledger movement shows up in `data/track_record.jsonl` and
`reports/track_record.json` rather than in the diff.

`PROJECT_STATE.json` is still stamped 2026-09-07 and was again deliberately not
regenerated: it measures the running deployment, and neither `tr-postgres` nor
the API is up. Run `python tools/project_state.py --write` with the stack
running rather than editing it by hand.
