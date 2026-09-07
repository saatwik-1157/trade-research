# LEVEL 22 — AUTONOMOUS BOT MANAGER

Completed 2026-09-04. Adaptive build mode: audit → baseline → plan → implement
→ integrate → test → verify → document.

The Bot Manager answers *what should be running, and is it actually running?*
It starts nothing that trades, evaluates no strategy, sizes no position and
submits no order — a test parses the whole package to prove it holds no
adapter and cannot reach the OMS.

---

## 1. What already existed

`MIGRATION_STATUS.md` recorded L22 as **NOT STARTED**, and that was too harsh.
A working bot runner has existed since L16.

| Component | State before | Verdict |
|---|---|---|
| `bots` / `bot_runs` / `bot_events` tables | L05, with pid, host, heartbeat, stop reason and a run summary | **KEEP + MODIFY** |
| `PaperService.start_bot / pause_bot / resume_bot / stop_bot` | A full paper lifecycle in background asyncio tasks that survive a closed browser | **KEEP + MODIFY** |
| `RunningBot` with a frozen config | Editing a bot changes the NEXT run, which is §11's versioning discipline already in force | **KEEP** |
| `/v1/paper-trading/bots` | List, start, control, get, events | **KEEP** |
| `app/workers/base.py` | `Worker` with heartbeat, staleness, cooperative shutdown; `WorkerRegistry` | **KEEP**, reused |
| Kill switches, scoped risk limits | L17 | **KEEP**, and the bot layer cannot bypass them |
| `ExecutionWorker` (L20), `PositionMonitor` (L21) | Built, registered, idle | **KEEP** |

**Nothing was duplicated.** No second bot manager, runner, scheduler, worker
system, queue or event bus was created.

---

## 2. The defect this level fixed

`PaperService.pause_bot` set the in-memory status to `paused` and wrote
**`stopping`** to `bot_runs`, because the table had no value for it:

```python
bot.status = "paused"
await self._set_run_status(bot, "stopping", "paused by the user")
```

So **a paused bot and a bot shutting down were the same row.** After a
restart, nothing could tell them apart — and the two need opposite treatment.
A supervisor reading that row would either resume a bot somebody had
deliberately paused, or abandon one that was only mid-shutdown. Both are
wrong, and only one of them is visible.

Migration `0012` adds `paused`, and `plan_restart` now acts on the difference:
a paused run is preserved as paused, a `stopping` one is an orphan whose
process is gone. `test_a_paused_run_is_recorded_as_paused` drives both through
the planner, which is what makes the fix observable rather than cosmetic.

---

## 3. The latent bug the work exposed

Importing `app.paper.service` **first** raised `ImportError: cannot import name
'AiFilter' from partially initialized module 'app.paper.engine'`.

`app/paper/engine.py` imported `app.execution.outcome`, which executes
`app/execution/__init__.py`, which imports `pipeline`, which imported
`AiFilter` back out of `app.paper.engine`. A cycle introduced at L20 and
invisible because the test suite's import order never hit it.

The fix is not an import shuffle: the AI seat is **not a paper concept**. The
orchestrator uses it, a demo bot would use it, and a module two pipelines
depend on cannot live inside one of them. `AiVerdict` and `AiFilter` moved to
`app/execution/ai.py`, re-exported from `app/paper/engine.py` so every
existing caller is unchanged.

---

## 4. What was added

### 4.1 Nine states, six of them under their own names

The brief names `ERROR`; this project has always called it `crashed`, which is
more specific and is what every existing row says. Renaming it would rewrite
history to match a document. `halted` likewise is this project's word for "a
kill switch stopped it", which is neither a crash nor a stop.

Three are new:

| State | Why it had to exist |
|---|---|
| `paused` | §2 above. |
| `recovering` | A supervisor is restarting it **right now**. Distinct from `crashed`, where nobody is acting — only the first means an answer is coming. The same distinction `reconciling` draws from `unknown` for a position. |
| `disabled` | Prevented from running until explicitly re-enabled. Distinct from `stopped`, which anyone may start again. |

`created` is deliberately **not** a run state. A `bot_runs` row exists because
a run was attempted; inventing one for "configured but never started" would
make the table lie about how many times a bot has run.

`MAY_TRADE` is the single-element set `{running}`. Pausing, stopping and every
failure state all mean "no new trades", and collapsing that into "not stopped"
would let a crashed bot keep trading.

### 4.2 Bot limits that can only tighten

§17's rule holds by **arithmetic, not by a check somebody has to run**.
`BotLimits.effective()` takes the more restrictive of the bot's figure and the
account's in every field, so a bot asking for 5% when its account permits 2%
gets 2% — the combination cannot produce a looser number than either input.

Deliberately not "validate at write time and reject": a limit that was legal
when saved can become illegal when the account tightens, and combining at read
time means the tighter figure wins *whenever* it is tighter.

`cooldown_seconds` is the one field where the **larger** number wins, because
a longer cooldown is the more restrictive one. Getting that backwards would
let a bot shorten a cooldown its account imposed.

`BotCounters` is read from the database every check, never cached: a counter
kept in memory comes back as zero after a restart, and a bot that had used 9
of 10 daily trades would then take another 10.

### 4.3 A supervisor that measures rather than reads

*"The database says RUNNING" is not evidence that a bot is running* — §26's
sentence, and the whole reason the module exists. A row says what the last
process to touch it believed; a heartbeat says something is still alive.

A silent run is marked `crashed` and **not restarted**. Recovery is a separate
pass, because marking a run dead and deciding to restart it are different
decisions and doing both in one loop makes the second invisible.

**Recovery refuses by default.** With no safety check wired, every attempt is
refused with that as the reason. A supervisor that restarted everything
because nobody told it not to would be the most dangerous default in the
system, so the absence of a check is treated as absence of evidence rather
than as permission.

`halted` is not recoverable. A kill switch is a decision somebody made, and
recovering out of it automatically would be exactly the bot-level bypass §22
forbids.

### 4.4 Restart recovery that resumes rather than re-runs

`plan_restart` **plans and does not act**, so a caller can log the whole plan
before any of it happens. A `stopped` bot stays stopped, a `paused` bot stays
paused, a disabled bot is left alone, and a bot the row calls `running` is one
whose process is gone — marked `crashed` for the supervisor to consider, never
silently started. §29's "do not blindly start every bot" is the sentence this
encodes.

### 4.5 `/v1/bots`, and `preflight`

A control plane. `POST /{id}/preflight` runs every gate a start runs and
**starts nothing**, so a caller can see why a bot will not run without having
to attempt it and read an error.

A live-mode bot fails preflight while `LIVE_TRADING` is false, and the refusal
**names the setting rather than the request** — a caller retrying with
different JSON should learn that the answer will not change.

`disable` closes no position, and says so in its response. An operator who
believes STOP or DISABLE is a flatten button will eventually press one
expecting that.

---

## 5. Changes, classified

**ADD** — `app/bots/` (`state.py`, `limits.py`, `supervisor.py`, `worker.py`),
`app/execution/ai.py`, `app/api/v1/bots.py`,
`alembic/versions/0012_bot_lifecycle.py`, `tests/test_bots.py` (36 tests), 13
API tests, a real bots page.

**KEEP + MODIFY** — `app/models/bots.py` (three states, five limits, the
disable flag), `app/paper/service.py` (the paused fix; imports the shared
machine), `app/paper/engine.py` (re-exports the AI seat),
`app/execution/pipeline.py` (imports the seat from its new home),
`app/api/pending.py` (the `/bots` 501 row is gone — the group is built),
frontend `BotStatus` and bot service.

**REFACTOR (MOVE)** — the AI seat out of the paper engine, breaking a real
import cycle.

**REMOVE** — the `/bots` pending row, because the group exists; and
`BotsTable.tsx`, a second table over the same rows I had briefly added, folded
into `BotStatus` so the health rule lives in one place.

---

## 6. Safety invariants (§59)

| # | Invariant | How it holds |
|---|---|---|
| 1–3 | Cannot bypass risk, sizing or the OMS | `app/bots` imports none of them; a test parses every module. |
| 4–6 | Bot, TradingView and AI cannot call MT5 | No adapter is reachable from this package. |
| 7 | Duplicate signals cannot duplicate orders | Unchanged: the OMS's `intent_id` is unique. |
| 8, 9 | Paused and stopped bots cannot trade | `MAY_TRADE == {running}`. |
| 10 | Positions stay managed after a stop | Nothing here touches a position; disable says so explicitly. |
| 11 | The kill switch stops new trading | `halted` is not recoverable, and preflight checks the switches. |
| 12 | Live disabled by default | Preflight refuses a live bot and names the setting. All ten gates false. |
| 13, 14 | Disconnect and unknown state | Unchanged from L19/L21; recovery refuses without a safety check. |
| 15 | Bot limits cannot exceed global | `effective()` cannot produce a looser figure than either input. |
| 16 | One bot's failure does not corrupt another | The sweep handles each run independently; a restart that raises marks only that run. |
| 17 | Browser closure stops nothing | Runners and the supervisor are `app.workers.Worker`s. |
| 18 | State survives restart | `plan_restart` reads the rows and preserves deliberate states. |
| 19 | Strategy version changes are explicit | `RunningBot` freezes its config at start; editing changes the next run. |
| 20–22 | No secrets, no future data, history preserved | This package holds no credential; additive migration; nothing deleted. |

---

## 7. Tests

| Suite | Baseline | After L22 |
|---|---|---|
| Backend | 1140 passed, 15 failed, 3 skipped | **1187 passed, 15 failed, 3 skipped** |
| Frontend | 83 passed | **84 passed** |
| Research toolkit | 34 passed | 34 passed, untouched |

The 15 backend failures are the same 15 in both columns: `redis.exceptions`
because Docker was not running on this machine. Environmental, and named as
such rather than counted as a pass.

`tests/test_bots.py` is **36 tests** where there were none. `tests/test_api_v1.py`
gains 13, `tests/test_auth.py` gains 2, and the frontend suite gains 1.

Lint, format and type checks are clean: `ruff check`, `ruff format --check`
and `mypy` across 202 backend source files; `eslint` and `tsc --noEmit` on the
frontend.

The three that matter most:

- `test_a_paused_run_is_recorded_as_paused`
- `test_a_bot_limit_can_only_tighten_an_account_limit`
- `test_recovery_is_refused_when_no_safety_check_is_wired`

### 7.1 What the full run caught, and the code fix it forced

The suite is run in full for a reason, and this level is the argument for it.
Every targeted run passed — `test_bots.py` 36, `test_api_v1.py` 90,
`test_paper.py` with bots 129 — and the full run then failed **three tests in
`test_auth.py` that L22 never touched**.

One of them was a real authorization regression I had shipped.

`GET /v1/bots` was a 501 stub gated on `Permission.manage_bots`, so a plain
USER got 403. The router that replaced it asked only for a logged-in user, so
**a fresh USER account could list every bot, its mode, its limits and why it
last stopped.** `RESOURCE_MIN_ROLE["bots"]` has said TRADER since L04 and the
frontend nav mirrors it; the backend had quietly stopped agreeing.

Fixed by giving reads the same permission as writes. There is no `view_bots`,
and adding one to widen this would be inventing a permission in order to grant
access rather than to describe it — a bot row is the operational state of an
automated trader, not the read-only view of results a new account is given.

The other two were stale rather than broken, and both are the pattern earlier
levels set: `/bots` unversioned is now 404 because a built group drops the
alias that carried its 501 (as `/orders` did at L06), and `/v1/bots` left the
pending-routes list. Both parametrisations record which level removed them.

What replaced that coverage is the more useful test:
`test_building_a_group_does_not_loosen_the_gate_it_replaced`. It is the rule
this level got wrong, written down: a built group inherits the gate of the stub
it replaces.

### 7.2 Two mypy errors and three stale nav rows, fixed in passing

`mypy` over the whole tree (202 files, not just `app/`) reported four
`payload["..."]` accesses on a `dict | None` in `tests/test_positions.py` and
one unused `type: ignore` in `tests/test_execution.py` — all from L20/L21 and
all in test files. Fixed rather than left, because a type gate that is known
to be red stops being read.

`frontend/src/lib/nav.ts` still described `/orders`, `/positions` and `/bots`
as `planned`, whose documented meaning is "shell only; every control disabled".
That was false for all three after L19, L21 and L22. They are now `partial`,
which is the honest word: real data, no lifecycle controls yet.

---

## 8. Remaining, and honest about it

- **Only paper has a runner.** The OMS (L19) and the broker executor (L21) are
  built, but nothing drives a *strategy loop* against them, so a demo or live
  bot has nothing to start. `preflight` refuses those modes and names what is
  missing rather than starting a bot that cannot trade.
- **The supervisor worker is registered and not started**, like the execution
  worker and for the same reason: beginning to supervise is an operator
  action. `POST /v1/bots/supervise` runs one sweep on demand, which is what
  makes the mechanism usable before anybody turns the loop on.
- **No safety check is wired to the supervisor**, so every recovery is
  refused. Wiring it means asking the risk service about kill switches and the
  OMS registry about unresolved orders — both exist, and connecting them is a
  deliberate step rather than a default, because the failure mode of getting
  it wrong is a bot restarted into an unsafe account.
- **Bot limits are enforced at the API and in the manager, not yet inside the
  paper runner's own loop.** The runner has its own risk engine per bot with
  the account's limits; threading the bot's tighter figures into that engine
  is the next wiring step, and until then the bot limits gate the manual and
  API paths rather than the autonomous one. Stated rather than implied.
- **Scheduling (§46) is not built.** No start/end time, session or timezone
  window. The platform has no scheduler to reuse, and adding one for a feature
  nobody has asked to configure would be the speculative work this brief warns
  against.
