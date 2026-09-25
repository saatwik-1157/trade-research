# IMPLEMENTATION_PRIORITY.md

Work sorted by priority, 2026-09-02; the CRITICAL and HIGH bands were marked
up as items landed, most recently 2026-09-04 (L22). Within each band the order
is the dependency order from `ARCHITECTURE_MIGRATION.md` §5.

**Struck-through rows are done.** They are kept rather than deleted because the
*reason* an item was critical is the part worth preserving — a row that
disappears takes its own justification with it, and the next person cannot tell
whether it was built or dropped.

The organising principle: **build the veto before the path it guards.** Every
critical item below either creates an execution path or prevents one from
existing unguarded.

---

## CRITICAL

Nothing may place an order until all four exist. Three of the four now do, and
the fourth is the one that matters most on the day anybody points this at a
real account.

| # | Work | Level | Why critical | Gate to pass |
|---|---|---|---|---|
| 1 | ~~**BrokerAdapter, MT5Adapter, FakeBroker**~~ **DONE at L10** | 10 | Seven levels are blocked behind it; two built levels (21, half of 16) cannot become real without it. Also merges the four duplicate `connect()` implementations, the worst duplication in the repository | Order-construction tests unchanged and passing; `mt5_account.py` output identical on the demo account |
| 2 | ~~**Risk Engine and kill switches**~~ **DONE at L17** | 17 | The veto must exist *before* the OMS, so no order path can be built that predates it. Global, account and strategy kill switches | Engine rejects exactly what `cycle()` rejects, on `FakeBroker`; with the switch on, the fake broker receives zero orders |
| 3 | ~~**OMS with idempotency and reconciliation**~~ **DONE at L19** | 19 | `orders.intent_id` is unique and unused; `place()` still treats an unknown send as a rejection, which can double-send on retry | A crash between send and log is recovered by reconciliation, never by retry; old order-log rows still readable |
| 4 | **Startup and disconnect reconciliation** | 38 | A restart resumes with no idea what the broker holds, and MT5 disconnect does not stop new orders. L19, L21 and L22 each built the planner for their own half of this — `OrderManager.reconcile`, `PositionReconciler`, `plan_restart` — and **none of them runs at boot**, which is exactly the gap this item names. The pieces exist; nothing calls them first | Duplicate, missing and unknown states each resolved without creating an order |

## HIGH

| # | Work | Level | Why | Depends on |
|---|---|---|---|---|
| 5 | ~~**Backend API surface**~~ **DONE at L06** | 06 | 24 route groups missing; eight later levels need them. Service layer, request IDs, error handling | 05 ✓ |
| 6 | ~~**Position sizing module**~~ **DONE at L18** | 18 | `lot_for_risk` works and is tested; it needs a home the Risk Engine can call | 17 |
| 7 | ~~**Strategy interface and registry**~~ **DONE at L12** | 12 | Rules return bare strings; the pipeline needs Signal objects with ids and versions | 10 |
| 8 | ~~**Paper trading through the pipeline**~~ **DONE at L16** | 16 | The DEMO loop must route through Risk → Sizing → OMS rather than calling `place()` directly | 10, 12, 17, 18, 19 |
| 9 | ~~**Automated execution wiring**~~ **DONE at L20** | 20 | Only a confirmed execution becomes a trade; six failure paths to test | 19 |
| 10 | ~~**Realtime: Redis bus and WebSockets**~~ **DONE at L07** | 07 | Redis runs and nothing uses it; bots and the UI both need events | 06 |
| 11 | ~~**Bot manager and workers**~~ **DONE at L22** | 22 | Bots must survive a closed browser; orphaning is a known live failure. Both were already true — the runners have been `asyncio` tasks since L16 — so what L22 added is the part that was missing: something that *checks*. A heartbeat is measured, a silent run is marked `crashed`, and a restart plan preserves what an operator deliberately chose instead of starting everything back up | 07 ✓, 20 ✓ |
| 12 | ~~**Market data provider interface**~~ **DONE at L08** | 08 | Four fetchers, no common shape; needed by replay, paper and the UI | 11 ✓ |
| 13 | ~~**TradingView gateway hardening**~~ **DONE at L09** | 09 | Replay protection, timestamp validation, idempotency before any path to execution exists | 06, 16 |
| 14 | **Security hardening** | 39 | Rate limiting, CSRF, encrypted broker credentials, headers | 06 |
| 15 | **Integration testing** | 40 | End-to-end pipeline and 15 failure scenarios | all above |

## MEDIUM

| # | Work | Level | Why |
|---|---|---|---|
| 16 | Trade journal service | 31 | Auto-record on close; the ledger already exists |
| 17 | ~~Analytics service and charts~~ **DONE at L32** | 32 | Engine exists as CLI; needs Sharpe, Sortino, filters, API |
| 18 | Portfolio service | 30 | Exposure by currency matters because seven USD pairs are one bet |
| 19 | ~~Market replay driver~~ **DONE at L15** | 15 | Both pieces exist; needs the driver and a leakage test |
| 20 | ~~Backtesting API and async runs~~ **DONE at L14** | 14 | Engine complete; expose it |
| 21 | ~~Strategy builder~~ **DONE at L13** | 13 | Over the existing factories |
| 22 | Admin control centre | 36 | 14 sections; admin must not bypass risk |
| 23 | Notifications engine | 34 | Table exists; channels and preferences missing |
| 24 | System monitoring | 37 | Per-dependency endpoints, latency, queue depth |
| 25 | Deployment split | 41 | dev/prod Compose, worker and scheduler services |

## LOW

| # | Work | Level | Why low |
|---|---|---|---|
| 26 | UI panels filled in | 03 | Each fills when its data source lands; the shell is done |
| 27 | ~~AI data pipeline~~ **DONE at L23** | 23 | The reason it was LOW turned out to be half right and half wrong. "252 rows is not a dataset" still holds — the trade record is not a training set. But the journal was never the blocker: `market_bars` is, and it has existed since L08 with validation, provenance and idempotent ingestion. The pipeline is built against that instead, and the 252 trades remain a stated gap because the imported ledger carries no bar reference to join on |
| 28 | ~~AI models and config~~ **DONE at L24** | 24 | Built as an advisor with no authority: three families, one prediction shape, and a gate that refuses rather than defaulting. The reason this was LOW is unchanged and still correct — there is no hypothesis, and the project's own searches have yet to find a rule clearing its permutation null out of sample. What L24 adds is that a model which *appears* to clear it can now be checked: versioned inputs, a deterministic fit, and a probability that names the label it is the probability of |
| 29 | ~~AI training~~ **DONE at L25** | 25 | The reason it was LOW has half changed. There is now something to train ON - a versioned, leakage-checked dataset and three model interfaces - so the engine was buildable. What is still missing is a *hypothesis*: the project's own searches have yet to find a rule clearing its permutation null out of sample, and no model has been trained on real data because `market_bars` covers one instrument here |
| 30 | AI strategy integration | 27 | **Now the earliest AI item that should be worked.** There IS something to integrate: `ModelBackedFilter` exists and is tested, and the seat in `app/execution/ai.py` has taken nothing since L16. What it needs first is L26's wrapper, so a model reaching the seat has cleared a permutation null rather than one held-out split |
| 31 | Model registry | 28 | Nothing to register |
| 32 | AI trade review | 33 | Needs the journal service |
| 33 | Discord | 35 | Optional by requirement; must never be required for trading |
| 34 | Final audit | 42 | Last by definition |

---

## Standing rules for every item

1. Run `python -m pytest tests -q` and the backend and frontend suites before
   and after; record both counts in `PROJECT_PROGRESS.md`.
2. **Run backend commands from `backend/`.** From the root, ruff reformats
   `tools/` and `tests/`, which are outside its scope. This happened once and
   was reverted.
3. No file under `data/` is rewritten. New fields go on new rows only.
4. `assert_demo`, `bracket_is_sane`, `lot_for_risk`, `filling_for`,
   `simulate` and `server_day_start` change only with a test that fails first.
5. Nothing is deleted; shims stay for one release.
6. No destructive migration without a justification in the migration file and
   a guard that refuses to run if the premise is false.
7. Live launches stay operator-run per `NIGHTLY.md`. No level starts one.

---

## Autonomy ladder, inserted 2026-09-03

The autonomous-builder brief adds a second ladder (A0–A9,
`TRADINGVIEW_ARCHITECTURE.md` §5). It does not reorder the bands above; it
inserts into them.

### Into CRITICAL — nothing

No autonomy item is critical, because none of them creates a path to a venue.
A8 would, which is why it is sequenced behind L19 and L38 rather than beside
them.

### Into HIGH — at the top

| # | Work | Level | Why | Depends on |
|---|---|---|---|---|
| **5a** | **Definition resolver** | A3 | A saved strategy definition cannot be backtested, replayed or paper-traded. All three runners call `registry.create()`, which resolves only classes registered at import; `BuiltStrategy` is built from data. This already limits the L13 builder that shipped, and it would make a compiled TradingView strategy unrunnable the day the compiler existed. Five brief sections route through those three call sites | L12 ✓, L13 ✓ |
| 6a | Source discovery + inbox | A0 | the unattended entry point; reuses the L09 gateway and `tv_import` rather than adding a second handler | L09 ✓ |
| 6b | Pine parser + declared subset | A1 | the one genuinely new body of work. Refusal is the feature: `request.security`, loops, `var`, sessions and pyramiding are recorded and the compile stops | A0 |
| 6c | Spec persistence + versioning | A2 | additive migration; new logic always mints a new version | A1 |
| 6d | Compiler + generated tests | A4 | emits the payload `parse_definition` already validates | A2, A3 |

A3 sits above the existing item 6 (position sizing) in dependency terms but not
in importance: **L18 still goes first**, because it is the level the project is
mid-way through and because A8 needs it. A3 is the first autonomy item and can
be done immediately after.

### Into MEDIUM

| # | Work | Level | Why |
|---|---|---|---|
| 19a | Validation harness | A6 | wraps L14, L15 and L26; adds Sharpe, Sortino and the TradingView divergence |
| 19b | Orchestrator | A7 | the lifecycle state machine, on the L02 worker base |

### Into LOW

| # | Work | Level | Why low |
|---|---|---|---|
| 26a | AI strategy interpreter | A5 | build-time only; the deterministic compiler still decides |
| 26b | Machine-readable project state | A9 | `PROJECT_STATE.json` is hand-written and honest in the meantime |

### Blocked

| Work | Level | Blocked on | Why it cannot be moved forward |
|---|---|---|---|
| ~~Alert → paper execution bridge~~ **DONE at L20** | A8 | ~~L18~~ ✓, ~~L19~~ ✓ | brief §20 requires the 13-state order machine with idempotency and no blind retry out of `unknown`. Building the bridge first would mean a second OMS (brief §9 forbids it) or an execution path predating the state machine that guards it |

### The resulting order

**~~L18~~ → ~~L19~~ → ~~L20 (= A8)~~ → ~~L21~~ → ~~L22~~ → A3 → A0 → A1 → A2 → A4 → A6 → A7 → A9 → A5 → L38**

**L18 through L22 are all COMPLETE** (L18-L20 on 2026-09-03, L21 and L22 on
2026-09-04)
(`LEVEL_18_POSITION_SIZING.md`, `LEVEL_19_OMS.md`,
`LEVEL_20_AUTOMATED_EXECUTION.md`, `LEVEL_21_POSITION_MANAGEMENT.md`,
`LEVEL_22_BOT_MANAGER.md`).

**A8 is done.** It was the alert-to-execution bridge, and it was blocked on
the order state machine. L19 built that machine and L20 built the consumer, so
`SIGNAL_CREATED` — published since L09 and consumed by nothing — now has
exactly one consumer, which runs every gate. The remaining autonomy work is
the TradingView ingestion side (A0–A3), not the execution side.

L38 stays ahead of anything that touches a real venue, and A8 is the only
autonomy item that gets anywhere near one.

**What L22 changes about the order.** Nothing, but it sharpens why A3 is next.
A bot in demo or live mode now fails preflight with a refusal that names the
missing piece: no strategy loop drives the OMS, because `registry.create()`
resolves only classes registered at import. That is item 5a. The bot manager
is built and it supervises exactly one kind of runner; A3 is what lets there
be another.

**What L23 changes about the order.** Nothing above it moves. A3 is still the
highest-priority item in either ladder, because a demo or live bot still has no
strategy loop to run. What L23 does is remove the reason L24 and L25 could not
be attempted honestly: there was no dataset, so any model would have been fitted
on something nobody could describe, versioned or reproduce. There is one now,
and it refuses to call itself READY when a leakage check fails.

L23 does not make L24 more likely to succeed. The project's measured position is
unchanged: across five universes and several hundred candidates, nothing has
cleared its own permutation null out of sample. A model faces the same gate.

---

## Added to HIGH by the L24 audit

| # | Work | Level | Why |
|---|---|---|---|
| **15a** | **The backend image cannot run the toolkit** | 41 | `backend/Dockerfile` installs only `backend/requirements.txt` and copies only `app`, `alembic`, `alembic.ini` and `pyproject.toml`. numpy was never declared there (now fixed) and `tools/` is never copied — yet `app/strategies/indicators.py` imports `rule_backtest` and `rule_search` from it. **In a container the strategy engine, the backtester, the replay engine and L23's feature engine all fail on import.** The tests run outside Docker and catch none of it, and `/health` imports none of it either. Fixing the second half is a build-context decision, which is why it sits here rather than being taken mid-level |

This is the first item any deployment work has to clear, and it is worth
noticing *why* it went unseen for twelve levels: every gate the project has —
tests, lint, types, CI — runs outside the artefact it is meant to be checking.

---

## The AI chain, after L28

**23 → 24 → 25 → 26 → 27 → 28 is closed.** A version reaches the active state
only through paper, its artifact is verified before every load, and every
transition names the person who made it and the reason they gave.

L28 cost less than L26 or L27 because almost everything was already there:
`model_versions` since L05, provenance from L24, candidates from L25, verdicts
from L26, and L27's eligibility boundary written specifically to be switched
over. What L28 added is the lifecycle, the artifact check, deployments and
resolution.

**Its own tests found the level's most useful defect**: the one-active-per-scope
unique index did not hold for the unrestricted scope, because NULLs are distinct
in a unique index. Absent exactly where it mattered, present everywhere it was
easy to test.

**The next item is L29 (model monitoring)**, and its inputs already exist:
`ai_decisions` carries every prediction, feature version, model version,
decision, latency and outcome; `model_deployments` says which version was active
when.

## The AI chain, after L27

**23 → 24 → 25 → 26 → 27 is closed.** A validated model can be wired into a
strategy, and the strongest thing it can do there is decline a signal the
strategy produced.

L27 cost less than L26 because the pipeline was already right: the AI seat has
sat between the strategy and the risk engine since L16, `Outcome.ai_rejected`
has been in `NO_ORDER` since L20, and L23–L26 supplied the feature engine, the
prediction contract, the registry and the verdict. What L27 added is four modes,
an eligibility boundary and a journal.

**The next item is L28 (model registry)**, and its integration boundary is
already built: `app/ai/eligibility.py` answers "may this version be used?" from
L26's verdict, and switching it to `model_versions.status` is a change to one
function.

**Read the backtest caveat before comparing an AI run to its baseline.**
Frequency is the one lever this project has shown has a sign, and it points
down. A filter that removes trades will improve total P&L on many of this
repository's own datasets for that reason alone.

## The AI chain, after L26

**23 → 24 → 25 → 26 is closed**: a versioned leakage-checked dataset, three
model families with one prediction shape and no authority, a training engine
that produces a candidate and cannot promote it, and a validation engine that
judges one and cannot promote it either.

L26 cost less than any level since L18 because its methodology has been complete
since before the platform existed — permutation null, Bonferroni, era blocks,
walk-forward, date and symbol clustering, unit fences. `app/validation/` calls
all of it and reimplements none of it.

**The first thing it did was return FAIL.** On synthetic noise, twelve checks
passed and the profit factor was 3.46, and the permutation null said p = 0.0784
with the best shuffle scoring above the model. That is the answer a rubber stamp
would have got wrong, and it is the reason to trust the next one.

**Nothing in the chain has run on real data.** `market_bars` covers one instrument on this
machine. The interfaces are verified end to end against seeded synthetic series;
no claim is made about any model's usefulness.

## After L30, 2026-09-04

Portfolio and exposure are closed. What moved, and what it changes about the
order of the remaining work:

**The risk engine's inputs now have a supplier.** `PortfolioState` was a
dataclass callers filled by hand; `to_risk_state()` fills the nine fields the
portfolio owns and DECLARES the nine it does not. That makes the gaps addressable
rather than invisible, and it points at where they live: five of the nine belong
to the trade journal and the Bot Manager.

**Three of those nine are L31's.** `trades_last_minute`, `trades_last_hour` and
`last_trade_at` are trade-journal facts, and the journal today is 252 imported
rows that nothing writes to from the live lifecycle. A rate limit the risk engine
cannot evaluate is a rate limit that vetoes — correct, and not the same as
working.

**L38 moved up in practice, not in the table.** L30's reconciliation READS and
reports; L21's repairs. Neither runs at boot, which is item 4 in CRITICAL and
remains the single largest gap between this platform and one that could be
pointed at a real account.

**The correlation gap is a data gap, not a design gap.** §27's analysis needs a
common window of market data across held instruments, and `market_bars` covers one instrument.
The currency breakdown is the honest proxy until ingestion runs — and it is the
same shared-dollar-move clustering the FX rule searches already document.

## After L31, 2026-09-04

**Three of the nine fields L30 declared unsupplied are now suppliable.**
`trades_last_minute`, `trades_last_hour` and `last_trade_at` are trade-journal
facts, and the journal now writes a row from the live lifecycle rather than only
from an import. Wiring them into `PortfolioView.to_risk_state()` is a small,
well-defined piece of work that closes a real gap: a rate limit the risk engine
cannot evaluate is a rate limit that vetoes -- correct, and not the same as
working.

**L38 is now the only CRITICAL item outstanding, and two levels lean on it.**
L30's reconciliation reports a broker mismatch; L31 records one as
`reconciliation_required` and refuses to finalise the trade. Neither runs at
boot, which is exactly item 4 in CRITICAL. The pieces have accumulated --
`OrderManager.reconcile`, `PositionReconciler`, `plan_restart`,
`PortfolioService.reconcile_for`, `TradeJournalService.mark_reconciliation_required`
-- and nothing calls any of them first.

**L32 has its inputs.** `trades` now carries environment, account, strategy
version, bot, AI decision, risk event, exit reason, each cost separately and R on
one row, and `/v1/trades` filters by every one of them. Analytics needs no new
source of truth.

**MAE/MFE is blocked on data, not design.** It needs intratrade price series and
`market_bars` covers one instrument. That is the same blocker the correlation engine has, and
both clear the moment ingestion runs.

## After L32, 2026-09-04

**The review chain is closed: 30 → 31 → 32.** The platform can say what is held,
what happened and how it performed, each from the system that owns it and with no
second copy anywhere.

**L38 is still the only CRITICAL item outstanding, and it now has four
dependants.** L30 reports a broker mismatch, L31 records one as
`reconciliation_required` and refuses to finalise the trade, L32 surfaces the
count of such trades in every summary, and none of it runs at boot. The planners
exist -- `OrderManager.reconcile`, `PositionReconciler`, `plan_restart`,
`PortfolioService.reconcile_for`, `TradeJournalService.mark_reconciliation_required`
-- and nothing calls any of them first. This is the single largest gap between
this platform and one that could be pointed at a real account.

**L33 has its inputs.** `/v1/trades/{id}/decisions` returns the strategy, AI,
risk, sizing and execution context as written; `/v1/trades/{id}/timeline` returns
the chronology; and `/v1/analytics/breakdown?dimension=model_version` gives the
outcome side at an exact model version. That is the complete "what did the system
know, and what happened next" record an AI review needs.

**The caching decision is deliberately deferred with a measurement attached.**
`calculation_ms` is on every analytics summary. When a real account accumulates
enough history for a summary to take long enough to notice, the figure to
revisit this with will already be in the response.

**The metric extraction removed a class of future defect.** There is now one
implementation of win rate, profit factor and drawdown, and the two former copies
call it. Any level that needs a performance figure should call it too rather than
writing a fourth.

## After L33, 2026-09-04

**The review chain is closed: 30 → 31 → 32 → 33.** The platform can say what is
held, what happened, how it performed and why — each from the system that owns
it, with no second copy anywhere.

**L38 remains the only CRITICAL item, and it now has five dependants.** L30
reports a broker mismatch, L31 records one as `reconciliation_required`, L32
counts them, and L33 refuses to review a trade whose close was never confirmed.
Every one of those is correct behaviour that leaves work for a reconciliation
pass which **still does not run at boot**.

**The highest-value small piece is now an LLM provider decision, not code.**
`ReviewProvider` is one method. Filling it needs a vendor, a cost ceiling and a
data-egress policy — and the payload is already assembled, already free of
credentials, and already validated on the way back. That is a procurement
decision with an hour of implementation behind it, not a level.

**Three of L30's nine `NOT_SUPPLIED` risk fields are still suppliable** and
still unwired: `trades_last_minute`, `trades_last_hour` and `last_trade_at` are
journal facts the journal now writes.

**The recurring lesson this level added to the list**: a safety test that flags
unrelated code gets suppressed. Three levels have now hit it — L27 and L28 on
grep-versus-docstring, L33 on a generic verb name. Prefer an import check over a
name check whenever the module boundary is the real constraint.


## After L34, 2026-09-05

**The platform can now tell somebody something.** Thirty-three levels of
recording, measuring and refusing, and until today none of it reached a person
who was not looking at a screen. That is the gap L34 closes, and it closes it
without a second event bus, a second queue or a notification call inside a
trading service.

**L38 remains the only CRITICAL item, and it now has six dependants.** L30
reports a broker mismatch, L31 records one as `reconciliation_required`, L32
counts them, L33 refuses to review a trade whose close was never confirmed --
and L34 has now declared the three `BROKER_*` rules that would tell an operator
about any of it. All five are correct behaviour waiting on a reconciliation pass
that **still does not run at boot**. L34 made the cost of that gap louder rather
than smaller: there is now a notification centre with a broker category that
cannot fire.

**Two small pieces are newly worth doing, both cheap:**

1. **Collapse `ModelMonitor._persist` into the event path.** L29 writes
   recipient-less `notifications` rows that nothing reads, and the same fact
   already reaches people through `MODEL_ALERT_CREATED`. Removing the direct
   write leaves one writer to the table. It belongs with L37, which owns
   monitoring's own reporting -- doing it inside L34 would have been a
   behavioural change to a level that was not being audited.
2. **Wire the three suppliable risk fields.** Unchanged from the L33 note and
   still unwired: `trades_last_minute`, `trades_last_hour` and `last_trade_at`
   are journal facts the journal writes. They are now *also* the inputs a
   `RISK_ALERT` notification would carry.

**A retention policy is now a real decision, not a hypothetical.**
`notifications` and `notification_deliveries` grow without bound by design and
nothing in this platform deletes anything on a timer. Section 49 forbids
inventing one, so L34 documented the shape instead: per-category age limits on
READ notifications only, never `SECURITY`, never `CRITICAL`, never a failed
delivery's audit row. Someone has to choose the numbers.

**The `SECURITY` category has no producer and that is a genuine gap.**
`app/core/audit.py` writes rows for every login, role change and CSRF failure
and publishes nothing. The category exists because the preference grid needs it
to be stable; the event type that fills it should be defined by L39, which owns
the security layer. Defining one at L34 would have been inventing an event,
which section 6 forbids in as many words.

**The recurring lesson this level added:** a table that two subsystems write to
will eventually be written in two vocabularies. `notifications.severity` held
L29's three lowercase words and was about to hold L34's five uppercase ones. The
fix was to import the enum in both places rather than to spell the words out
twice -- the same move L28 made for statuses and L33 for review states. Prefer
an imported vocabulary over a documented one whenever two modules write one
column.

## After L35, 2026-09-05

**Two channels deliver and one seat is filled.** In-app, email and Discord all
route through one service, one preference model and one retry policy. Adding a
fourth destination is now an adapter and a factory line.

**L35 cost less than any level since L18, and the reason is worth recording.**
L34 built the seat: the enum member, the preference row, the delivery CHECK and
a factory that returned NOT_CONFIGURED. So L35 needed no migration, no new
status, no new column and no schema change of any kind -- one adapter, two
routes, one page. Building a seat one level early is cheaper than building a
migration one level late.

**The gap L35 makes most visible is unchanged and still L38.** A user can now
enable Discord for `BROKER` and correctly receive nothing, because nothing in
this platform publishes a `BROKER_DISCONNECTED` event. Six systems are now
waiting on the reconciliation pass that does not run at boot.

**Newly worth doing, cheaply:**

1. **A live send.** Every Discord response code is exercised against a mocked
   transport and no message has ever left this machine. One real webhook and one
   test send would confirm the embed renders as intended -- which is a
   five-minute check nobody can do without a server.
2. **Message editing for conditions that resolve.** A `BROKER_DISCONNECTED`
   followed by a `BROKER_CONNECTED` posts twice. Editing the first needs
   `?wait=true` and a stored message id, and `notification_deliveries` already
   has `provider_message_id` waiting for it. Worth doing only if somebody asks.
3. **Per-user Discord.** Needs encryption at rest and a key-management decision.
   It is a real feature request, not a refactor, and it should be taken as one.

**The recurring lesson this level added:** an optional integration should report
three states, not two. `NOT_CONFIGURED`, `DISABLED` and `CONFIGURED` are
different operational facts, and collapsing the first two into "off" is how an
operator spends an afternoon looking for a broken webhook that was never
switched on. The same distinction is why a routed delivery to a disabled channel
is recorded SKIPPED rather than FAILED.

## After L36, 2026-09-05

**The panel exists and is deliberately small.** Eleven routes, three of which
write, all three about a user's access. Every trading control stayed on the
surface that owns it, which is the whole shape of the level -- and it means the
admin surface has no authorization rule that could drift out of step with the
one that actually guards a bot.

**Two security gaps are now written down rather than merely absent:**

1. **No MFA and no step-up re-authentication.** An administrative session is a
   password and a cookie, and L36 says so in the API, in the panel and in
   `LEVEL_36_ADMIN.md`. This is L39's, and it is the single largest gap on the
   security side.
2. **Admin sessions have the same lifetime as any other.** A shorter privileged
   lifetime needs a second session policy in L04's model, which is a real change
   rather than a setting.

**A retention policy is now overdue in two places.** L34 left notifications
without one and L36 leaves the audit trail without one; both endpoints say so.
The numbers are an operator decision, and the two should be decided together
because they have opposite answers -- an informational notification can age out
in weeks and a security audit row should not.

**L38 remains the only CRITICAL item, and it now has seven dependants.** L36
added the seventh in the most visible way possible: the dashboard reports
`orders_needing_reconciliation` from `orders.status = 'unknown'`, and there is
nothing in the platform that runs a reconciliation pass at boot to resolve one.
An administrator can now watch that number and has no button that would fix it,
which is the honest state.

**The recurring lesson this level added:** the safest implementation of "do not
let a browser edit configuration" is to build no field. L36's configuration tab
renders zero input elements and a test asserts it. A validated field that
accepts a whitelist is a whitelist somebody widens; a page with no field is a
page that cannot be widened by accident.

## After L37, 2026-09-05

**The platform can now answer "is it healthy" from evidence.** Twenty
components across seven layers, each read from the system that owns it, with a
weighted overall verdict and a trading-safety reading that carries its reasons.
Two states of the five are about honesty rather than health: `UNKNOWN` for what
was not observed and `NOT_CONFIGURED` for what nobody set up.

**What monitoring made visible, in order of how uncomfortable it is:**

1. **`market_data.freshness` is UNKNOWN on this machine** and says "no bar has
   ever been stored". That has been true since L08 and this is the first level
   that puts it on a dashboard.
2. **`broker` is NOT_CONFIGURED**, because no adapter has ever been registered.
   Also long true, also newly visible.
3. **`oms` reports orders needing reconciliation** and nothing runs a
   reconciliation pass at boot.

**L38 is next and is the only CRITICAL item, with eight dependants now.** L37
added the eighth by detecting exactly the conditions L38 is supposed to resolve
and deliberately doing nothing about them: worker staleness, order state that
could not be established, positions in `reconciling`, and bot runs with no
heartbeat. The boundary was drawn on purpose -- section 47 -- and it means L38
inherits a detector rather than having to build one.

**Two observability gaps are documented rather than hidden:**

* **No tracing spans.** Correlation ids already thread request to event to log,
  which answers "where did it go"; per-stage timing over the execution path
  needs a tracer, and adding OpenTelemetry is the competing stack section 3
  warns about.
* **No frontend error reporting.** Reusing a provider is what section 37 asks
  for; there is none, and choosing one is a vendor decision.

**The recurring lesson this level added:** a health state needs a word for "not
observed". Three states force every unknown into healthy or unhealthy, and both
are lies -- one reassuring, one alarming. The moment `UNKNOWN` and
`NOT_CONFIGURED` existed, three collectors that would otherwise have had to
guess became straightforward to write.

## After L38, 2026-09-05

**The only CRITICAL item is closed.** The reconciliation pass runs at boot, and
seven levels' worth of dependants -- L19's unknown orders, L21's reconciling
positions, L22's crashed bots, L30's portfolio mismatch, L31's
`reconciliation_required`, L34's broker notification rules, L37's detector --
now have something that reads them before the platform starts work.

**What L38 deliberately did not do, and what it costs:**

1. **No automatic reconnection.** A disconnected adapter latches safe mode and
   waits for a person. That is the right behaviour for a platform with no live
   adapter, and it is the first thing to revisit when one exists: reconnect,
   reconcile, then release -- in that order, and never reconnect-then-resume.
2. **No backup automation.** `BACKUP_RESTORE.md` documents what to back up and
   the restore order, and states an honest RPO of "since the last manual dump".
   Two commands on a schedule and one verification would change that sentence,
   and nothing else in the platform has to change for it.
3. **The safe-mode latch is per process.** One API process today. A second one
   needs a shared latch, which is a row and a lease rather than a flag.

**Three retention policies are now overdue and should be decided together.**
L34 left notifications without one, L36 left the audit trail without one, L37
left `system_events` without one, and L38 added recovery rows to the third.
They have opposite answers -- an informational notification can age out in
weeks and a security audit row should not -- which is exactly why deciding them
one at a time produces three inconsistent ones.

**The highest-value remaining work is not a level.** It is running the platform
against a real MT5 demo terminal for a week: `market_bars` is still empty,
`broker` is still NOT_CONFIGURED, no adapter has ever been registered, and
every reconciliation path is exercised against a fake. The code is ready to be
told it is wrong; nothing has told it yet.

**The recurring lesson this level added:** the expensive direction should be the
one that resumes. Closing safe mode is automatic and costs nothing; opening it
re-runs the whole sequence, requires a person and a reason, and refuses while
the condition holds. A latch that was equally easy to open and close would be
opened by whoever was in a hurry.

## After L40, 2026-09-05

**Level 39 and Level 40 are complete.** The full backend suite completed on
this machine for the first time -- 2338 passed, 0 failed, 1 skipped -- with
Postgres and Redis up and `TEST_DATABASE_URL` set.

### The highest-priority items, in order

1. **CI is red, and was red before L39.** `mypy` reports 16 errors and
   `ruff format --check .` reports 12 files, all in L34-L38 code. The workflow
   gates on both, so **no commit since roughly L34 can have passed CI**. This is
   now the cheapest high-value fix in the repository and it belongs to the
   levels that own the files.

2. **Register a real broker adapter.** Unchanged since L10 and still the single
   largest gap: every reconciliation, disconnect, rejection and unknown-state
   recovery is exercised against a fake this repository wrote. L40 could not
   close it and no test can.

3. **Run the concurrency tests against PostgreSQL.** Two real defects were found
   at the edge of what SQLite with `StaticPool` can express, and the second only
   appeared after the first was fixed. `KNOWN_TEST_LIMITATIONS.md` has the
   command.

4. **A frontend-to-backend contract test.** All 32 frontend files mock
   `services.ts`. A renamed Pydantic field would break the running application
   with no test failing.

5. **A circuit breaker on the event bus client.** L40 bounded a Redis outage to
   0.5s per request, from 2.06s. Removing the remaining 0.5s means not
   attempting a connection known to be refused.

### What L40 changed about the priorities

**`market_bars` is still empty and no model is deployed**, but those are no
longer the most alarming entries. The migration defect was: the schema could
not be applied to the database it is deployed on, and the three tests that
would have said so had been reporting as skips since L02. Before adding
capability, check what the suite is quietly not running.

---

## After L41, 2026-09-05

**Level 41 is PARTIAL.** The deployment is built and verified against a running
stack; two production-readiness items fail and both are backup/restore.

### The blockers, in order

1. **No backup has ever been taken or restored.** This is now the top item in
   the repository. Step 41's rule is that a backup is not operational until a
   restore has been tested, and nothing here has been. Closing it needs a
   decision about where backups go -- a deployment decision, not a code one.

2. **Register a real broker adapter.** Unchanged and still the largest gap. It
   moved *up* relative to everything else because L41 verified that the
   deployment around it works: the platform now starts, migrates, serves,
   reconciles and shuts down correctly, and the only thing it has never done is
   talk to a venue.

3. **Fix CI.** 16 `mypy` errors and 12 unformatted files, all pre-existing in
   L34-L38. The workflow gates on both, so no commit since roughly L34 can have
   passed. Cheapest high-value fix in the repository.

4. **Rehearse a rollback.** Documented at L41, never performed. A rollback
   procedure that has not been run is a document, not a capability.

5. **Leader election for the worker container**, if it ever needs to scale.

### What L41 changed about the priorities

`market_bars` being empty and no model being deployed are unchanged, but they
have both dropped below backup/restore. The reason is that L41 verified the
platform can be deployed and operated safely in paper mode -- so the next
failure that would actually cost something is the one where a database is lost
and there is nothing to restore from.

---

## After L44, 2026-09-05

**Both remaining blockers now need a human decision, not code.** That is a
different situation from every previous level and it changes what "next" means.

1. **Take a backup and restore it.** Needs a storage decision. Until then the
   platform's durability is "the volume has not failed yet".
2. **Connect the MT5 demo adapter.** It exists, it is demo-fenced, and it has
   never run. Needs a Windows host with a terminal and credentials. **No test
   can substitute for this.**

Then, in order of value:

3. **Backtest through the full pipeline.** Today a strategy that backtests well
   has not been shown to survive the risk limits and sizing rules that would
   apply to it. `EXECUTION_CONSISTENCY.md` §1.
4. **A frontend-to-backend contract test.** A renamed Pydantic field breaks the
   running application with no test failing.
5. **MFA**, or a written acceptance that an administrative session is a
   password plus a same-password step-up.
6. Rehearse a rollback. Documented at L41, never performed.

### What changed at L44

Shadow mode exists, so the platform can now run its **real decision chain** and
record what it would have done without executing anything. That is the safest
validation available before a terminal is connected, and it is the thing to run
first when one is.

### What changed at L62

Nothing about what to build next, and that is worth saying plainly. L62 was a
verification level: it added a way to ask whether the safety properties still
hold, and the answer was that twenty-three of twenty-five were already enforced
before it started. The two that are not applicable stay that way until there is
an autonomous control loop to constrain.

**The priority list above is unchanged.** Items 1 and 2 are still the two that
no amount of further verification can substitute for -- a backup that has been
restored, and an adapter that has actually connected. L62 certified the
architecture around a component that has never run; that is worth having, and
it is not the same as the component having run.

The one addition, and it is small:

7. **Approve or replace the safety envelope's eleven defaults**
   (`AUTONOMOUS_ACTION_SAFETY_ENVELOPE.md`). They are configuration decisions,
   not measurements, because no autonomous action has ever been applied here.
   Nothing runs against them today, so this is not urgent -- but the first
   thing that does run against them must not be the first thing that reads
   them.

### What changed at L63

One thing that is worth acting on, and it is not on the list above.

**Change impact analysis is now real and runs today.** `impact.analyse()` takes
the set of files a commit touches and returns whether certification still
stands. Unlike everything else built at L62 and L63, it does not need an
autonomous control loop to be useful -- it needs a diff, which exists on every
commit.

8. **Wire `impact.analyse()` into CI.** It is a function nobody calls. Running
   it on each push would make certification impact a thing the repository
   notices rather than a thing somebody remembers to check, and it is a few
   lines against the existing workflow. This is the highest-value item L63
   produced.

The priority list above is otherwise unchanged, and items 1 and 2 -- a backup
that has been restored, and an adapter that has actually connected -- remain
the two that no amount of governance can substitute for. Three levels have now
certified, verified and governed the architecture around a component that has
never run.

### What changed at L64

Nothing on the list, again, and the pattern is now worth naming.

L62 verified the architecture, L63 governed its certification, L64 bounded what
may be researched about it. All three were worth doing and all three certified,
governed and bounded **a component that has never run.** Items 1 and 2 have not
moved in four levels.

The one item L64 adds is small and follows L63's:

9. **Approve or replace the eight researchable-parameter bounds**
   (`POLICY_CANDIDATE_POLICY.md`). Configuration decisions, not measurements.
   Nothing runs against them, so this is not urgent -- but as with the safety
   envelope, the first thing that runs against them must not be the first thing
   that reads them.

**The honest summary of the last three levels:** the safety machinery around
autonomous control is now thorough, tested and self-checking. The autonomous
control is not built, and the broker adapter it would drive has never
connected. More governance is not what this platform needs next.

### What changed at L65

Nothing on the list. Four levels in a row now.

L62 verified, L63 governed, L64 bounded research, L65 coordinated horizons. The
decision machinery around autonomous portfolio control is now genuinely
thorough. **It has never made a decision**, because the contexts it would reason
over read a portfolio with one open paper position and a broker that has never
connected.

Items 1 and 2 have not moved since L44. The most useful thing that could happen
to this platform is not another safety layer -- it is one connected demo
terminal and one restored backup, after which most of what has been built since
L60 would have data to run on for the first time.

### What changed at L66

Nothing on the list. Five levels now.

L66 is the first one where the missing data became the subject rather than the
footnote. Scenario intelligence needs market history across several instruments
and a record of predictions against outcomes. The database holds **700 H1 bars
for one symbol and one open position**, so most of the level was declined for
the reason the level itself is about: predicting from nothing produces numbers
with methodologies attached and nothing underneath.

Items 1 and 2 are unchanged and are now blocking more than they were:

1. Restore a backup.
2. **Connect the MT5 demo adapter.** Beyond durability, this is what would
   populate `market_bars` across instruments -- and correlation, fragility,
   liquidity stress and scenario calibration all become possible the day it
   runs. Four levels of machinery are waiting on one connection.

### What changed at L67

Nothing on the list. Six levels.

But L67 produced the clearest statement yet of why, and it is a number rather
than an argument: **stress coverage is 0 of 16, and portfolio resilience is
UNKNOWN.** Not degraded, not failing -- unmeasured. The platform cannot stress
what it cannot observe, and it observes one instrument and one position.

Items 1 and 2 are unchanged and now have a fourth level of machinery waiting on
them. `market_bars` across instruments would unlock correlation, fragility,
liquidity stress, scenario calibration and stress coverage in one step.

