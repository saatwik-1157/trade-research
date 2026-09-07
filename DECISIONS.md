# DECISIONS.md

The engineering decision log required by brief §36. One entry per decision that
changes what code exists or how a component is reached. Newest first.

Every entry carries: timestamp, component, old state, new state, decision,
reason, files changed, tests run, test result, risk level.

Risk levels: **none** (documentation only) · **low** (additive, no existing
call site changes) · **medium** (an existing call site or schema changes) ·
**high** (touches an execution, risk or reconciliation path).

---

## 2026-09-04 - The test environment was more permissive than production

**Component:** `app/models/execution.py`, `app/models/ai.py`,
`alembic/versions/`, `backend/Dockerfile`
**Old state:** every gate - pytest, ruff, mypy, CI - ran against SQLite with
Redis absent and no PostgreSQL
**New state:** the suite runs against the services the platform deploys on, and
four defects it could not previously see are fixed
**Decision:** KEEP + MODIFY (four fixes), ADD (a guard test, two migrations, a
.dockerignore)
**Reason:** the defects are worth listing because they share one cause.
`positions.status` was `String(8)` while L21 added `partially_closed` (16) and
`reconciling` (11); SQLite ignores VARCHAR length and PostgreSQL enforces it, so
65 position tests passed and a partial close would have raised
`StringDataRightTruncationError` in production. `training_runs.status` had the
same defect from L25. Migrations 0014 and 0015 passed pre-prefixed constraint
names to `drop_constraint`, so their downgrades were broken and nothing had ever
run one. Three `Money` columns were `Numeric(18,8)` against models declaring
`Numeric(18,4)` - drift the "zero drift" assertion exists to catch, on a test
skipped without a database.

**The environment was more permissive than the target.** That is the general
form, and it is why `test_every_status_value_fits_its_column` walks every table
rather than fixing the two columns that were caught: the fix for a class of
defect is a check that covers the class.
**Files:** `app/models/execution.py`, `app/models/ai.py`,
`alembic/versions/0016_money_precision.py`,
`alembic/versions/0017_status_column_widths.py`, `tests/test_models.py`,
`backend/Dockerfile`, `docker-compose.yml`, `.dockerignore`
**Tests:** the full suite against PostgreSQL and Redis - 1417 passed, 0 failed,
0 skipped - plus `tests/test_migrations.py` (3 passed, previously skipped)
**Result:** PASSED
**Risk level:** **high** - `positions.status` is the position-management path.

---

## 2026-09-04 - Level 33: the seat, not an occupant

The audit found no LLM anywhere in this platform -- no client, no API key, no
prompt template, no provider abstraction. So L33 defined `ReviewProvider` as a
one-method Protocol and made the default a DETERMINISTIC implementation that
writes the summary from sections the assessors already produced.

It is L27's AI_DISABLED decision again, and for the same reason: there, the seat
is `None` rather than a filter that accepts everything, because a permissive stub
and a real component that agrees look identical in the record. Here the trap
would be a provider returning plausible prose. The default returns prose that is
DERIVED, says so, and is labelled `deterministic` in `review_model`.

Choosing a vendor, a cost model and a data-egress policy is a decision somebody
should make deliberately, not one a level makes as a side effect of needing a
sentence written.

## 2026-09-04 - Level 33: the decision/outcome split is structural

`DecisionContext` holds what was known at or before the entry; `OutcomeContext`
holds what happened after. Four of the five assessors take the first and nothing
else, and the type has no `exit_price`, `net_profit`, `r_multiple`, `mae` or
`mfe` on it at all.

Section 63 asks for a test proving the entry assessment gains no future
information when post-entry data changes. **It passes because the function is not
given the data**, not because it is careful. A convention gets forgotten; a
signature does not.

A field added to `DecisionContext` later is a claim that the platform knew that
thing before the entry, and it should be uncomfortable to make.

## 2026-09-04 - Level 33: a provider cannot return a rating

`Narration` carries a summary and three lists of statements. There is nowhere on
it to put a rating, a price, a P&L, an outcome or a model version.

Section 37 lists the hallucinations to guard against -- nonexistent prices,
incorrect P&L, unsupported model versions -- and the cheapest guard is a shape
that has no field for them. The validator then catches what remains: prose that
ASSERTS a figure the facts contradict.

Section 36's rule is the same one from the other side: do not ask a model to
calculate what can be calculated. Every rating in a review is deterministic.

## 2026-09-04 - Level 33: UNKNOWN is never POOR

Section 12 says a trade must not be labelled non-compliant when the strategy data
was not recorded. The same rule is applied to every section: no risk record is
not mismanaged risk, no fill record is not poor execution.

Every UNKNOWN section carries the field it needed, and validation rejects one
that does not. This is L26's BLOCKED-is-not-FAIL and L29's
INSUFFICIENT_DATA-is-not-HEALTHY one level further along, and the sentence is the
same each time: "we could not tell" and "it was bad" are different claims, and
only one of them is about the trade.

## 2026-09-04 - Level 33: confidence is derived, never self-reported

`confidence = (data completeness + section coverage) / 2`.

There is deliberately no term for how sure the narrative sounded. Section 22 says
not to treat a generator's confidence as statistical certainty, and a
self-reported number reflects neither the evidence nor the uncertainty -- it
reflects the model's opinion of its own prose.

The consequence is the useful part: a fluent review of a trade with no strategy,
risk or sizing record scores low, because it IS low.

## 2026-09-04 - Level 33: journal_entries.ai_review is kept and left unused

The column has carried a `# L33` comment since L05. It was not used.

`journal_entries` is a USER's note -- a title, a body, and that column -- and L31
section 39 already separated user notes from system-generated facts. A table
whose rows are sometimes a person's observation and sometimes a machine
assessment cannot answer "who said this?", and the separation would be gone the
moment somebody queried it.

The column stays, unused, rather than being dropped: something reserved it for a
reason, and removing it would lose that reasoning.

## 2026-09-04 - Level 33: a safety test that cried wolf was narrowed

The forbidden-name check first listed `rollback` and `register`, and caught
`db.rollback()` in the idempotency handler and `WorkerRegistry.register`.

Both are unrelated to the model registry, and a rule that flags unrelated code is
a rule people learn to suppress. The name check now lists the registry's specific
verbs -- `promote`, `retire`, `deploy_to_paper`, `stop_deployment` -- and an
import check does the real work: `app.ai.registry_service` is never imported, so
its verbs are unreachable whatever they are called.

Same shape as the grep-versus-docstring failures at L27 and L28. A safety test is
only worth having if its failures mean something.

## 2026-09-04 - Level 32: three metric implementations became one

The audit found win rate, profit factor and maximum drawdown implemented three
times -- in points, in label returns and in account currency. They agreed.

That is the dangerous case rather than the safe one. Three copies stay agreeing
only until somebody changes one, and the copy nobody looked at is the one
somebody quotes. `app/analytics/metrics.py` is the extraction, and
`backtest/runner.py` and `training/metrics.py` now call it.

**No output shape changed.** `compute_metrics` still returns `NOT_AVAILABLE`
rather than `INSUFFICIENT_DATA`, because the backtest API has served that token
since L14 and renaming it would be a breaking change for a cosmetic gain. 98
backtest and training tests pass unchanged, which is the evidence the refactor
moved no number.

This is the fifth extraction of this shape in the repository -- after
`value_per_price_unit` (L18), `AiVerdict` (L22), `AiPolicy` (L27) and the exit
vocabulary (L31). The pattern is settled: when the same fact has two homes, give
it one and re-export.

## 2026-09-04 - Level 32: the unit is a type, not a comment

`Series` carries a `Unit` and `concat` raises `UnitMismatch` rather than pooling
points with currency.

The metals-points error is documented at length in `CLAUDE.md` -- median H1 ATR
is 160 points in silver against 9,386 in palladium, and pooling them produced a
+4,236-point "result" that was arithmetic rather than a finding. A comment saying
so has been in the repository for months and did not stop the same error
appearing as a lot-size spread in the live record. A type does.

Every summary is therefore computed twice, in currency and in R, each labelled,
with a field saying which one pools.

## 2026-09-04 - Level 32: a trade without an R is excluded, never assigned one

Deriving R from the realised loss would make every loser exactly -1R by
construction. The resulting distribution would be a picture of the definition
rather than a measurement, and its mean would move with the win rate alone.

So a trade whose planned risk was never recorded contributes to the currency
series and not to the R series, and the two blocks report different trade counts
where that happens -- visibly, rather than by quietly padding one.

## 2026-09-04 - Level 32: a profit factor with no losers is not infinity

`INSUFFICIENT_DATA`, not a large number and not `inf`.

A run of winners has an undefined profit factor. Reporting a big one invites
reading a small sample as a strong edge, which is exactly how this repository's
93%-win-rate live regime looked decisive: a random entry with a 6:1 adverse
bracket wins about six times in seven by construction, and the losses had not
landed yet.

The same reasoning gives `recovery_factor` `INSUFFICIENT_DATA` when there was no
drawdown: dividing by zero is undefined, not impressive.

## 2026-09-04 - Level 32: two equity curves, and the account one says why

§7 asks that deposits and withdrawals not be read as trading profit, *if these
exist in the project*. They do not: there is no cash-movement table and no column
records a transfer.

The honest consequence is not one curve with a caveat. It is two: the realized
curve, built from trade results and therefore incapable of containing a deposit,
which is the performance figure; and the account curve, built from recorded
equity, which carries a note saying a deposit and a profit look identical in it.

`equity.combine()` exists only to raise `EnvironmentMismatch`. A function that
says no, rather than an absent one, because the absence would be filled by
somebody writing `a.points + b.points`.

## 2026-09-04 - Level 32: no caching, and that is a measured decision

§27 permits caching *where appropriate* and §40 says not to optimise
prematurely without measuring.

Measured: the largest table holds 252 rows and a summary computes in under 10ms.
A cache would add an invalidation path that could serve a stale equity figure as
current -- which §27 itself names as the thing not to allow -- and §59 requires
that a broker reconciliation changing a trade be eventually reflected, which a
precomputed row is one more place to have to notice.

`calculation_ms` is on every summary, so this is revisitable with evidence rather
than re-argued from taste.

## 2026-09-04 - Level 32: a comparison states its own limits in the payload

`/analytics/compare` returns both sample sizes, a `comparable` flag and a
`language` field spelling out the observational reading.

§14 and §24 both ask for this, and the repository's own AI caveat is the reason
it matters: a filter that removes trades will improve total P&L on many of these
datasets *for that reason alone*, because frequency is the one lever with a
proven sign and it points down. A comparison that did not say so would be read as
evidence the filter worked.

## 2026-09-04 - Level 32: analytics contains no write verb

Parsed: no `add`, `commit`, `delete`, `flush`, `drop_all`, `truncate` or `merge`
anywhere in `app/analytics/` or its router.

Stronger than "it does not place orders", and deliberately so. A read-only module
that acquired a write would become an owner of state, and the whole §54/§55
separation -- the portfolio owns account state, the journal owns trade history --
depends on analytics never being one.

## 2026-09-04 - Level 31: one journal row per POSITION EPISODE

A position that filled in two parts and closed in three is ONE trade. Enforced by
a partial unique index on `position_id`, not by a convention.

Section 4 separates order, fill, position and trade, and section 41 warns
specifically against counting execution rows as trades. The index is what makes
the second impossible rather than merely discouraged -- and it is the same
insight L28 recorded: a constraint that is easy to test and hard to violate is
not evidence, so the guarantee belongs in the schema.

NULLs are distinct in that index, and here that is the WANTED behaviour: the 252
imported rows carry no `position_id`, they are not position episodes, and
collapsing them into one would destroy the record.

## 2026-09-04 - Level 31: journal generation is a sweep, not a hook

`record_pending` asks the database which finished positions have no journal row.
It does not hook the four places a position can end -- the position manager, the
reconciler, the paper service and the close route.

A hook missed at one of those produces a trade that silently never exists, and an
empty journal looks exactly like an account that has not traded. A sweep cannot
miss one, and because `record_close` is idempotent it is safe to run on every
pass.

## 2026-09-04 - Level 31: the timeline is derived and stored nowhere

Every event a trade's timeline needs is already recorded, with a timestamp, by
the system that produced it: nine tables between webhook and close.

A `trade_events` table would be a second copy of all of that. It could be written
wrongly, it could fall behind, and when it disagreed with the rows it was copied
from there would be no way to tell which was right -- and the one nobody looked
at would be the one somebody quoted.

Derived, it also satisfies section 27 for free: the same rows produce the same
timeline however many times it is read, and a duplicate event upstream is visible
AS a duplicate rather than being merged into a second history.

## 2026-09-04 - Level 31: realized P&L is summed from the closes, never recomputed

The subtle way to double-count a partial close is to book `(weighted_exit -
weighted_entry) x quantity` **as well as** the per-close figures.

Those two agree only when the entry was never weighted. Whenever a position
filled in parts they differ, and only one of them is what the account actually
received. `positions.realized_pnl` -- the running total L21 booked at each
confirmed close, on the size that closed, at the price the venue filled -- is
authoritative, and nothing here adds to it.

## 2026-09-04 - Level 31: `cancelled` and `rejected` are not trade statuses

The journal's states are open, partially_closed, closed, reconciliation_required
and unknown.

A journal row is written when a POSITION closes, and a cancelled or rejected
order never opened one. `orders.status` already carries both words, and section
25 is explicit that a rejected order must not be confused with a trade. Declaring
only reachable states is L28's lifecycle rule applied one level along.

## 2026-09-04 - Level 31: an absent context block says WHICH KIND of absent

"No AI decision is linked" for a strategy configured AI_DISABLED is the correct
and expected shape, and the block says so -- explicitly, including the sentence
"it is NOT evidence the AI was bypassed".

A blank renders identically whether the AI was never consulted, the link was
never written, or something went around it. The reader's natural inference from a
blank is the alarming one, and naming the case is what stops a correct
configuration reading as a bypass.

## 2026-09-04 - Level 31: data quality is detected and flagged, never corrected

Ten checks, and not one of them writes a price, a quantity or a timestamp. A
negative duration is reported rather than reordered.

Section 32 makes a completed trade a historical fact and section 44 says not to
silently correct one. A journal that quietly repaired an impossible timestamp
would leave a record that looks clean and is wrong, with the repair invisible --
which is the same failure `CLAUDE.md` records about the order log that stored the
requested price as the entry.

`data_quality` is NULL when the checks have not run and `{"checked": true,
"findings": []}` when they ran and found nothing. Those are different facts, and
the column stores the difference.

## 2026-09-04 - Level 31: statistics stop before Sharpe, deliberately

`/v1/trades/statistics` counts, sums and averages, and returns a `not_computed`
block naming Sharpe, Sortino, the expectancy curve and significance as L32's.

The line is not arbitrary. Those are the figures somebody quotes as a track
record, and this repository's own live sample produced a t of 9.33 that was
arithmetic rather than evidence -- a random entry with a 6:1 adverse bracket wins
about six times in seven by construction. `win_rate` is null on an empty set for
the same reason: nought wins from nought trades is not a nought percent win rate.

## 2026-09-04 - Level 31: bare constraint names, both directions

`op.drop_constraint("ck_trades_status", ...)` produced
`ck_trades_ck_trades_status`. Fourth occurrence in this repository and the first
on a DROP rather than a CREATE.

The rule now has no exception: every constraint kind, both directions, bare name,
always. It is worth recording that only the round-trip downgrade against a real
PostgreSQL reaches this statement -- SQLite never executes it, so the test
environment is again more permissive than production.

## 2026-09-04 - Level 30: gross and net are carried everywhere, never one figure

Every `Bucket` in the exposure report has both. Long 50,000 and short 30,000 is
80,000 gross and +20,000 net, and a report that showed one where the other was
meant would understate the position by more than half.

The case that settles it is the flat book: equal long and short is `net = 0` and
`gross > 0`. A single "exposure" number renders a fully hedged pair of positions
as no exposure at all — and which of the two a given line meant would depend on
who wrote it.

## 2026-09-04 - Level 30: a notional without a measured contract size is refused

`notional_of` returns `Notional(None, computable=False, reason=...)` rather than
assuming a contract size, and the refused positions are COUNTED in
`Bucket.uncomputable` rather than dropped.

This is the metals-points error made unrepresentable. `CLAUDE.md` records a
+4,236-point pooled result that was arithmetic rather than a finding, because
median H1 ATR runs 160 points in silver against 9,386 in palladium and pooling
adds numbers that are not the same quantity. A notional built from an assumed
contract size is a number in the wrong unit, and it pools with everything else.

Counting rather than dropping matters as much as refusing: a total that silently
excluded three positions would be wrong in a way nobody could see.

## 2026-09-04 - Level 30: unrealized P&L is all-or-nothing

`unrealized_of` returns `None` when ANY position could not be marked, and names
the symbols that could not be.

A partial total is a number a reader will treat as the whole. There is no visual
difference between "unrealized is +1,200" and "unrealized is +1,200 across the
four positions we could price, out of seven", and only the first fits in a tile.

The same rule sends `PnL.total` to `None` when either half is unknown.

## 2026-09-04 - Level 30: an unstopped position has no risk, not zero risk

`open_risk` reports `None` for a position with no stop loss, with the reason
"NOT zero: an unstopped position is the one whose loss has no floor."

Zero would say the position cannot lose. It is the exact inversion of the fact,
and it would sum into a total that looked SAFER the more unstopped positions the
account held.

## 2026-09-04 - Level 30: the peak only ever rises

`Drawdown.observe` has no branch that lowers `peak_equity`, and the peak is read
from the whole of `portfolio_snapshots` rather than a rolling window.

§22 says not to reset the historical peak accidentally, and the accident is
specific: a peak derived from the last 30 days falls every time the window rolls
past the old high, so the drawdown shrinks without the account recovering. Making
it structurally impossible beats remembering.

## 2026-09-04 - Level 30: the portfolio compares, and L21 repairs

`reconcile_for` reads the broker's positions, compares them to the platform's and
reports the difference. It does not settle, close, adjust or create anything.

§63 forbids this engine from modifying a position, and L21's
`PositionReconciler.sweep` already does the repair behind its own route where an
operator asks for it. Two systems that both repair would race; one that reports
and one that repairs cannot.

`not_checked` is deliberately not `agrees=True`. An unchecked portfolio and a
checked one that matched must not look alike.

## 2026-09-04 - Level 30: what the risk engine reads and this engine does not own is DECLARED

`NOT_SUPPLIED` names nine `PortfolioState` fields and the system that owns each:
market data for `market_open`, the strategy registry for `strategy_enabled`, the
Bot Manager for `bot_state`, the trade journal for the trade-rate fields, sizing
for `margin_required`.

L28's `DECLINED` pattern, and the same reasoning: an absence looks identical
whether it was reasoned about or forgotten. A test asserts every `PortfolioState`
field is either supplied or declared, so adding one to the risk engine forces a
decision — rather than producing a `None` that L17 correctly reads as a veto and
that nobody meant.

## 2026-09-04 - Level 30: a stale portfolio makes trading more conservative

Every value in `to_risk_state()` may be `None`, and L17 already treats a `None` a
limit needs as a veto rather than an assumption.

Worth writing down because the intuitive fear runs the other way — that a broken
portfolio read would let something through. It cannot: the failure mode of this
handoff is refusing trades, not permitting them, and that is the direction a
failure should point.

## 2026-09-04 - Level 30: the timezone seam is crossed explicitly, not avoided

`app.risk.state.day_start` works in AWARE UTC; the portfolio engine works in
NAIVE UTC because that is what the database stores. The service converts in both
directions with a comment saying why.

Reusing L17's boundary was the right call and it was not sufficient. A naive
datetime's `.astimezone(UTC)` is read as LOCAL time, so passing one straight
through would have started the trading day at 18:30 the previous evening on this
UTC+5:30 machine — the identical bug `CLAUDE.md` documents, arrived at from a
different direction. A parsed test now asserts no module in `app/portfolio/`
calls `.replace(hour=...)` at all.

## 2026-09-04 - Level 29: INSUFFICIENT_DATA outranks HEALTHY

The health precedence is OFFLINE > CRITICAL > DEGRADED > WARNING >
INSUFFICIENT_DATA > HEALTHY, and a CHECK constraint enforces the last part:
`health_state <> 'HEALTHY' OR sample_count > 0`.

A run in which checks could not be evaluated is not a healthy run. §43 is
explicit that too little data must never become a false healthy state, and the
ordering is what makes that true by construction rather than by a caller
remembering to check.

It is L26's BLOCKED-is-not-FAIL one level along, and the same sentence applies:
"we could not tell" and "it is fine" are different claims, and only one of them
is safe to act on.

## 2026-09-04 - Level 29: DEGRADED is reserved for the model

A `serious` reading on a feature distribution produces WARNING. The same
severity on calibration, prediction drift, confidence or performance produces
DEGRADED.

§42 asks that a distribution change not be read as a model failure -- a market
regime changed and the feature distribution changed with it is the EXPECTED
case. Making the two produce different states is where that caution becomes
something an operator acts on differently, rather than a sentence in a document
nobody reads at 2am.

## 2026-09-04 - Level 29: concept drift is inferred from one pattern only

§15 forbids claiming concept drift when only input drift was measured. So
`possible_concept_drift` never measures it and fires on exactly one signature:
outcomes or calibration degraded while the input distributions did NOT move.

The reverse case -- inputs moved, performance held -- returns `ok` with the
summary "NOT concept drift", explicitly. That is the reading a person is most
likely to get wrong, and staying silent about it would leave them to get it
wrong.

## 2026-09-04 - Level 29: a severity change is a new alert

The alert fingerprint is `(model version, scope, check, subject, SEVERITY)`.
Including the severity means WARNING -> CRITICAL produces a different
fingerprint and therefore a new alert that the cooldown cannot suppress.

The cooldown exists because a monitor that says the same thing every run is
muted within a week and then detects nothing. But the transition from warning to
critical is exactly what an operator needs to see, and a deduplication scheme
that hid it would be worse than no deduplication at all.

## 2026-09-04 - Level 29: availability excludes AI_DISABLED

The error rate is computed over inferences that were ASKED. A strategy
configured AI_DISABLED was never asked, so counting it would make **turning the
AI off look like perfect uptime** -- and the better the coverage got, the worse
the number would look.

Same reasoning as L24's answer-rate denominator: a refusal that leaves no row is
indistinguishable from a model nobody asked.

## 2026-09-04 - Level 29: an unresolved decision is not a loss

Only a `filled` outcome resolves into a 0/1 for the calibration and performance
checks. A risk veto, an AI rejection and a sizing refusal are all excluded.

None of them says anything about whether the model was right, and scoring a veto
as a wrong prediction would make a conservative risk configuration look like a
broken model -- which is precisely backwards, and would push somebody to loosen
the risk engine to make the monitoring look better.

## 2026-09-04 - Level 28: eight lifecycle states, and four declined in code

The brief lists ten. `app/ai/lifecycle.DECLINED` records why each of the other
four is absent, in the module rather than in a document, because "we thought
about it and decided against" and "we forgot" look identical in a schema.

`CANDIDATE` already exists as `draft` and `ACTIVE` as `promoted`; a second word
for one state is how a reader comes to believe they are two. `FAILED` lives on
`validation_runs.status`. And `VALIDATING` is the sharpest: **nothing could set
it.** L26 deliberately writes no model status, and `validation_runs` already
records that a run is running -- the same fact in two places means the copy
nobody updates is the one somebody reads.

A test walks the transition table and asserts every declared state is reachable.

## 2026-09-04 - Level 28: one edge into `promoted`, and it starts at `paper`

§12 forbids Training -> Validation PASS -> LIVE. That is enforced by the
TRANSITION TABLE rather than by a check in the promotion function: there is no
arc from `registered` to `promoted`, so a newly trained model cannot arrive
there whatever a future code path tries.

A test enumerates the states from which `promoted` is reachable and asserts the
list is exactly `[paper]`. A check inside a function can be forgotten by the
next function; a missing edge cannot.

## 2026-09-04 - Level 28: `validated` does not serve inference

Validation says the EVIDENCE supports the candidate. Registration says the
ARTEFACT loads, its digest matches, and its feature contract still holds. They
are different checks and they fail independently.

So `SERVING_STATUSES` is `{registered, paper, promoted}` and deliberately not
`{validated, ...}`. Serving inference from a version that passed only the first
check is the gap §16 exists to close.

## 2026-09-04 - Level 28: superseding is not retiring

§14's example retires the previous version when a new one is promoted. This
implementation returns it to `registered` instead.

Retiring it would make every rollback a resurrection of a terminal state, and
§22 says retirement should be a deliberate, separate act. The deployment row
records the supersession; the version stays eligible, which is exactly what §20
needs a rollback to be able to rely on.

## 2026-09-04 - Level 28: NULLs are distinct in a unique index

The one-active-per-scope rule (§14) was a partial unique index over
`(model_key, strategy_key, symbol, timeframe, environment)`. It did not work,
and the test that tried to insert a second active deployment got no error.

NULLs are distinct in a unique index, so any number of rows could share the
UNRESTRICTED scope -- the most common one. The guarantee was absent exactly
where it mattered most and present everywhere it was easy to test.

Fixed with a derived NOT NULL `scope_key`, written by `Scope.key()` so no caller
can produce an inconsistent one. The nullable columns stay as they are: they are
what a reader and a query use, and "not restricted" must remain expressible as
NULL rather than as a sentinel somebody could type by accident.

## 2026-09-04 - Level 28: the model cache is keyed by version, never by scope

A scope's answer changes on every promotion and rollback. A version's artifact
does not, because §6 makes a changed model a new version.

So the cache never has to be invalidated on a lifecycle change: the resolution
runs first and asks for a different key. §30's stale-authorisation problem
cannot arise, rather than being prevented by an invalidation somebody has to
remember to call. The digest is re-checked on every hit anyway -- correctness
over caching speed, and hashing a few hundred bytes is cheaper than serving a
trading decision from an artifact that changed under the process.

## 2026-09-04 - Level 28: a version substitution is refused, not served

`RegistryResolver` refuses to start a bot whose strategy names
`trade_probability v2.1` when the registry resolves `v2.3` for that scope.

Serving the other version would be quieter and would look like resilience. It
would also make every `ai_decisions` row wrong: §37 needs a decision to name the
model that made it, and a substituted version breaks that for every record the
run produces.

## 2026-09-04 - Level 28: promotion is an administrator's, registration is not

One new permission, `promote_ai_models`, granted only to ADMIN. Reads,
registration and paper deployment stay at `manage_ai_models` (TRADER).

§24 lists six permissions; this codebase has one granularity for the AI surface
and inventing six would be a vocabulary nothing enforces. The line that matters
is the one between producing a candidate and deciding it is the version a scope
resolves to, and that is one permission.

## 2026-09-04 - Level 27: AI_DISABLED means there is no AI object

A paper bot whose strategy is AI_DISABLED gets `ai=None` on its engine, not a
filter that accepts everything.

The difference is not cosmetic. A filter would run, journal a row and appear in
the counters, so a reader could not tell a disabled deployment from one whose
model agrees with everything. §7 asks that AI_DISABLED behave EXACTLY as the
deterministic strategy, and the cheapest way to guarantee that is for there to
be nothing to behave differently.

The same reasoning makes `BacktestResult.ai` None for a run with no AI service:
the signal vector is not touched, so the run is byte-identical to every backtest
from before the AI layer existed.

## 2026-09-04 - Level 27: AI_ADVISORY records NEUTRAL, never ACCEPT

Advisory mode returns `Decision.neutral`. At the seat it becomes `accept=True`,
because the signal proceeds -- but the JOURNAL says NEUTRAL.

Somebody will later count how often the AI layer agreed. An advisory reading is
not agreement, and the two must be distinguishable in the table a year from now.
Same reasoning `SignalType` keeps HOLD apart from NO_SIGNAL.

## 2026-09-04 - Level 27: the default scoring formula is min(strategy, ai)

Three named formulas, and the pessimistic one is the default: `min`, then
`weighted`, then `product`.

`min` is the only one of the three that cannot let a confident AI rescue a weak
strategy signal. A weighted average with a high AI weight can, and that is the
shape a "the model liked it" trade takes.

The formulas are NAMED rather than supplied by the caller. §41 forbids accepting
arbitrary code and an arbitrary formula is arbitrary code with extra steps. Each
is monotone non-decreasing in both inputs, asserted by a test, which is what
makes a threshold on the result mean anything at all.

## 2026-09-04 - Level 27: a window reaching past the signal is refused, not trimmed

`_bars_are_causal` refuses a `SignalContext` whose last bar closes after the
signal's own bar time.

Trimming would be the repair §16 exists to prevent: it turns a caller's bug into
a silently different evaluation, and the resulting model looks fine. A refusal
leaves `LOOKAHEAD_REFUSED` in the journal and a reason that names both times.

The AI layer also fetches nothing at all -- no module in `app/ai/` reads market
data, a database or a cache of bars. A seat that could fetch a bar could fetch
tomorrow's.

## 2026-09-04 - Level 27: a flat bar is never offered to the AI

In `apply_ai_filter`, a signal of 0 is appended unchanged and the AI layer is
not consulted.

There is therefore no branch anywhere that could turn a flat bar into a trade,
which is §2's rule ("AI must NOT independently decide: place this order") made
structural rather than intended. A test runs an accept-everything model over an
all-zero vector and asserts the output is identical and the layer was asked zero
times.

## 2026-09-04 - Level 27: AI confidence is not allowed risk

§22, and there is no code path to the alternative. `AiVerdict` has four fields
-- accept, confidence, reason, model -- and nothing downstream reads
`confidence` as a size. A 99% probability produces the same shape as a 51% one.

A test enumerates `quantity`, `volume`, `lots`, `risk_percent`, `risk_amount`,
`account`, `order`, `approve` and `leverage` and asserts none appears in an
`AiDecision` payload either. The richer type gained fields; it did not gain
authority.

## 2026-09-04 - Level 27: eligibility is a boundary, not a registry

`app/ai/eligibility.py` holds no state and stores nothing. It reads
`model_versions.status` and `validation_runs` and answers one question.

§12 asks that the integration boundary be built so it can consume L28's registry
when that exists, and that no competing registry be created meanwhile. So the
switch is one function: today `USABLE_VERDICTS` from L26 decides, and when L28
starts writing statuses `USABLE_STATUSES` becomes the gate.

An unvalidated model is refused, and the refusal says why it is not a judgement
about the model: nothing has been established about it. Same distinction L26
draws between FAIL and BLOCKED, one level down.

## 2026-09-04 - Level 26: the verdict is precedence, never a score

A validation report is a list of named verdicts. `verdict_from()` returns the
most severe: `BLOCKED > FAIL > CONDITIONAL > PASS`.

The alternative — a weighted composite, "83/100" — was rejected because any
weighting scheme can be tuned until it hides the check that mattered, and
because a number invites "close enough" in a way that `FAIL on significance`
does not. `test_the_verdict_is_not_a_score` asserts the payload carries no key
ending in `_score`, so a composite cannot arrive later without a test failing.

A composite figure IS offered as supplementary information — the counts by
severity — and it is never the answer.

## 2026-09-04 - Level 26: BLOCKED outranks FAIL

FAIL says the candidate is not good enough. BLOCKED says we could not tell.

Every check can return BLOCKED, and several do routinely: 30 evaluation rows, no
baseline recorded, an artifact that will not load, no trades at the decision
threshold, fewer than three walk-forward windows, no regime with a supportable
sample. A calibration figure from 40 predictions has not measured calibration;
reporting PASS or FAIL from it would be a claim the sample cannot support.

BLOCKED outranks FAIL in the precedence because a report containing an
unevaluable check cannot honestly say the candidate *failed* either. The general
rule this expresses: **a missing check is not a passing one.**

## 2026-09-04 - Level 26: no `validated` status on `model_versions`

Migration 0018 creates one table and alters nothing. The obvious-looking
addition — a `validating` or `rejected` status on the version row — was declined
because `app/validation/` writes no version status at all, so it would be a
state nothing could enter.

L25 declined `validation_passed` on identical reasoning, and L22 and L23 did the
same for bot and dataset states. Declaring a state nothing can reach makes a
vocabulary a wish list.

Whatever states promotion needs are L28's to add, when something can set them.

## 2026-09-04 - Level 26: the economics go through the project's own simulator

`app/validation/economics.py` turns a model's probabilities into the signal array
`tools/rule_backtest.simulate` already takes. It does not implement a second
backtest.

The brief forbids one; the stronger reason is that every measured figure in
`CLAUDE.md` — the -1.50 points per trade for `rsi_reversion`, the 50.5-52.7%
breakeven win rates, the bracket sweeps, the exit searches — came out of that
function. A model evaluated by a different simulator could not be compared with
any of them, and the comparison is the point.

The friction comes with it and is not negotiable: entry at the NEXT bar's open
from a signal read on closed bars, the spread charged on every trade, and the
LOSS booked when one bar's range covers both the stop and the target.

## 2026-09-04 - Level 26: the model against its own shuffled self

The significance gate is a permutation null over the candidate's own
predictions, not a comparison against a baseline.

Beating a baseline is not evidence, and this project has the measurement to say
so: `reports/bracket_sweep.json` records a 36-cell sweep in which the RANDOM
rule scored an in-sample t of 1.76 while the best real candidate reached 0.83.

Shuffling the OUTCOMES rather than the scores keeps the model's own distribution
of confidence intact, so the null has the same trade count, the same class
balance and the same exposure, and differs only in whether the predictions line
up. The seed is fixed: a validation result that changes between runs is not a
validation result. The p-value carries the Phipson-Smyth +1 on both sides,
because a p of exactly 0 claims more than 200 shuffles can support.

Bonferroni is applied over the number of candidates actually tried, and the
uncorrected figure is printed beside it. Hiding either is how a search launders
itself into a finding.

## 2026-09-04 - Level 26: calibration fails as a WARNING

A miscalibrated model can still rank correctly, and ranking is what the AI seat
uses a model for. So a calibration failure constrains how the number may be READ
— "the probability must not be read as a frequency: 0.80 does not mean 80%" —
rather than whether the model is usable.

Failing the candidate for it would reject a usable filter for a property it is
not being asked to have. Same reasoning L24 used when it made `calibrated` false
until calibration has been measured.

## 2026-09-04 - Level 26: the in-sample figure is measured, not borrowed

The overfitting check needs a train-segment metric. L25 records one that looks
like it fits — `best_validation_log_loss` — and it is from the VALIDATION
segment, which is the segment used for early stopping and therefore not
in-sample.

So `_score_segment` runs twice, over train and over test, and the gap is AUC on
one against AUC on the other. Reading the stored number would have compared two
quantities that are not the same thing, and the report would have looked
identical.

## 2026-09-04 - Level 25: no new queue, and no hyperparameter search

**Component:** `app/training/service.py`
**Old state:** no training engine existed
**New state:** background asyncio tasks bounded by a semaphore, exactly as
`BacktestService` and `ReplayService` have run since L14 and L15
**Decision:** ADD, reusing an existing pattern
**Reason:** section 4 says reuse the background job system if one exists, and
one does. A fourth way of running a background job in one codebase is three too
many, and Celery or RQ would add a broker, a worker process and a deployment
surface to run fits that complete in under a second.

The related refusal is more interesting. **No hyperparameter search**, and not
for scope reasons: `reports/bracket_sweep.json` records a 36-cell sweep in which
the RANDOM rule scored an in-sample t of 1.76 against the best real candidate's
0.83, and `reports/rule_search.json` records 41 candidates whose best
out-of-sample result went negative. A search is a machine for producing winners
that do not survive. Building one before L26's correction machinery exists would
be building precisely the trap this project's own numbers describe, and it would
be the most convincing-looking thing in the repository.
**Files:** `app/training/service.py`, `AI_TRAINING_ARCHITECTURE.md`
**Tests:** `tests/test_training.py`
**Result:** PASSED
**Risk level:** **low** - additive, and it constrains what was added.

---

## 2026-09-04 - Level 25: the dataset lock is checked, not just declared

**Component:** `app/training/service.py`
**Old state:** n/a
**New state:** the job stores L23's dataset fingerprint before fitting,
re-derives it afterwards, and FAILS if it moved
**Decision:** ADD
**Reason:** section 6 asks a job to record the exact data it trained on, and a
version string only records what the data was called. L23's fingerprint covers
the dataset configuration AND a digest of the bars, so "the dataset changed
under the running job" is a detectable event rather than a rule somebody is
trusted not to break. A candidate recorded against provenance that is a guess is
worse than no candidate: it looks exactly like a reproducible one.
**Files:** `app/training/service.py`
**Tests:** `tests/test_training.py::test_a_dataset_that_moves_under_a_running_job_fails_it`
**Result:** PASSED
**Risk level:** **medium** - it decides whether a candidate is recorded.

---

## 2026-09-04 - Level 25: a successful run ends at `validation_pending`

**Component:** `app/models/ai.py`, `app/training/service.py`
**Old state:** `training_runs.status` offered `finished`
**New state:** a successful run ends at `validation_pending`; `paused`,
`validation_passed` and `validation_failed` are deliberately absent
**Decision:** KEEP + MODIFY
**Reason:** section 25 draws the distinction and it is the one this level exists
to hold: "candidate model successfully trained" is not "model approved for
trading". A status called `completed` would be read as the second by everyone
who did not read this entry, so there is not one. The three absent values follow
the rule L22 used on bot states and L23 on dataset states: a state nothing can
enter makes the vocabulary a wish list. `paused` has no checkpoint to resume
from, and the two validation verdicts belong to the level that writes them.
The database enforces the other half: a `validation_pending` row must name its
candidate.
**Files:** `app/models/ai.py`, `alembic/versions/0015_training_jobs.py`,
`app/training/service.py`, `frontend/src/components/TrainingJobs.tsx`
**Tests:** `test_the_status_vocabulary_declares_only_reachable_states`,
`test_training_produces_a_draft_and_promotes_nothing`
**Result:** PASSED
**Risk level:** **medium** - a persisted status vocabulary changes.

---

## 2026-09-04 - Level 25: the fit runs on a worker thread

**Component:** `app/training/service.py`
**Old state:** the fit ran inline in the background asyncio task
**New state:** `asyncio.to_thread`
**Decision:** KEEP + MODIFY (a defect fix within the level)
**Reason:** section 4 says training must not run inside an HTTP request. Running
it on the same THREAD as every HTTP request is the same problem wearing a
different hat: `fit_weighted_logistic` is a synchronous loop with no await in
it, so while it ran nothing else in the process could make progress - including
the health check. Found because the test suite became flaky, not because a test
targeted it, which is an argument for running the whole file rather than the one
test that changed.
**Files:** `app/training/service.py`
**Tests:** `tests/test_training.py` (the whole file; the symptom was timeouts)
**Result:** PASSED
**Risk level:** **high** - it is the responsiveness of the whole API process.

---

## 2026-09-04 - Level 25: the cancel flag is owned by the job, not looked up

**Component:** `app/training/service.py`
**Old state:** `should_stop` read `self._running[job_id].cancelled`
**New state:** the job and its worker thread close over a `_Cancellation` object
**Decision:** KEEP + MODIFY (a defect fix within the level)
**Reason:** the task's done-callback pops its entry from `_running`. So a
cancelled task's worker thread looked itself up, found nothing, read "not
cancelled" and ran the fit to completion - burning CPU for as long as the fit
took, after the job had already been recorded as cancelled. The row said one
thing and the machine did another, which is the exact failure mode L22's
heartbeat supervisor exists to catch one level up. A flag the job owns cannot
disappear while the job is using it.
**Files:** `app/training/service.py`
**Tests:** `tests/test_training.py::test_a_cancelled_job_is_never_recorded_as_trained`
**Result:** PASSED
**Risk level:** **medium** - it is the cancellation path.

---

## 2026-09-04 - Level 24: no scikit-learn

**Component:** `app/ai/`
**Old state:** no AI model or ML dependency existed
**New state:** three models in the standard library; no ML framework added
**Decision:** ADD (and a dependency deliberately not added)
**Reason:** four things compound. (1) Section 17 says use what the project
already supports, and the dependency audit found the backend does not even
DECLARE numpy -- it has imported it since L12 and works only because a
developer machine has the toolkit's requirements installed. (2) The three
models needed here are a few dozen lines each: quantile cuts, a logistic
regression, and a median/MAD z-score. (3) A logistic model's coefficients ARE
the feature importance section 23 asks for -- weight x value is the exact
contribution to the logit, not a surrogate, and section 23 forbids claiming a
feature influenced a model when it did not. (4) Determinism is easier to
guarantee without a framework's defaults. When level 25 needs gradient
boosting it should be added with a stated reason and after the packaging gap
is closed; that is a different decision from taking it now because it is
conventional.
**Files:** `app/ai/*.py`, `backend/requirements.txt`
**Tests:** `tests/test_ai.py`
**Result:** PASSED
**Risk level:** **low** - it constrains what was added rather than changing
anything that existed.

---

## 2026-09-04 - Level 24: a refusal cannot carry a value, in the type and in the database

**Component:** `app/ai/contract.py`, `app/models/ai.py`
**Old state:** no prediction type existed
**New state:** `Prediction.__post_init__` raises if a non-OK status carries a
value, and `ck_model_predictions_refusal_carries_no_value` enforces it again
**Decision:** ADD
**Reason:** the difference between "a caller must check the status" and "there
is nothing for a caller to read" is the difference between a convention and a
guarantee. A row that reaches the table by another path is still a row somebody
will read, so the rule is stated twice. Implementing it exposed a real subtlety:
SQLAlchemy's JSON type serialises Python `None` as the JSON literal `null`,
which is not SQL NULL -- so the constraint that exists to catch a refusal
carrying a value FAILED on a refusal. Fixed with a `NullableJSONType` for that
column, putting the semantics in the schema rather than in a call-site
discipline somebody has to remember.
**Files:** `app/ai/contract.py`, `app/models/ai.py`, `app/models/base.py`,
`alembic/versions/0014_model_metadata.py`
**Tests:** `test_a_refusal_cannot_also_carry_a_value`,
`test_a_refusal_is_recorded_as_a_refusal`
**Result:** PASSED
**Risk level:** **medium** - a schema constraint and a shared column type.

---

## 2026-09-04 - Level 24: a prediction's identity covers its input

**Component:** `app/ai/contract.py`, `app/ai/base.py`
**Old state:** `prediction_id()` hashed the model, version, timestamp, symbol
and timeframe
**New state:** it also hashes a digest of the feature values and the status
**Decision:** KEEP + MODIFY (a defect fix within the level)
**Reason:** section 24 requires deterministic inference, and determinism is
about the INPUT. Without the digest, two different feature vectors scored at one
nominal timestamp produced the same id -- and since `model_predictions` is
idempotent on that id, they collapsed into a single row.
`test_the_answer_rate_counts_the_refusals_too` recorded three predictions and
read back one, which is how it was found. Every distribution built over that
table would have been wrong in a way nothing reported. The digest uses `repr`
of each value rather than a rounded form, because 0.1 and 0.1000000000000001
are different inputs and rounding them together would reintroduce the collision
at a smaller scale.
**Files:** `app/ai/contract.py`, `app/ai/base.py`, the three models
**Tests:** `test_two_different_feature_vectors_are_two_predictions`,
`test_the_answer_rate_counts_the_refusals_too`
**Result:** PASSED
**Risk level:** **medium** - it changes an idempotency key.

---

## 2026-09-04 - Level 24: regime thresholds are fitted, not chosen

**Component:** `app/ai/regime.py`
**Old state:** no regime model existed
**New state:** four boundaries, each a quantile of the training segment
**Decision:** ADD
**Reason:** section 6 forbids hardcoded regime labels without a defined
methodology, and the reason bites harder than it looks. A 0.3% ATR is high
volatility in EURUSD and quiet in BTC, so any fixed threshold encodes one
instrument's habits as a universal fact -- which is the same mistake as a raw
price level being a feature, one level up, and this project has already
measured what that costs. Fitting on the training segment only means the regime
model inherits L23's leakage discipline rather than restating it. `fit_cuts`
refuses below 30 readings rather than falling back to constants, because a
model with invented thresholds produces output indistinguishable from a fitted
one's.
**Files:** `app/ai/regime.py`
**Tests:** `test_regime_thresholds_are_fitted_not_hardcoded`,
`test_a_regime_model_refuses_to_fit_on_too_little_rather_than_guessing`
**Result:** PASSED
**Risk level:** **low** - additive, and it constrains a label rather than a
trade.

---

## 2026-09-04 - Level 24: the AI failure policy has two values and no third

**Component:** `app/execution/ai.py`
**Old state:** no policy existed; the seat took whatever filter it was handed
**New state:** `AiPolicy.required` and `AiPolicy.optional`, applied in one place
**Decision:** ADD
**Reason:** section 26 is emphatic that this must never be implicit, and the
third option somebody always wants -- "carry on if it seems safe" -- is exactly
what an explicit policy exists to replace. Under AI_REQUIRED a model that is
unavailable, version-mismatched, stale or input-starved DECLINES: a model that
could not answer is not a model that agreed. Under AI_OPTIONAL the signal
proceeds to the risk engine, which is unchanged and still authoritative -- so
"optional" never means "unguarded". `_no_answer` is the single place the policy
is applied, so it cannot be applied twice or differently in two branches, and a
feature build that RAISES is routed through it too rather than becoming an
accept by omission.
**Files:** `app/execution/ai.py`
**Tests:** `test_ai_required_and_no_model_means_no_trade`,
`test_ai_optional_and_no_model_follows_the_configured_fallback`,
`test_a_feature_build_that_raises_is_no_answer_not_an_accept`
**Result:** PASSED
**Risk level:** **high** - it decides whether a signal proceeds.

---

## 2026-09-04 - Level 24: `app.ai` never imports `app.execution`

**Component:** `app/ai/`, `app/execution/ai.py`
**Old state:** n/a
**New state:** the model-backed filter and the policy live in
`app/execution/ai.py`; `app/ai/` imports nothing from execution
**Decision:** ADD, with the direction chosen deliberately
**Reason:** L22 spent a level finding and breaking an import cycle that had sat
undetected since L20, because the test suite's import order happened never to
hit it. The obvious place for a filter is beside the models; putting it there
would have made `app.ai` import `app.execution`, which imports `pipeline`,
which imports the paper engine -- the same shape as the cycle that was just
removed. So the dependency runs one way: execution knows about ai, ai knows
nothing about execution. A test PARSES every module in the package rather than
grepping it, because the first version of that test failed on a docstring
explaining the rule -- a text search cannot tell an explanation from a
violation.
**Files:** `app/ai/*.py`, `app/execution/ai.py`, `tests/test_ai.py`
**Tests:** `test_the_dependency_runs_one_way_from_execution_to_ai`
**Result:** PASSED
**Risk level:** **low** - module boundaries; no behaviour changes.

---

## 2026-09-04 - Level 24: numpy is declared, and the image still cannot run the toolkit

**Component:** `backend/requirements.txt`, `backend/Dockerfile`
**Old state:** numpy imported by four modules since L12 and declared nowhere in
the backend; `tools/` imported by `app/strategies/indicators.py` and never
copied into the image
**New state:** numpy declared; the `tools/` gap documented in three places and
left for L41
**Decision:** KEEP + MODIFY (partial fix), and REPORT (the rest)
**Reason:** found by this level's dependency audit. The backend image installs
only `backend/requirements.txt` and copies only `app`, `alembic`, `alembic.ini`
and `pyproject.toml`, so in a container the strategy engine, the backtester,
the replay engine and L23's feature engine would all fail on import. Nothing
catches it because the tests run outside Docker and the health check touches
none of it. Declaring numpy is unambiguously correct and costs two lines.
Changing the Docker build context to carry `tools/` is a deployment decision
with consequences for image size, layer caching and what else becomes visible
in the image -- L41's subject, and not a change to make unilaterally in the
middle of a model level. Recorded in `PROJECT_AUDIT.md` section 21.2, against
L41 in `MIGRATION_STATUS.md`, and in `PROJECT_STATE.json` under
`deployment_defect`, so it cannot be lost. It also shaped this level: `app/ai/`
uses only the standard library, so the AI layer does not deepen a gap it did
not create.
**Files:** `backend/requirements.txt`
**Tests:** the full suite, which runs outside Docker and therefore does not
cover the remaining half -- stated rather than implied.
**Result:** PASSED
**Risk level:** **medium** - a declared dependency; the real risk is the half
that is still open.

---

## 2026-09-04 - Level 23: no feature is a raw price level

**Component:** `app/datasets/features.py`
**Old state:** no feature engine existed
**New state:** 22 features, every one dimensionless; a moving average appears
only as a distance from it and volatility only as a fraction of price
**Decision:** ADD
**Reason:** this project has already measured what price-scaled quantities do to
a pooled result. The metals run produced +4,236 out-of-sample points across
symbols whose median H1 ATR spans 59x, and it was an arithmetic error rather
than a finding; `rule_search.py` now raises a data gap above 5x. A model trained
on `sma_20` learns the price of the instrument, and one trained across symbols
learns which instrument it is looking at. Offering the raw ATR would be worse
still - it is the unit every bracket in this repository is quoted in, so it
would look like the most natural feature in the catalogue.
**Files:** `app/datasets/features.py`
**Tests:** `tests/test_datasets.py::test_no_feature_is_a_raw_price_level`
**Result:** PASSED
**Risk level:** **low** - nothing existing changes; it constrains what is added.

---

## 2026-09-04 - Level 23: the scaler takes a split, so the leak has no spelling

**Component:** `app/datasets/scaler.py`
**Old state:** no normalisation existed
**New state:** `fit(rows, split, ...)` reads `split.train` and nothing else;
there is no function that takes a whole dataset
**Decision:** ADD
**Reason:** fitting a scaler on the whole dataset is invisible in every metric.
The mean and standard deviation of the test period are future information, every
training row is then standardised against numbers that could not have been
known, and the model simply scores better than it should. A check that the
scaler "was fitted correctly" is only as good as whoever remembers to run it, so
the argument is made structural instead: the split is a required parameter, and
"fit on everything" has to be spelled as a split covering everything - which is
then visible in `fitted_rows` and caught by `scaler_fitted_on_train_only`.
**Files:** `app/datasets/scaler.py`, `app/datasets/leakage.py`
**Tests:** `test_a_scaler_cannot_be_fitted_on_anything_but_the_training_segment`,
`test_the_leakage_check_catches_a_scaler_fitted_on_the_whole_series`
**Result:** PASSED
**Risk level:** **medium** - it decides what a future model may see.

---

## 2026-09-04 - Level 23: a dataset is a recipe, and its rows are not stored

**Component:** `app/models/datasets.py`, `app/datasets/builder.py`
**Old state:** no dataset concept existed
**New state:** `datasets` holds a manifest and a fingerprint; rows are rebuilt
from `market_bars` on demand
**Decision:** ADD
**Reason:** section 30 requires that the same bars, feature version, label
version and configuration reproduce the same dataset. If that holds, storing the
rows is optional - and storing them creates a second copy of data the platform
already has, which can then drift from the first. That is the argument
`market_bars` itself makes for keeping `provider` in its identity. The
fingerprint therefore covers the configuration AND a digest of the bars: without
the second half it would say "same recipe" and be read as "same dataset". The
constraint `status <> 'READY' OR (fingerprint IS NOT NULL AND row_count > 0)`
is what stops a row claiming to be trainable without the identity that
reproduces it.
**Files:** `app/models/datasets.py`, `app/datasets/builder.py`,
`alembic/versions/0013_datasets.py`
**Tests:** `test_the_same_bars_and_configuration_rebuild_the_same_dataset`,
`test_different_bars_under_the_same_recipe_are_a_different_dataset`
**Result:** PASSED
**Risk level:** **low** - four new tables, nothing existing touched.

---

## 2026-09-04 - Level 23: the leakage check has a negative control

**Component:** `tests/test_datasets.py`
**Old state:** `no_future_influence` passed on the real feature engine
**New state:** a deliberately forward-reading feature is fed to the same check
and must FAIL
**Decision:** ADD
**Reason:** a check that has only ever passed proves that it runs, not that it
discriminates. The whole value of section 56 is that it can tell a causal
pipeline from one that reads forward, and the only way to know it can is to show
it rejecting one. The same reasoning put a negative control on the scaler check.
This is the permutation-null argument from `rule_search.py` applied to a test
rather than to a strategy: measure the thing against a case where the answer is
known.
**Files:** `tests/test_datasets.py`
**Tests:** `test_the_leakage_check_reports_a_feature_that_reads_forward`
**Result:** PASSED
**Risk level:** **none** - test only.

---

## 2026-09-04 - Level 23: a bar containing both barriers is AMBIGUOUS

**Component:** `app/datasets/labels.py`
**Old state:** no labels existed
**New state:** `Outcome.ambiguous` alongside WIN, LOSS and TIMEOUT
**Decision:** ADD
**Reason:** the bracket label is path-dependent, and bar data records that two
prices traded within a bar without recording which came first. Picking the
nearer one, or the one the close is nearer, invents the half of the record that
is missing - and the project has a live example of what that costs. Position
10200315596 was an NZDUSD sell whose M1 bar spanned 282 points and whose bracket
was computed from the top of that band; both exits ended up on the wrong side of
the fill and every branch was a loss. A row labelled by a coin flip is worse
than a row that is dropped, because it is indistinguishable from a real one.
**Files:** `app/datasets/labels.py`
**Tests:** `test_a_bar_containing_both_barriers_is_ambiguous_rather_than_guessed`,
`test_the_first_barrier_reached_wins_not_the_nearer_one`
**Result:** PASSED
**Risk level:** **low** - additive; it constrains a label rather than a trade.

---

## 2026-09-04 - Level 22: a built group inherits the gate of the stub it replaced

**Component:** `app/api/v1/bots.py`
**Old state:** reads on `/v1/bots` asked only for a logged-in user, so a plain
USER could list every bot, its mode, its limits and why it last stopped
**New state:** reads ask for `Permission.manage_bots`, the same permission the
writes ask for and the same one the 501 stub enforced
**Decision:** KEEP + MODIFY (a regression fix)
**Reason:** `/v1/bots` was a pending stub gated on `manage_bots` and answered
403 to a USER. Building the group silently opened it, and nothing said so -
`RESOURCE_MIN_ROLE["bots"]` has said TRADER since L04 and `frontend/src/lib/nav.ts`
mirrors it, so the declared policy and the enforced one had come apart. Adding
a `view_bots` permission to make the wider access legitimate was considered and
rejected: that would be inventing a permission in order to grant access rather
than to describe it, and a bot row is the operational state of an automated
trader, not the read-only view of results a new account is given. Every
targeted run passed and only the full suite caught it, which is the argument
for the standing rule that the full suite runs before a level closes.
**Files:** `app/api/v1/bots.py`, `tests/test_auth.py`, `tests/test_api_v1.py`
**Tests:** `tests/test_auth.py::test_building_a_group_does_not_loosen_the_gate_it_replaced`,
`tests/test_api_v1.py::test_the_whole_bot_group_needs_the_permission`
**Result:** PASSED
**Risk level:** **high** - it is an authorization boundary.

---

## 2026-09-04 - Level 22: a built group drops its unversioned alias

**Component:** `app/api/pending.py`, `tests/test_auth.py`
**Old state:** `/bots` and `/v1/bots` both answered 501 from one pending group
**New state:** `/v1/bots` is real and `/bots` is 404
**Decision:** REMOVE
**Reason:** the unversioned alias existed to carry a single 501 for a whole
group. Once the group is real the alias would be a second path to one
implementation, which is the thing an alias exists not to be - and L06 settled
this when `/orders` was removed for exactly the same reason, followed by
`/brokers` at L10 and `/strategies` at L12. 404 is the honest answer for an
unversioned path the API no longer serves. The two parametrised lists in
`tests/test_auth.py` record which level removed each entry, so the shrinking
list stays readable as history rather than looking like deleted coverage.
**Files:** `tests/test_auth.py`
**Tests:** `tests/test_auth.py::test_a_built_group_drops_its_unversioned_alias`
**Result:** PASSED
**Risk level:** **low** - a path that answered only 501 stops answering.

---

## 2026-09-04 - Level 22: `paused` is a run state, not a shade of `stopping`

**Component:** `app/models/bots.py`, `app/paper/service.py`, `app/bots/state.py`
**Old state:** `bot_runs.status` had eight values and no `paused`, so
`pause_bot` wrote `stopping` while holding `paused` in memory
**New state:** nine values; a paused run is recorded as paused
**Decision:** KEEP + MODIFY
**Reason:** the in-memory status was correct and the durable one was not, which
is the worst arrangement of the two - it looks right for as long as the process
lives and is wrong the moment it matters. A paused bot and a bot shutting down
need opposite treatment on restart: one should stay paused, the other is an
orphan whose process is gone. One value for both makes that decision
unmakeable, and nothing in the system reports the ambiguity because from the
row's point of view there isn't one.
**Files:** `app/models/bots.py`, `app/paper/service.py`, `app/bots/state.py`,
`alembic/versions/0012_bot_lifecycle.py`
**Tests:** `tests/test_bots.py`, `tests/test_paper.py`
**Result:** PASSED
**Risk level:** **medium** - a persisted status value and one existing call
site change; no execution path.

---

## 2026-09-04 - Level 22: bot limits combine by arithmetic, not by validation

**Component:** `app/bots/limits.py`
**Old state:** per-bot limits did not exist; the account's applied to everything
**New state:** `BotLimits.effective(account)` returns the more restrictive
figure in every field
**Decision:** ADD
**Reason:** the requirement is "a bot's limits may never exceed the account's",
and there are two ways to hold it. Validating at write time and rejecting a
loose figure fails silently later: a limit that was legal when saved becomes
illegal the moment the account tightens, and nothing re-checks it. Combining at
read time cannot produce a looser number than either input no matter what is
stored, so the invariant holds by the shape of the function rather than by a
check somebody has to remember to run. `cooldown_seconds` takes the LARGER
value for the same reason the others take the smaller - a longer cooldown is
the more restrictive one, and getting that backwards would let a bot shorten a
cooldown its account imposed.
**Files:** `app/bots/limits.py`, `app/models/bots.py`, `app/api/v1/bots.py`
**Tests:** `tests/test_bots.py`
**Result:** PASSED
**Risk level:** **medium** - it gates trading decisions, though nothing new
reaches a venue through it.

---

## 2026-09-04 - Level 22: a supervisor measures the heartbeat; it does not read the status

**Component:** `app/bots/supervisor.py`
**Old state:** `bot_runs.status` was the only answer to "is this bot running?"
**New state:** the status column is treated as a claim; the heartbeat is the
evidence
**Decision:** ADD
**Reason:** a status column says what the last process to touch it believed,
and a process that dies mid-run touches nothing. Every crash therefore leaves a
row reading `running`, which is the one case where the column is both wrong and
reassuring. The heartbeat is written by something alive, so its absence is
evidence rather than an absence of evidence. Marking a silent run `crashed` and
restarting it are kept as two passes deliberately: doing both in one loop makes
the second decision invisible, and restarting a bot is the decision that needs
to be visible.
**Files:** `app/bots/supervisor.py`, `app/bots/worker.py`, `app/api/v1/bots.py`
**Tests:** `tests/test_bots.py`
**Result:** PASSED
**Risk level:** **high** - it decides whether an automated trader runs.

---

## 2026-09-04 - Level 22: recovery refuses when no safety check is wired

**Component:** `app/bots/supervisor.py`
**Old state:** nothing recovered a crashed bot
**New state:** `recover()` refuses every attempt unless a safety check is
supplied, and reports the missing check as the reason
**Decision:** ADD
**Reason:** the alternative default is "restart unless told otherwise", and it
is the most dangerous line that could be written here - it turns an unconfigured
supervisor into one that restarts bots into an account whose kill switch is on,
whose orders are unresolved, or whose broker is disconnected. Absence of a
check is absence of evidence, not permission. The cost of this default is that
recovery does nothing until somebody wires it, and that cost is paid in a log
line rather than in a position.
**Files:** `app/bots/supervisor.py`
**Tests:** `tests/test_bots.py::test_recovery_is_refused_when_no_safety_check_is_wired`
**Result:** PASSED
**Risk level:** **high** - it is the automated-restart path.

---

## 2026-09-04 - Level 22: the AI seat moved out of the paper engine

**Component:** `app/execution/ai.py`, `app/paper/engine.py`
**Old state:** `AiVerdict` and `AiFilter` lived in `app/paper/engine.py`, which
`app/execution/pipeline.py` imported them from
**New state:** both live in `app/execution/ai.py`; the paper engine re-exports
them
**Decision:** REFACTOR (MOVE)
**Reason:** `paper.engine` imports `app.execution.outcome`, which runs
`app/execution/__init__.py`, which imports `pipeline`, which imported back into
a partially initialized `paper.engine`. Importing `app.paper.service` first
raised `ImportError`; the test suite's import order never did, so a real cycle
sat undetected from L20. Reordering imports would have hidden it rather than
removed it. The seat is not a paper concept - the orchestrator uses it, and a
demo bot would - and a module two pipelines depend on cannot live inside one of
them. Re-exporting keeps every existing caller unchanged.
**Files:** `app/execution/ai.py`, `app/paper/engine.py`,
`app/execution/pipeline.py`
**Tests:** full backend suite
**Result:** PASSED
**Risk level:** **medium** - module boundaries move; no behaviour changes.

---

## 2026-09-04 - Level 22: `recovering` and `disabled` are distinct states, not flags

**Component:** `app/bots/state.py`
**Old state:** a crashed bot being restarted looked identical to one nobody was
touching; a bot barred from running looked identical to one merely stopped
**New state:** `recovering` and `disabled` are their own states
**Decision:** ADD
**Reason:** `recovering` says somebody is acting on this right now, and
`crashed` says nobody is - only the first means an answer is coming, and an
operator who cannot tell them apart either waits on a bot nothing will touch or
intervenes on top of a supervisor already mid-restart. It is the same
distinction `reconciling` draws from `unknown` for a position, adopted
deliberately so the two layers read the same way. `disabled` differs from
`stopped` in who may undo it: anyone may start a stopped bot, and a disabled one
requires an explicit re-enable. `created` was considered and rejected as a run
state - a `bot_runs` row exists because a run was attempted, and inventing one
for "configured but never started" would make the table overstate how many
times a bot has run.
**Files:** `app/bots/state.py`, `app/models/bots.py`,
`alembic/versions/0012_bot_lifecycle.py`, frontend `BotStatus.tsx`
**Tests:** `tests/test_bots.py`
**Result:** PASSED
**Risk level:** **low** - additive; no existing call site changes.

---

## 2026-09-04 - Level 21: what we intend and what the venue holds are two columns

**Component:** `app/models/execution.py`, `app/positions/policies.py`
**Old state:** `stop_loss` meant both "what this platform intends" and "what is
in force at the venue"
**New state:** `stop_loss`/`take_profit` are the intent; `broker_stop_loss`/
`broker_take_profit`/`broker_synced_at` are what the venue last reported
**Decision:** ADD
**Reason:** the two are the same only until they are not, and the case where
they differ is a position running unprotected while the record says otherwise.
That is the most dangerous thing this layer can observe, and one column cannot
express it. `broker_synced_at` of None means NEVER READ rather than absent,
because reporting an unsynced position as a mismatch would cry wolf on every
one of them.
**Files:** `app/models/execution.py`, `app/positions/policies.py`,
`app/positions/manager.py`, `app/positions/reconciler.py`,
`app/api/v1/schemas.py`, `alembic/versions/0011_position_lifecycle.py`,
frontend positions table
**Tests:** `tests/test_positions.py` (57), `tests/test_api_v1.py`
**Result:** PASSED
**Risk level:** **high** - it is the protective-stop path.

---

## 2026-09-04 - Level 21: a close larger than the position is refused, not clamped

**Component:** `app/positions/broker_executor.py`, `app/api/v1/positions.py`,
`positions` CHECK constraint
**Old state:** no partial close existed
**New state:** refused in the executor, in the API, and by the database
**Decision:** ADD
**Reason:** clamping looks helpful and is not. A caller asking to close 30 of a
position it believes is 100 has a stale view, and giving it 30 of 70 silently
leaves it believing something false about the remaining 40. Three enforcement
points because the constraint is the one that survives a caller finding a way
past the code.
**Files:** as above, plus `alembic/versions/0011_position_lifecycle.py`
**Tests:** `tests/test_positions.py`, `tests/test_api_v1.py`
**Result:** PASSED
**Risk level:** **high**.

---

## 2026-09-04 - Level 21: the most protective stop wins, not the first

**Component:** `app/positions/manager.py`, `app/positions/policies.py`
**Old state:** only the trailing policy could move a stop, so the question did
not arise
**New state:** `PolicySet.stop_movers()` returns every policy that proposes a
move, and `_best_stop` takes the most protective proposal
**Decision:** ADD
**Reason:** a trail and a break-even can both propose on the same tick, and
taking whichever ran first would make the outcome depend on list order - which
brief 20 forbids. "Most protective" is deterministic and cannot loosen
anything, because each policy has already refused to propose a loosening of its
own.
**Files:** `app/positions/manager.py`, `app/positions/policies.py`
**Tests:** `tests/test_positions.py`
**Result:** PASSED
**Risk level:** medium.

---

## 2026-09-04 - Level 21: `reconciling` is not `unknown`

**Component:** `app/models/execution.py`, `app/positions/manager.py`,
`app/positions/reconciler.py`
**Old state:** three position states
**New state:** seven, including `reconciling` beside `unknown`
**Decision:** ADD
**Reason:** `unknown` means nobody is looking; `reconciling` means somebody is,
and only the second means an answer is coming. A sweep that reused `unknown`
would be indistinguishable from a position that had been abandoned, and an
operator reading the table could not tell whether to intervene. The manager
refuses both, so the safety behaviour is identical - what differs is what the
record says about why.
**Files:** `app/models/execution.py`, `app/positions/manager.py`,
`app/positions/reconciler.py`, `alembic/versions/0011_position_lifecycle.py`
**Tests:** `tests/test_positions.py`
**Result:** PASSED
**Risk level:** medium.

---

## 2026-09-03 - Level 20: a second pipeline, not a widened one

**Component:** `app/execution/pipeline.py` vs `app/paper/engine.py`
**Old state:** one pipeline, driven by bars, running a strategy that produces
its own signal; externally-arriving signals had no consumer at all
**New state:** a second orchestrator for signals produced elsewhere, sharing
every gate and reimplementing none
**Decision:** ADD (deliberately not MERGE)
**Reason:** the two answer different questions. One PRODUCES a signal from
bars; the other CONSUMES a signal produced elsewhere, and has failure modes the
first cannot have - a malformed payload, an unknown strategy, a late alert, an
unauthenticated source. A class doing both is two jobs wearing one name. What
they share is called rather than copied: neither computes a risk limit, a
quantity or a fill, and a test parses `app/execution` to prove it holds no
broker adapter and defines no sizing function.
**Files:** `app/execution/*`, `app/api/v1/execution.py`, `app/main.py`
**Tests:** `tests/test_execution.py` (48), `tests/test_api_v1.py` (67)
**Result:** PASSED
**Risk level:** **high** - it is an execution path, even though every gate on
it predates the level.

---

## 2026-09-03 - Level 20: one outcome vocabulary, extracted

**Component:** `app/paper/engine.py` -> `app/execution/outcome.py`
**Old state:** `Outcome` and `NO_ORDER` private to the paper engine
**New state:** the same objects in `app/execution/outcome.py`, re-exported
**Decision:** REFACTOR (MOVE)
**Reason:** brief 35 asks for counters across the whole pipeline. A paper bot
reporting `risk_vetoed` and an orchestrator reporting something else cannot be
added together, and the first dashboard built over them would silently
under-count one. The enum is the thing that makes the counters poolable.
Re-exporting means no existing import or test changed, and a test asserts the
two modules expose the same object rather than equal ones.
**Files:** `app/execution/outcome.py`, `app/paper/engine.py`
**Tests:** `tests/test_paper.py` (93, unchanged), `tests/test_execution.py`
**Result:** PASSED
**Risk level:** low - a move with no behaviour change.

---

## 2026-09-03 - Level 20: four outcomes do not consume the signal

**Component:** `app/execution/worker.py`
**Old state:** n/a - no consumer existed
**New state:** `status_for` returns None for `execution_unknown`, `no_venue`,
`spec_incomplete` and `strategy_error`, leaving those signals in `new`
**Decision:** ADD
**Reason:** most refusals must consume the signal - a vetoed one left in `new`
re-runs the same veto every two seconds forever, and a disabled strategy's
backlog would all fire the moment it was re-enabled. But those four are
conditions that CLEAR: a venue that was unclear, an adapter nobody had
registered yet, a spec that had not been synced, a service that was down.
Marking them finished throws a signal away because a dependency was briefly
down. The set is pinned by a test so a value added later has to be decided
about rather than defaulting.
**Files:** `app/execution/worker.py`
**Tests:** `tests/test_execution.py`
**Result:** PASSED
**Risk level:** medium - it decides whether a recorded signal is ever acted on.

---

## 2026-09-03 - Level 20: execution runs in a worker, not the webhook request

**Component:** `app/execution/worker.py`, `app/api/v1/execution.py`
**Old state:** the gateway wrote a row and nothing read it
**New state:** a supervised `app.workers.Worker` drains `signals` in `new`,
claiming each with FOR UPDATE SKIP LOCKED; the API only starts, stops and
reports
**Decision:** ADD
**Reason:** brief 20 says not to execute inside the HTTP request, and the
reason is stronger than latency - an alert executed inside the request is an
alert whose execution is lost if the connection drops after the row was
written. The gateway's job ends at "recorded"; the worker's starts there, and
the table is the handover. The claim being in the same transaction as the read
is the only duplicate guard that survives two processes; `seen`,
`guard_resend` and `intent_id UNIQUE` are the other three.
**Files:** `app/execution/worker.py`, `app/api/v1/execution.py`, `app/main.py`
**Tests:** `tests/test_execution.py`, `tests/test_api_v1.py`
**Result:** PASSED
**Risk level:** medium.

---

## 2026-09-03 - Level 19: `failed` is not a synonym for `rejected`

**Component:** `app/oms/state.py`, `app/models/execution.py`
**Old state:** eight order states; a request that never reached the venue and a
request the venue refused both had to be recorded as `rejected`
**New state:** twelve states, with `failed` (never transmitted), `rejected`
(the venue refused) and `unknown` (we do not know) as three distinct facts
**Decision:** ADD
**Reason:** the three differ in exactly one way that matters - whether a fresh
order for the same intent is safe. `failed` is safe, `rejected` is not, and
`unknown` must be reconciled first. Collapsing them loses the distinction that
decides the retry, which is the most dangerous decision the OMS makes.
`SAFE_TO_RESEND` is the one-element set `{failed}` and a test walks every state
against it.
**Files:** `app/oms/state.py`, `app/models/execution.py`,
`alembic/versions/0010_oms_lifecycle.py`
**Tests:** `tests/test_oms.py` (63), `tests/test_paper.py` (93)
**Result:** PASSED
**Risk level:** **high** - the order state machine is the execution path.

---

## 2026-09-03 - Level 19: one state machine, extracted rather than copied

**Component:** `app/paper/oms.py` -> `app/oms/state.py`
**Old state:** a correct transition table private to the paper package
**New state:** the same table in `app/oms/state.py`, imported and re-exported
by `app/paper/oms.py`
**Decision:** REFACTOR (MOVE)
**Reason:** the broker-bound lifecycle needed the same machine, and a second
copy that disagreed about whether `accepted -> cancelled` is legal is how a
cancel succeeds in paper and corrupts an order in demo. Re-exporting means no
existing import, caller or test changed;
`test_the_paper_oms_and_the_broker_oms_share_one_state_machine` asserts the two
modules expose the same object rather than equal ones.
**Files:** `app/oms/state.py`, `app/paper/oms.py`
**Tests:** `tests/test_paper.py` (93, unchanged), `tests/test_oms.py`
**Result:** PASSED
**Risk level:** medium - a move with no behaviour change, on an execution path.

---

## 2026-09-03 - Level 19: one lifecycle core, two venue bindings

**Component:** `app/oms/service.py`, `app/paper/oms.py`
**Old state:** one paper OMS; no broker-bound lifecycle
**New state:** `state.py` and `fills.py` shared; `OrderManager` binds them to a
`BrokerAdapter`, `app/paper/oms.py` binds them to the in-process provider
**Decision:** ADD, without merging
**Reason:** brief 16 forbids a `DemoOMS` beside a `LiveOMS`, and there is
neither - demo and live are the same manager with a different registered
adapter. Merging the paper binding into the same class would mean making the
paper path async, which ripples through `PaperEngine` and its 93 tests: a
rebuild of working code, which the brief also forbids. What 16 actually forbids
is a second SET OF RULES, and there is one - one state machine, one fill
accounting, one idempotency key, one reconciliation rule.
**Files:** `app/oms/*`, `app/paper/oms.py`
**Tests:** `tests/test_oms.py`, `tests/test_paper.py`
**Result:** PASSED
**Risk level:** medium.

---

## 2026-09-03 - Level 19: `POST /v1/orders` is built and runs the full chain

**Component:** `app/api/v1/orders.py`
**Old state:** 501 naming L19
**New state:** symbol + contract spec -> `app.sizing` -> `app.risk` -> OMS ->
adapter, with `Idempotency-Key` becoming `orders.intent_id`
**Decision:** REPLACE the handler
**Reason:** the components it named now exist, and a 501 would be a false
statement. It is deliberately not a shortcut: a manual order runs the same
gates a bot signal runs, through the same objects, reaching risk the way the
paper service reaches it so the route cannot hold a looser copy of the limits.
The client's quantity is an INPUT to sizing rather than the quantity traded, so
a size below the venue minimum is refused and never raised to it. And the order
manager registry is empty until an operator registers an adapter, so in the
default deployment the route refuses with that reason - which is the honest
description of a platform in paper mode with ten live gates false.
**Files:** `app/api/v1/orders.py`, `app/main.py`, `app/oms/registry.py`
**Tests:** `tests/test_api_v1.py` (62)
**Result:** PASSED
**Risk level:** **high** - it is a client-facing path toward a venue.

---

## 2026-09-03 - Level 19: `order_events.sequence`

**Component:** `app/models/execution.py`
**Old state:** the audit trail was ordered by `occurred_at`
**New state:** a per-order `sequence`, written from the transition index
**Decision:** ADD
**Reason:** found by a test. A submit that completes inside one clock reading
produces four transitions with identical `occurred_at`, and the primary key is
a uuid, so the four are unorderable. An audit trail whose rows cannot be put in
order is not an audit trail, whatever columns it carries.
**Files:** `app/models/execution.py`, `app/oms/repository.py`,
`alembic/versions/0010_oms_lifecycle.py`
**Tests:** `tests/test_oms.py`, `tests/test_api_v1.py`
**Result:** PASSED
**Risk level:** low - additive, and NULL on the rows that predate it.

---

## 2026-09-03 - Level 18: refuse a size below the venue minimum

**Component:** `app/sizing/calculator.py`
**Old state:** a computed volume below `spec.minimum_volume` was set TO the
minimum; when that risked more than the budget the fact was written to a `gap`
string and `result.ok` remained true
**New state:** refused, with the message naming what the minimum lot would
have risked against the budget
**Decision:** REPLACE the behaviour
**Reason:** it silently exceeds the configured risk, which is the single thing
position sizing exists to prevent (brief 9, 10, invariant 36.2). It also
contradicted this project's own `app/symbols/precision.normalize_quantity`,
which already refused the identical case with the identical reasoning - so the
two modules disagreed about the same question, and the unsafe one was the one
in the trading path. The maximum is deliberately NOT symmetric: it binds
downward, can only reduce risk, and is applied with a warning.
**Files:** `app/sizing/calculator.py`
**Tests:** `tests/test_sizing.py` (59), `tests/test_paper.py` (93),
`tests/test_backtest.py` (47)
**Result:** PASSED
**Risk level:** **high** - it is a sizing path, and the change makes some
previously-accepted trades refuse. That is the intent: those trades were
risking more than their budget.

---

## 2026-09-03 - Level 18: validate stop direction, refuse rather than correct

**Component:** `app/sizing/calculator.py`, `app/paper/engine.py`
**Old state:** the engine received only `stop_distance`; direction was never
checked anywhere in the pipeline
**New state:** `side`, `entry_price` and `stop_loss` are accepted; a long stop
above its entry or a short stop below it is refused
**Decision:** ADD
**Reason:** `abs()` makes a target indistinguishable from a stop, so a broken
bracket sized normally and reached the OMS. This repository has already paid
for that shape (position 10200315596, `CLAUDE.md`). Refusing rather than
correcting because both corrections - flip the stop, flip the side - change
what the caller asked for.
**Files:** `app/sizing/calculator.py`, `app/paper/engine.py`
**Tests:** `tests/test_sizing.py`, `tests/test_paper.py`
**Result:** PASSED
**Risk level:** **high** - live pipeline path.

---

## 2026-09-03 - Level 18: three sizing modes, not seven

**Component:** `app/sizing/calculator.py`, `app/api/v1/position_sizing.py`
**Old state:** three modes; the brief names seven
**New state:** three modes plus an alias table resolved at the edges
**Decision:** KEEP the three, ADD aliases
**Reason:** `fixed_lot` IS `fixed_quantity` (the quantity is the lot on every
instrument here, MT5 included); `monetary_risk` IS `fixed_risk`; stop-loss
sizing IS both risk modes; ATR sizing is this arithmetic over an ATR-derived
stop, which `PaperEngine._bracket` and the backtester already compute - a
separate mode would put the bracket calculation in two places; broker
constraints apply to every result rather than being selectable. A second name
for one calculation is a second calculation waiting to diverge.
**Files:** `app/sizing/calculator.py`, `app/api/v1/position_sizing.py`
**Tests:** `tests/test_sizing.py`, `tests/test_api_v1.py`
**Result:** PASSED
**Risk level:** low - additive.

---

## 2026-09-03 - Level 18: risk sizing wired into the backtester, not into replay

**Component:** `app/backtest/`, `app/replay/`
**Old state:** both refused every mode but `fixed_quantity`, naming L18
**New state:** the backtester sizes each trade through `app.sizing.calculate`;
replay still refuses, now naming the reason rather than the level
**Decision:** ADD (backtest), KEEP + document (replay)
**Reason:** one authoritative sizing calculation (brief 18) - a
`BacktestPositionSizer` beside a `LivePositionSizer` is two answers to one
question. Replay's engine carries one quantity on the portfolio and is proved
trade-for-trade identical to `simulate()`; varying the size per position
changes that engine rather than configuring it, and that equivalence proof is
worth more than the feature. The shared request shape is kept - a test asserts
it - and the risk modes are refused there rather than accepted and ignored,
because a flat-lot replay the caller believes was risk-sized is the silent
divergence the shared shape exists to prevent.
**Files:** `app/backtest/config.py`, `app/backtest/runner.py`,
`app/backtest/service.py`, `app/api/v1/backtests.py`, `app/api/v1/replay.py`
**Tests:** `tests/test_backtest.py` (47), `tests/test_replay.py` (61)
**Result:** PASSED
**Risk level:** medium - an existing call site (`run()`) gained a parameter and
`_enrich` changed shape; no execution path is touched.

---

## 2026-09-03 — TradingView autonomy: audit and architecture

**Component:** whole repository (audit); TradingView chain (design)
**Old state:** Levels 00–17 complete plus 21, 26, 29, 32; no TradingView
strategy ingestion beyond alerts and CSV exports; no orchestrator
**New state:** unchanged code; five architecture documents, a decision log, a
changelog and a machine-readable project state added; an A0–A9 autonomy ladder
planned
**Decision:** AUDIT + PLAN only, per brief §43 and §44. No code modified.
**Reason:** the brief forbids implementing the autonomous system in one
operation and specifies audit → architecture → plan → incremental
implementation. Sixteen of the twenty capabilities the brief names already
exist in the repository, so the plan is mostly wiring, and knowing which parts
are already there is what stops it becoming a rebuild.
**Files changed:** `TRADINGVIEW_ARCHITECTURE.md`, `TRADINGVIEW_DISCOVERY.md`,
`STRATEGY_SPECIFICATION.md`, `STRATEGY_COMPILER.md`, `STRATEGY_VALIDATION.md`,
`DECISIONS.md`, `CHANGELOG.md`, `PROJECT_STATE.json` (new);
`MIGRATION_STATUS.md`, `IMPLEMENTATION_PRIORITY.md`, `PROJECT_AUDIT.md`,
`ARCHITECTURE_MIGRATION.md`, `PROJECT_PROGRESS.md` (appended)
**Tests run:** all three suites, before (no after — no code changed)
**Test result:** research 34 passed · backend 900 passed, 3 skipped · frontend
77 passed. Green.
**Risk level:** none

---

## 2026-09-03 — The compiler targets `StrategyDefinition`, not a new interface

**Component:** strategy compilation
**Old state:** L13's declarative definition exists and is executed by
`BuiltStrategy`; nothing produces definitions except the visual builder
**New state:** planned — `StrategyCompiler` emits a `StrategyDefinition`
payload
**Decision:** KEEP `StrategyDefinition` and `BuiltStrategy` unchanged; the
TradingView compiler emits the payload they already accept.
**Reason:** brief §11 asks for Compiler → Internal Strategy Interface → Strategy
Engine, and that interface already exists, is validated, round-trips, refuses
unknown fields and refuses incomparable units. A second internal strategy shape
would be brief §9's forbidden duplicate — a second strategy engine by another
name. The `TradingViewSpec` stays separate because it must record what *cannot*
be run, which a definition has no field for.
**Files changed:** none yet (A4)
**Tests run:** — · **Test result:** — · **Risk level:** low when built
(additive)

---

## 2026-09-03 — Brackets are not compiled into the strategy definition

**Component:** compiler / position management
**Old state:** exits are owned by `app/positions/policies.py` (7 policies,
priority-ordered) and the paper engine's bracket check
**New state:** planned — Pine `strategy.exit(stop=, limit=)` becomes bracket
configuration attached to the strategy version
**Decision:** MODIFY the compiler's output shape rather than add a stop field
to `StrategyDefinition`.
**Reason:** a stop inside the strategy definition would create a second exit
authority racing the position manager. One position, one exit owner. The Pine
stop is preserved exactly — as configuration the existing policy enforces —
and the preservation report records that it moved.
**Files changed:** none yet (A4)
**Tests run:** — · **Test result:** — · **Risk level:** medium when built
(touches the exit path)

---

## 2026-09-03 — A3 (definition resolver) precedes A4 (compiler)

**Component:** strategy resolution in backtest, replay and paper
**Old state:** `app/backtest/service.py:180`, `app/replay/service.py:137` and
`app/paper/service.py:351` all call `registry.create(strategy_key, config)`,
which resolves only classes registered at import. `BuiltStrategy` is
constructed from data and is referenced outside its module only by
`app/api/v1/strategy_builder.py:527`, for a preview.
**New state:** planned — a resolver accepting either a registry key or a stored
definition reference, used at all three call sites
**Decision:** ADD the resolver, and build it **before** the compiler.
**Reason:** a saved definition currently cannot be backtested, replayed or
paper-traded. That already limits the L13 builder, and it would make the
compiler's output unrunnable on the day it existed. Brief §14, §15, §16, §32
and §33 all route through those three call sites. Build the runway before the
aircraft.
**Files changed:** none yet (A3)
**Tests run:** — · **Test result:** — · **Risk level:** medium when built
(three existing call sites)

---

## 2026-09-03 — The Pine subset is declared and small; everything else refuses

**Component:** Pine parser
**Old state:** no Pine parsing anywhere in the repository
**New state:** planned — a declared subset (4 indicators, 5 price fields, 7
comparisons, AND/OR/NOT, single-assignment variables, entry/exit/close calls);
everything else recorded in `unsupported_features` and compilation refused
**Decision:** ADD the parser with an explicitly closed subset. Do **not** widen
the indicator catalogue to make a strategy compile.
**Reason:** the evaluator implements exactly what the subset says, and offering
MACD or Bollinger from `tools/indicators.py` would mean either a second
implementation or a conversion layer over a different data shape — the thing
L13 refused deliberately. Brief §4: flag what cannot be determined, never
invent it. `request.security` is refused specifically because it repaints and
is the standard route to a look-ahead a backtest cannot see.
**Files changed:** none yet (A1)
**Tests run:** — · **Test result:** — · **Risk level:** low when built

---

## 2026-09-03 — A8 (alert → execution) is blocked, and stays blocked

**Component:** signal consumption / OMS
**Old state:** the L09 gateway writes a `Signal` and publishes
`SIGNAL_CREATED`; nothing consumes it. L18 (sizing) and L19 (OMS state machine,
idempotency use, reconcile-before-retry) are PARTIALLY COMPLETE.
**New state:** planned as A8, sequenced after L18 and L19
**Decision:** do not build the alert → execution bridge yet. Report it as
blocked per brief §41.
**Reason:** brief §20 requires the 13-state order machine with idempotency and
no blind retry out of `unknown`. Building the bridge first would mean either a
second OMS (brief §9 forbids it) or an execution path that predates the state
machine guarding it. The project's doctrine is to build the veto before the
path.
**Files changed:** none
**Tests run:** — · **Test result:** — · **Risk level:** high when built

---

## 2026-09-03 — The LLM interpreter proposes; the deterministic validator decides

**Component:** AI strategy interpreter (brief §5)
**Old state:** no LLM in any runtime or build path
**New state:** planned as A5 — an interpreter that proposes mappings for
ambiguous Pine constructs, with provenance recorded in the spec
**Decision:** ADD at build time only. An LLM proposal reaches a running
strategy only by passing the same deterministic parser, compiler and unit
checks as hand-written input.
**Reason:** this is CLAUDE.md's one rule applied to a new surface — Python
computes, the model interprets, and the model never introduces a figure. An
LLM-proposed indicator period is a figure. Brief §5 says the same thing from
the other direction: the AI must not directly execute trades and the
deterministic engine stays responsible for evaluation.
**Files changed:** none yet (A5)
**Tests run:** — · **Test result:** — · **Risk level:** low when built (no
runtime path)

---

## 2026-09-03 — No TradingView → MT5 path exists to refactor

**Component:** execution coupling
**Old state:** assumed by brief §33 to possibly exist
**New state:** verified absent
**Decision:** no refactor; build the chain instead.
**Reason:** verified by import graph rather than by reading docstrings. No
module in `app/paper`, `app/replay`, `app/backtest` or `app/strategies` imports
`app.brokers` or `MetaTrader5`. `MetaTrader5` is imported lazily in
`app/marketdata/providers/mt5.py` and `app/symbols/sync_mt5.py` only, besides
the adapter. `tools/tv_webhook.py` holds no credentials and places nothing, and
a test already asserts the L09 gateway imports no strategy, risk, sizing or OMS
module.
**Files changed:** none
**Tests run:** import-graph greps + the existing package-parsing tests
**Test result:** clean · **Risk level:** none

---

## 2026-09-06 — A close is approved, not vetoed

**Component:** `app/risk/engine.py`, `app/positions/broker_executor.py`,
`app/risk/service.py`
**Old state:** `broker_executor.py` called `manager.adapter.close_position()`
directly — no `Approval`, no OMS, no `intent_id`, no order record — while its
own module docstring stated that the Risk Engine approves and the OMS submits
(L45 C-2).
**New state:** the close goes `RiskEngine.approve_close` → `OrderManager.create`
→ `OrderManager.close`, recorded before and after the venue call. No attribute
named `adapter` is read anywhere in the module.
**Decision:** route the close through the OMS, and give the RiskEngine a
**second, separate entry point** for it rather than reusing `approve`.

**Reason:** routing a close through `approve` naively would have been worse than
the bug. Every limit this engine enforces bounds the risk of *taking* a
position — exposure, position count, daily loss, drawdown, the kill switches.
Applying them to a close refuses to reduce exposure at the moment exposure is
worst: a breached daily loss would make a position impossible to exit, and a
kill switch would trap every open position behind it. That is not a risk
control, and it is very likely why the original code went around the engine
instead of through it.

So `approve_close` evaluates the close, **records** the verdict, and approves it
regardless — re-stamping the breached checks `enforced=False`, which is what
`not_enforced` has always meant, so they reach the order's `risk_snapshot` and
the record of the close names every limit it went over. One check still
refuses: the mode fence, because a close is still an instruction transmitted to
a venue.

What was actually at stake in C-2 was **idempotency, not permission**. No
`intent_id` meant two concurrent close requests were two `close_position` calls
at the venue, and on a hedging account a double close *opens* a position the
other way.

**Consequence worth stating:** `Approval` now has two constructors instead of
one, so the invariant is no longer "one method" but **one class**. A test walks
every file in `app/` and fails if an `Approval` is built outside
`app/risk/engine.py` — anything that can build one has bypassed the engine,
because the OMS accepts any `Approval` it is handed.

**Files changed:** `app/risk/engine.py` (`approve_close`), `app/risk/service.py`
(`engine_for_close`), `app/positions/broker_executor.py`,
`app/api/v1/positions.py`, `tests/test_positions.py`, `tests/test_risk.py`
**Tests run:** `tests/test_positions.py`, `tests/test_risk.py`
**Test result:** 71 and 87 passed · **Risk level:** high — this is the
execution core

---

## 2026-09-06 — The database is the stricter authority on "one intent, one order"

**Component:** `app/execution/store.py` (new), `app/execution/pipeline.py`,
`app/oms/repository.py`, `app/api/v1/orders.py`
**Old state:** the execution pipeline created orders in memory and wrote none,
so `orders.intent_id` UNIQUE guarded nothing on the automated path, startup
reconciliation found `unresolved=0`, safe mode never latched, and an unknown
order was re-sent after any restart (L45 C-1).
**New state:** the pipeline writes the `orders` row before it transmits, and
asks the database — not only in-process memory — before creating an order.
**Decision:** where the OMS's in-memory guard and the schema disagree, **the
schema wins.**

**Reason:** `SAFE_TO_RESEND` permits a fresh order for an intent whose send
provably failed, and `orders.intent_id` is UNIQUE, so that second order has
nowhere to be written. Left alone it would have surfaced as an IntegrityError
raised *between* `create` and `submit` — a failure at the least recoverable
moment there is, for a reason the schema knew all along. Refusing at the guard,
where the message can explain itself, is strictly better than failing at the
insert.

The cost is bounded and worth naming: an intent whose first send failed is not
retried. `execution_rejected` already retires the signal row, so the worker was
never going to reoffer it; a genuine retry needs a fresh alert, which carries a
fresh intent.

**Related decision, same fix:** a durable write that fails **refuses the send**
(new outcome `not_recorded`). The order is discarded from the OMS and the
signal is not consumed, so a database that blinks costs a delay rather than a
trading signal. An order at a venue that the platform has no record of is the
state no guard can reason about, and §18's ordering exists to prevent exactly
it.

**Files changed:** `app/execution/store.py`, `app/execution/pipeline.py`,
`app/execution/outcome.py`, `app/execution/worker.py` (unchanged behaviour),
`app/oms/service.py` (`discard`), `app/oms/repository.py`, `app/main.py`,
`app/api/v1/orders.py`, `tests/test_execution_durability.py` (new)
**Tests run:** `tests/test_execution_durability.py` and the execution/OMS suites
**Test result:** 27 passed, with the pre-fix behaviour pinned as an assertion
· **Risk level:** high — this is the execution core


## 2026-09-06 — The palette is measured, and four values were not verbatim

**Decision:** every colour pair a reader has to separate is **computed** in a
test that reads `globals.css`, and four tokens were lifted to clear their floor:
`--line` 1.24 -> 1.87, `--surface-2` 1.09 -> 1.35, `--baseline` 1.48 -> 3.01,
`--critical` 3.62 -> 4.50.

**Reason:** `--line` is every panel edge and every table row divider in the
app, and at 1.24:1 it was decorative in the sense of having no visible effect.
`--surface-2` is the active nav item, so the page you are on was marked at
1.09:1 against the ones you are not. Neither was reported by anything, because
nothing was looking.

Two further failures were fixed by changing the label rather than the fill:
white on the accent measured 3.64 and white on the red 3.87, and the page
colour on the same two fills measures 5.34 and 5.02. Darkening the accent
instead would have fixed the primary button and broken every link, which sits
at 4.79 with no margin to give. The red one is the LIVE-ARMED badge, which of
every string in this app is the one that must survive a bad monitor.

The stylesheet says which four values stopped being quotations from the
validated palette. A palette that claims to be validated has to say where it
stopped being one.

**The cost, named:** `--line` is held deliberately BELOW WCAG 1.4.11's 3:1, in
a band of 1.7 to 3.0. It is not a control boundary anybody has to find, it is
the hairline between table rows, and at 3:1 a dense table becomes a grid of
cages. The test asserts both ends of that band rather than only the floor.

**Files changed:** `frontend/src/app/globals.css`,
`frontend/src/components/ui/{Button,Input,DataTable,contrast.test.ts}.tsx`,
`frontend/src/components/{Panel,StatTile,Sidebar,ModeBadge,Unavailable,AuthForm}.tsx`,
`INTERFACE.md` (new)
**Tests run:** `contrast.test.ts`, then the whole frontend suite
**Test result:** 18 passed; capability-checked by restoring the four old values,
which fails exactly 6 assertions · **Risk level:** low — presentation only


## 2026-09-06 — Panels separate by edge and shadow, never by fill

**Decision:** panel separation comes from the border and a shadow. The surface
is NOT lifted away from the page.

**Reason:** it cannot be. The panel surface is 1.12:1 against the page, and a
**pure black** page reaches only 1.21:1 against it — so there is no version of
"make the panels stand out by darkening the background" that arrives anywhere,
and the attempt would have greyed out the whole terminal to get 0.09 of a
ratio. The border does the work instead, which is the reason `--line` was worth
moving at all.

A test asserts the 1.21 ceiling directly. It is the only test here that exists
to make a *future* attempt fail fast: the next person to try fixing panel
separation the obvious way finds the reason it does not work as a red test
rather than as a comment they can talk themselves past.

**Files changed:** `frontend/src/components/{Panel,StatTile}.tsx`,
`frontend/src/app/globals.css`
**Tests run:** `contrast.test.ts` · **Test result:** passed · **Risk level:** low


## 2026-09-06 — The front page renders the component that owns the question

**Decision:** the dashboard's four stale panels now render `PositionsTable`,
`OrdersTable`, `BotStatus` and `RecentTrades` instead of fixed "not built yet"
notices, and the account tiles read `/v1/accounts` and `/v1/portfolio/summary`.

**Reason:** the notices had stopped being true. The page said "No order
management system" and "No bot manager" while `/v1/orders` and `/v1/bots` were
serving and both already had working components on their own pages. The front
door was describing a platform several levels behind the one running behind it.

Understating what exists is **not** the safe direction of that error. It is the
same screen-disagrees-with-system failure as overstating it, and it is the one
nobody files a bug about, because a user who is told a feature does not exist
stops looking for it.

The fix is structural rather than a text edit: each panel renders the component
that owns the question, and those components already resolve their own
available / unavailable state from the API. The next notice to stop being true
now corrects itself. Two notices remain, unchanged, because they are still
accurate — there is no live quote subscription and the research reports are not
served read-only.

`DashboardAccount` shows ONE named account rather than a total. A paper balance
and a demo balance are not one number, and a sum whose provenance the reader
cannot see is the figure this repository exists to not print.

**Files changed:** `frontend/src/app/page.tsx`,
`frontend/src/components/{DashboardAccount,RecentTrades}.tsx` (new)
**Tests run:** frontend suite, `tsc --noEmit`, `eslint`, `next build`
**Test result:** see the run below · **Risk level:** low — read-only surfaces


## 2026-09-06 — Below 768px there was no navigation, and one nav list serves both

**Decision:** `MobileNav`, a drawer below `md`, rendering the **same**
`NavList` the desktop rail renders.

**Reason:** the sidebar is `hidden md:flex` and nothing replaced it. On a phone
every route was reachable only by typing its URL — not a rough edge, the
application being unusable, and the sort of thing a desktop-only review never
sees.

The list is extracted rather than copied because two lists over the same `NAV`
would agree right up until the first route was added to one of them, and the
one that drifts is always the one fewer people open. Same reasoning as
`OutcomeVocabulary` at L20 and the one lifecycle core at L19.

The top bar sheds its right-hand cluster as the viewport narrows — clock, then
API version, then session. The mode badge never hides: which mode this is, is
the one thing on that bar nobody may have to guess at.

**Files changed:** `frontend/src/components/{MobileNav,MobileNav.test,Sidebar,TopBar}.tsx`
**Tests run:** `MobileNav.test.tsx`
**Test result:** 6 passed; capability-checked by removing the close-on-navigate
callback and leaking the scroll lock, which fails exactly those 2 assertions
· **Risk level:** low


## 2026-09-06 — The type scale had the hierarchy backwards, and is now named

**Decision:** three named steps — `text-micro` 11px, `text-mini` 12px,
`text-body` 13px — replacing `text-[10px]`, `text-[11px]` and `text-xs` across
299 sites. `text-sm` upward is unchanged.

**Reason:** the sizes were arbitrary, but that was the smaller half of it.
`text-[11px]` was used 192 times and **most of those were prose** — the notes,
the authority statements, the reason a panel is unavailable. The sentences a
reader most has to read carefully were set smaller than the table data beside
them. Contrast work does not fix a hierarchy that is upside down.

**The sweep was applied by role, not by size.** An uppercase tracked run is a
label whatever pixel value it happened to be written at, so it went to
`text-mini` whether it started at 10px or 11px; everything else that was 10px
is a badge or a tag; the rest is prose. The decision was made per class list,
which is why 10px did not map to a single destination.

Naming them is the durable part. Three bracket values with no names is how five
jobs end up sharing three sizes in the first place.

**Files changed:** `frontend/src/app/globals.css` plus 47 component and page
files
**Tests run:** full frontend suite, `tsc --noEmit`, `eslint`, `next build`
**Risk level:** low — presentation only, and no layout is size-dependent


## 2026-09-06 — The palette was not the palette, so the guard is structural too

**Decision:** every stock Tailwind colour is mapped onto a design token, and a
test now walks the source and fails on any that come back.

**Reason:** five components had reached past the design system for Tailwind's
stock scales — 39 uses, plus `text-black` and a `bg-black/60` backdrop. Two of
them were **the exact failures the contrast test exists to catch**, and it
could not have caught either: `text-neutral-500` measured 3.67:1 against the
panel, under the 4.5 body floor, and `border-neutral-800` measured 1.15:1,
invisible — the same defect `--line` had, in a component that had opted out of
`--line`.

That is the finding worth keeping. **A colour that never appears in
`globals.css` is not a colour a test of `globals.css` can see.** Measuring the
tokens is not enough on its own if a component can simply decline to use them,
so the numeric guard now has a structural one beside it that names the file and
line.

The other 37 were consistency, not contrast, and saying so matters:
`text-black` measures 5.43:1 and 11.45:1 on its two fills and passes
comfortably. It moved because every other dark-on-fill label in this app uses
`--plane`, and pure black beside a near-black is a difference you see without
being able to name. Not every fix in this pass was a failure being corrected.

`transparent`, `current` and `inherit` are exempt from the guard — keywords
rather than colour choices, and `border-transparent` is how the nav reserves
space for its active marker.

**Files changed:** `frontend/src/components/{AnalyticsDashboard,PortfolioDashboard,TradeJournal,TradeReview,NotificationBell}.tsx`,
`frontend/src/app/strategy-builder/page.tsx`,
`frontend/src/components/ui/{ConfirmDialog,contrast.test.ts}`
**Tests run:** `contrast.test.ts`
**Test result:** 19 passed; capability-checked by reintroducing one
`text-neutral-500`, which fails the guard and names the file and line
· **Risk level:** low


## 2026-09-06 - L62: the invariant registry names evidence, and a test resolves it

**Decision:** each of the twenty-five safety invariants records the module that
enforces it and the test that proves it, and a test fails if either does not
exist.

**Reason:** the registry was written from memory and greps, and the resolver
caught **thirteen** bad pointers on its first run -- eleven naming real tests in
the wrong file, two naming nothing at all. Without it the document would have
been a list of confident sentences that happened to be wrong, which is worse
than no document because it would have been quoted.

**The related decision, and the harder one:** two invariants are marked
NOT_APPLICABLE rather than ENFORCED. The components they constrain -- an
autonomous decision engine, a stored policy version -- do not exist here, so
nothing can violate them and every test of them would pass. A test that cannot
fail is not evidence, and a green row for a vacuous invariant spends
credibility the platform has not earned. It is the reason the certification
result is CONDITIONALLY_CERTIFIED rather than CERTIFIED, and that is the
correct answer rather than a shortfall.

**Files changed:** `app/safety/invariants.py`, `app/safety/certification.py`,
`tests/test_safety_invariants.py`, `POLICY_INVARIANTS.md`,
`SAFETY_CERTIFICATION.md`
**Tests run:** `tests/test_safety_invariants.py` · **Test result:** 60 passed
· **Risk level:** low -- the package enforces nothing


## 2026-09-06 - L62: section 34 is verified as a property of the whole codebase

**Decision:** rather than testing the six forbidden paths the brief names, one
test walks the syntax tree of every module under `app/` and asserts that only
the OMS calls a venue write method.

**Reason:** a list of six named paths is a blocklist, and it protects against
exactly the six somebody thought of. The property -- *nothing outside the OMS
reaches a venue to write* -- covers all six and every path added later under a
name nobody has thought of yet. It is the same reasoning L61 used for
classifying actions by effect rather than by name, applied to architecture.

**What it found:** exactly two modules in the backend call `place_order`,
`close_position`, `cancel_order` or `modify_order`. `app/oms/service.py`, which
is the single broker boundary since L19, and `app/brokers/mt5.py` for one
`self.modify_order` attaching a bracket to a fill it just received. The MT5
allowance is pinned by a second test asserting the call is on `self` and is the
only one in the file, so the allow-list entry cannot silently license a future
write.

**Files changed:** `tests/test_safety_invariants.py`
**Test result:** passed; capability-checked by planting
`await adapter.place_order(request)` in `app/execution/pipeline.py`, which fails
it with the file and line · **Risk level:** low


## 2026-09-06 - L62: absent confidence was more permissive than zero confidence

**Decision:** an action that states no confidence has not cleared the
confidence bar. Leaving a field out is not a way to skip a check.

**Reason:** `within_envelope` checks only what it is given, so a proposal
stating `confidence=0.0` was refused for being under the bar while one stating
`confidence=None` passed unchecked. An action with no confidence behind it was
treated as MORE trustworthy than one that honestly reported having none.

This is the L53 defect in different clothes. `PortfolioState.open_symbols`
defaulted to an empty frozenset, so "nobody told me what is open" was
indistinguishable from "nothing is open", and the one aggregate risk control
enabled by default could not fire. Absent is not zero, and absent is never
permission.

**How it was found is the part worth keeping.** L62's TEST J was written as a
PROPERTY over the whole confidence range -- absent is never more permissive
than present -- rather than as a single scenario. It failed on first run at
0.0. A single-value test would have picked a number and passed. This is the
argument for property framing over example framing, in a case where it paid
immediately.

The cost is stated: every auto-applied action must now state a confidence. A
deterministic action states 1.0 and says so.

**Files changed:** `app/safety/verification.py`,
`tests/test_policy_verification.py`
**Test result:** 38 passed, with the pre-fix behaviour pinned as
`test_stating_no_confidence_is_not_better_than_stating_none`
· **Risk level:** medium -- it changes what is auto-appliable


## 2026-09-06 - L62: the verifier delegates, and a test says so

**Decision:** `PolicyVerificationEngine` calls `control.permitted` for action
classification and `decision.decide` for the layer hierarchy, and a syntax-tree
test asserts those calls exist and that no `def classify(`, `def decide(` or
`class RiskEngine` has grown inside it.

**Reason:** L62 says not to build a second policy engine. That rule is never
broken by a file called `policy_engine_2.py` -- it is broken by a wrapper that
starts re-deriving a classification the existing one already makes, because
calling out felt awkward at the time. The day the two disagree, the trade goes
to whichever one the code consulted last, and the disagreement is silent.

The same reasoning produced the package import guard: `app/safety/` may import
`app.portfolio.control` and `app.portfolio.decision` and nothing else, so a
verifier cannot begin consulting -- and then replacing -- the authority it is
supposed to be checking. That guard fired during development on a
`from app.portfolio import control, decision`, and the fix was to make the
import exact rather than to widen the allow-list.

**Files changed:** `app/safety/verification.py`,
`tests/test_policy_verification.py`, `tests/test_safety_invariants.py`
**Test result:** passed · **Risk level:** low


## 2026-09-06 - L63: the governance machinery had an approval path out of itself

**Decision:** `autonomy_level`, `certification_state`, `certification_expiry`,
`audit_logging`, `monitoring` and the rest of `GOVERNANCE_TARGETS` are hard
limits, so an action aimed at one is FORBIDDEN outright.

**Reason:** before this they classified as REQUIRES_APPROVAL. Section 15 says
never allow escalation above the currently certified level and section 23 says
the system may not disable its own certification, audit log or monitoring.
**REQUIRES_APPROVAL satisfies neither, because it is a door rather than a
wall** -- a path by which the system could ask for more authority than it has
been certified for, and be granted it.

The fix needed no new mechanism, which is the part worth keeping. L62 solved
exactly this shape for the safety envelope by putting every envelope field
inside the set L61 already refuses outright; the certification machinery was
missing from that set only because it did not exist when the set was written.
Adding a new rule would have created a second thing to remember.

**Files changed:** `app/safety/autonomy.py` (new),
`app/safety/verification.py`, `app/safety/invariants.py` (INV-27)
**Tests run:** `tests/test_autonomy_governance.py`
**Test result:** 48 passed; capability-checked by dropping `autonomy_level`
from the protected set · **Risk level:** high -- it is the self-modification
boundary


## 2026-09-06 - L63: a guard parameterised over the thing it guards proves nothing

**Decision:** `MUST_BE_PROTECTED` is written out by hand and does not read
`GOVERNANCE_TARGETS`.

**Reason:** the first version of the guard was `@parametrize` over
`sorted(GOVERNANCE_TARGETS)`. Deleting `autonomy_level` from the set also
deleted the test case that would have caught it, so the suite stayed **green
under exactly the regression it existed to prevent.** The capability check is
the only reason this was found; the test passed on the fixed code and passed on
the broken code too.

**This is the fourth time this repository has hit this shape** -- C-4 compared
an object to itself, the L57 lifecycle guard grepped for a word its own
docstring contained, L62 avoided it in three modules by walking the AST, and
this one read its own parameter source. The rule that keeps emerging: **a test
must not derive its expectations from the thing it is testing**, and the only
reliable way to know is to break the code and watch the test fail.

**Files changed:** `tests/test_autonomy_governance.py`
**Test result:** 48 passed, and dropping `autonomy_level` now fails
`test_the_targets_that_must_never_be_droppable_are_protected[autonomy_level]`
· **Risk level:** low, but the lesson is not


## 2026-09-06 - L63: certification expires, and the evidence expires sooner

**Decision:** a 30-day TTL on the certificate and a **1-day** TTL on the
evidence behind it, producing two different states -- CERTIFICATION_EXPIRED and
DEGRADED.

**Reason:** L62's `certify()` is a pure function of a static registry, so it
returned the same answer forever. A certification with no expiry is a claim
about a moment presented as a claim about now.

The two clocks are deliberately different lengths and deliberately produce
different states, because they are different failures. The certificate being a
month old is one thing; the facts underneath it being a month old is worse, and
collapsing them into one timer would have hidden the worse case behind the
milder name. Missing evidence is DEGRADED rather than HEALTHY on the same
reasoning that makes UNKNOWN block autonomy as hard as CRITICAL.

Both numbers are configuration decisions, not measurements -- nothing has ever
been recertified here -- and are recorded as requiring approval.

**Files changed:** `app/safety/lifecycle.py` (new), `app/safety/autonomy.py`
**Tests run:** `tests/test_autonomy_governance.py` · **Test result:** passed
· **Risk level:** medium


## 2026-09-06 - L63: no state reaches CERTIFIED except through REVALIDATING

**Decision:** the transition table has no edge from SUSPENDED, DEGRADED or
CERTIFICATION_REVOKED to CERTIFIED. The only way back is REVALIDATING, and
restoration returns one autonomy level at a time, capped by the certification
ceiling.

**Reason:** section 16 says do not restore autonomy merely because the original
error disappeared, and the way that rule gets broken is not a bad decision --
it is a transition table where the edge simply exists, and somebody takes it
during an incident because the condition has cleared and the dashboard is red.
**A condition clearing is not evidence that the thing it broke now works.**

Asserted over the whole table rather than at the three states that happened to
be tested, so a state added later cannot quietly acquire the edge.

The asymmetry is the design: falling is one step and automatic, climbing back
is staged and needs evidence at each stage. A system that recovered as fast as
it degraded would be one whose degradation meant nothing.

**Files changed:** `app/safety/lifecycle.py`, `app/safety/autonomy.py`
**Test result:** `test_no_state_reaches_certified_except_through_revalidating`
· **Risk level:** medium


## 2026-09-06 - L63: change impact is read from the diff, not inferred from a metric

**Decision:** `impact.analyse()` takes a list of changed files and resolves
them through L62's invariant registry to invariants, gates and a verdict.
Unrecognised paths are FULL_REVALIDATION, never NONE.

**Reason:** it is the one part of L63 with something real to work on. Every
other section describes watching a control loop that is not running; a changed
file is a fact the repository produces on every commit.

It needed no new bookkeeping because **the registry already was the mapping** --
each invariant names the module that enforces it and each gate names its
invariants, which is exactly a file-to-verdict function read backwards. That is
the second time L62's registry has paid for itself in a way it was not designed
for.

**Two rules make it an analyser rather than a rubber stamp.** An unrecognised
path is high impact, because a path nobody classified is a path nobody thought
about -- and that is the branch that will be most tempting to soften the first
time it flags something dull. And `app/safety/` is itself a critical path, so
changing the analyser suspends autonomy: a governance tool exempt from its own
rule is the first place a hole appears.

The cost is stated: `CRITICAL_PATHS` is hand-maintained, so a new module as
important as the OMS is not covered until somebody adds it. The
unrecognised-path rule limits that to FULL_REVALIDATION rather than
SUSPEND_AUTONOMY.

**Files changed:** `app/safety/impact.py` (new)
**Test result:** passed, including against this session's own changes, which it
correctly returns SUSPEND_AUTONOMY for · **Risk level:** low


## 2026-09-06 - L64: research uses an allow-list, and the polarity is the point

**Decision:** a parameter is Category A -- immutable, not even researchable --
unless it appears in `RESEARCHABLE` with a category and explicit bounds.
`categorise()` returns HARD_SAFETY for anything unrecognised.

**Reason:** L62 and L63 both used blocklists, and both were right to. An
action's targets are known when the action is written, so naming the forbidden
ones is complete.

Research is not like that. A blocklist there means *everything is researchable
unless forbidden*, so a safety parameter added next month is researchable by
default -- not by anybody's decision, but by the absence of one. The failure
mode of the allow-list is that somebody forgets to classify a legitimate
research target and it stays immutable. The failure mode of the blocklist is
that somebody forgets to forbid a hard limit and a research engine starts
proposing changes to it. Only one of those is survivable.

**Category A has no approval path**, which is stronger than requiring sign-off:
not reviewable, not proposable, not optimizable. The same shape L61 used for
FORBIDDEN actions, for the same reason -- a governance loop that could be
argued into moving a hard limit has no hard limits.

**Files changed:** `app/safety/policy_research.py` (new),
`app/safety/invariants.py` (INV-28), `app/safety/certification.py`
**Test result:** 33 passed; capability-checked by flipping the default to
GOVERNANCE, which fails twelve tests · **Risk level:** high -- it is the
boundary of what may be proposed at all


## 2026-09-06 - L64: there is no sandbox because a candidate is not code

**Decision:** `Candidate.changes` is `dict[str, Decimal]`. No expression, no
callable, no source string, no interpreter.

**Reason:** section 9 asks for a sandbox preventing broker access, MT5 access,
filesystem abuse, secret access, arbitrary network access, production database
mutation, live trading, policy deployment and monitoring disablement.

Every item on that list is **unreachable from a dictionary of decimals** rather
than blocked. Building a sandbox would be posting a guard at a door that does
not exist, and its presence would imply the door does -- which is worse than
useless, because a future author would reasonably conclude that executable
candidates are anticipated.

The cheapest safety property in the module is the type annotation, and it
should be defended rather than relaxed. If a later level needs expressions, the
whole sandbox requirement returns and `POLICY_SANDBOX_SECURITY.md` stops
applying; that document says so.

**Files changed:** `app/safety/policy_research.py`
**Test result:** `test_a_candidate_cannot_reach_a_venue_because_it_is_not_code`
parses the module and fails on eval, exec, compile, __import__, open, system,
popen, run, loads or any adapter write · **Risk level:** low


## 2026-09-06 - L64: safety dominates lexicographically, not by weight

**Decision:** `Score.ordering()` returns
`(safety, robustness, stability, -complexity)` and selection is tuple
comparison.

**Reason:** section 18 requires that a policy with better performance and worse
safety must lose. **A weighted sum cannot deliver that.** A weight is a price:
however large, there is a performance gain that outbids it, and the sum tips
without anybody deciding it should. Lexicographic comparison has no price --
safety is compared first and alone, and the rest is only consulted on a tie.

This is the third time this pattern has been the right answer here: L60's
`decide()` takes the most severe verdict so a lower layer cannot relax a higher
one, L62's verification takes the most restrictive decision, and now this.
Where a safety property must be absolute, make the alternative
**unrepresentable** rather than expensive.

A tie goes to the incumbent, because a running policy has evidence from running
that a candidate does not.

**Files changed:** `app/safety/policy_research.py`
**Test result:** `test_safety_is_compared_first_and_alone` puts safety 1 with
robustness 100 against safety 2 with robustness 0, and the safer one wins
· **Risk level:** low


## 2026-09-06 - L64: the multiple-testing gate was reused, not rebuilt

**Decision:** sections 25 and 26 are answered by `app/research/selection.py`,
the L58/L59 gate, unchanged.

**Reason:** it is the one piece of research machinery this project has
validated against its own recorded data. CLAUDE.md reports 41 candidates
clearing about 1 by chance, 128 combinations clearing 3.2, 200 clearing 4.9 and
28 shape candidates clearing 0.7, and the gate reproduces every figure.

A second implementation inside the policy layer would have been the worst
outcome available at this level: two multiple-testing corrections that agree
until they do not, in a system whose entire purpose is deciding whether a
result is real. The one that disagreed silently would be the one nobody read.

Its three verdicts are worth restating because none of them is approval:
NOTHING_ESTABLISHED (the verdict every search in CLAUDE.md received),
ABOVE_CHANCE_NOT_SIGNIFICANT, and SURVIVES_CORRECTION -- which is not an
approval either, but the point at which out-of-sample evidence becomes worth
gathering.

**Files changed:** none -- that is the decision
**Test result:** `test_a_candidate_strong_in_sample_and_weak_out_of_sample_loses`
· **Risk level:** low


## 2026-09-06 - L65: horizon and layer are orthogonal, so they compose

**Decision:** `Horizon` (when) maps onto `Layer` (who says so) and the
resolution is delegated entirely to L60's `decide()`. There is no second
ordering.

**Reason:** the audit's first question was whether `Layer` already covers this.
It does not -- a long-horizon view from RISK and one from OPTIMIZATION carry
different authority despite sharing a time scale, so the two questions are
genuinely independent.

But independent questions must not become independent ANSWERS. Two orderings
resolving the same conflict is the shape this repository keeps rejecting: they
agree until they do not, and the disagreement is silent. Mapping horizon onto
layer and letting `decide()` resolve means there is exactly one place a
conflict is settled, and it is the place that already could not express a lower
layer relaxing a higher one.

`Horizon` is an `IntEnum` where LOWER wins, deliberately inverted from `Layer`
where higher wins. A reader who has to check which way round it is will check;
sharing a direction would invite reading one as the other.

**Files changed:** `app/portfolio/horizon.py` (new),
`app/safety/invariants.py` (INV-29), `app/safety/certification.py`
**Test result:** 30 passed; capability-checked by breaking the layer mapping,
the emergency hysteresis exemption and the deferral rule, which fails seven
tests · **Risk level:** medium


## 2026-09-06 - L65: a test of mine asserted the wrong property

**Decision:** the horizon-to-layer mapping is NOT monotonic, and the assertion
that it should be was replaced rather than the mapping.

**Reason:** the obvious property is "longer horizon, weaker layer". It fails:
MEDIUM_TERM maps to PORTFOLIO(5) while SHORT_TERM maps to STRATEGY(4).

Checking rather than flipping showed the mapping is right and the test was
wrong. Medium-term concerns -- allocation, concentration, correlation --
genuinely ARE portfolio concerns, and `Layer` names the KIND of concern rather
than its urgency. And it does not affect outcomes anyway: **`decide()` takes the
most restrictive VERDICT first and uses layer only to break a tie**, so a
medium-term RESTRICT beats a short-term ALLOW on severity, which is exactly
what section 44's TEST 4 asks for. The layer ordering only chooses which reason
gets NAMED when two horizons say the same thing.

The replacement asserts the property that actually matters: the two safety
horizons own the two safety layers, and no other horizon may borrow one. It is
narrower, it is true, and it still fails when the mapping is broken.

Worth recording because the tempting move was to flip PORTFOLIO and STRATEGY to
make a red test green -- which would have made the layer assignment
semantically wrong in order to satisfy an assertion that was never the
requirement.

**Files changed:** `tests/test_multi_horizon.py`
**Test result:** 30 passed · **Risk level:** low, but the reasoning is the point


## 2026-09-06 - L65: a losing recommendation is kept, and hysteresis never touches safety

**Decision:** a non-safety recommendation that loses becomes a `Deferred`
carrying why it lost, what would clear it and when it expires. A safety
recommendation is never deferred and never smoothed.

**Reason:** `decide()` picks a winner, and section 8 requires the loser be
kept. The reason is worth stating precisely: **what displaced it is a
condition, and conditions pass.** Discarding it means the same analysis is
redone from scratch when the constraint lifts, and the second run may not
reach the same conclusion.

Every deferral names its required condition, because a deferral with no
condition is a queue entry nobody can ever clear -- which is how "we will look
at it later" becomes "never". That, not a size cap, is what stops section 28's
indefinite queue.

**Two asymmetries.** A safety horizon is never parked in the queue: a deferral
is a waiting state and safety does not wait. And hysteresis exempts safety
horizons entirely -- a smoothing rule that also smoothed the emergency stop
would be the L61 defect repeated, a guard that suppresses the response to the
thing it guards against.

**And an undated recommendation is expired, not eternal.** It cannot be shown
to be about now, and unknown is not fresh -- except for an emergency, which is
never dropped for being stale, because a stale emergency is a reason to look
rather than a reason to discard.

**Files changed:** `app/portfolio/horizon.py`
**Test result:** 30 passed, including all twelve of section 44
· **Risk level:** medium


## 2026-09-06 - L66: a prediction may tighten and may never loosen

**Decision:** `gate()` takes the verdict the platform already holds and returns
`max(current, everything the scenario adds)`. No input lowers a verdict.

**Reason:** the asymmetry is the cost function, not caution. A scenario that
wrongly predicts danger costs a missed opportunity; one that wrongly predicts
safety costs the portfolio. Those are different mistakes and must not have the
same authority.

The way that gets lost is not a bad decision -- it is a gate that computes its
verdict symmetrically from a forecast, because computing it symmetrically is
the obvious implementation and reads as neutral. Taking `current` as an
argument and returning a max makes the property structural: section 47's
"prediction must never become direct execution" becomes a fact about the return
value rather than a policy somebody enforces.

Same move as L61 for actions, L60 for layers and L64 for scoring: **where a
safety property must be absolute, make the alternative unrepresentable rather
than forbidden.** That is now four levels where it was the right answer.

**Files changed:** `app/portfolio/scenario.py` (new),
`app/safety/invariants.py` (INV-30), `app/safety/certification.py`
**Test result:** 42 passed; capability-checked by dropping the floor, by
letting unknown confidence read as high, and by burying the unlikely-extreme
matrix row -- six tests fail · **Risk level:** medium


## 2026-09-06 - L66: the risk matrix is enumerated, not computed

**Decision:** twelve explicit cells rather than likelihood x severity.

**Reason:** section 20 says low-likelihood/high-severity events must remain
visible, and a product is exactly what makes them disappear. UNLIKELY x EXTREME
classifies HIGH; under multiplication it would rank below a likely
inconvenience.

The enumerated version is longer and that is the cost. It buys a table where
the row that matters can be read directly and cannot be quietly changed by
someone adjusting a coefficient.

`classify()` has no fallback: an unmapped pair raises rather than returning
LOW. A default there would mean a combination nobody classified silently
becoming the safest possible answer.

**Files changed:** `app/portfolio/scenario.py`
**Test result:** `test_an_unlikely_extreme_scenario_stays_visible` and
`test_the_matrix_is_total` · **Risk level:** low


## 2026-09-06 - L66: a guard that matched substrings flagged the wrong thing

**Decision:** every `Recommendation` carries a declared effect, and the
no-loosening test checks the effect rather than the name.

**Reason:** the first version matched `("INCREASE", "RAISE", "ENABLE", ...)`
against the member name and failed on `DEFER_INCREASE` -- which *withholds* an
increase and is a restriction.

The false positive was harmless and the reason it is worth recording is the
false NEGATIVE it implies: a member called `OPTIMISE_HEADROOM` or
`RELAX_BOUND` would have passed a substring blocklist cleanly. **A blocklist on
strings gets both, and only one of them is loud.**

This is the same lesson as C-4, the L57 lifecycle grep, the L64 import guard
and the L66 `correlation_note` test, all in this session's history: match the
structure, never the characters.

**Files changed:** `app/portfolio/scenario.py`,
`tests/test_scenario_intelligence.py` · **Risk level:** low


## 2026-09-06 - L66: correlation was right for a reason that had stopped being true

**Decision:** `correlation_note()` still reports correlation as unavailable,
with a corrected reason, and the test that pinned the old wording now asserts
the property instead.

**Reason:** it said `market_bars` was empty. The table now holds 700 rows --
all EURUSD, one month. The verdict was still correct, because correlation needs
a common window across two or more instruments and there is one.

**A correct verdict resting on a false reason is worse than it looks.** The
next person does not re-derive the verdict; they read the reason to decide
whether anything has changed. "The table is empty" is checkable in one query
and would have come back false, which invites concluding the whole note is
stale.

The test had to change too, and that is the second half of the finding: it
asserted `"market_bars" in note["reason"]`, so it **blocked the reason being
corrected**. A test that pins a sentence prevents the sentence being fixed. It
now asserts what matters -- unavailable, with a substantive reason and a named
substitute.

**Files changed:** `app/portfolio/exposure.py`, `tests/test_portfolio.py`
**Test result:** passed · **Risk level:** low


## 2026-09-06 - L67: coverage is earned, never assumed

**Decision:** `Coverage.is_covered` is true for one value of five, and there is
no path from an absence to it. A failed run, another account's run, an old run
and a run with no stated confidence all fail to produce COVERED.

**Reason:** this module makes the only POSITIVE claim in the safety stack.
Everything else refuses; a coverage matrix asserts that the portfolio has been
tested against a condition -- and a positive claim is the one that gets quoted
in a meeting.

The failure mode is specific and worth naming: a stress orchestrator that
reports green without having run anything does not merely fail to help. It
manufactures the confidence the rest of this platform is built to withhold, and
it does so with the authority of a system that looks like it measured
something.

Read honestly against this deployment the matrix returns **0 of 16** and
resilience is UNKNOWN, which blocks autonomy. A test asserts that reading so it
cannot drift into a green report as the platform changes around it.

**Files changed:** `app/portfolio/stress.py` (new),
`app/safety/invariants.py` (INV-31), `app/safety/certification.py`
**Test result:** 41 passed; capability-checked by making absence return
COVERED, by letting UNKNOWN count as covered, by demoting the hard-failure
check and by removing the combination cap -- ten tests fail
· **Risk level:** medium


## 2026-09-06 - L67: the cascade graph is almost entirely HYPOTHETICAL

**Decision:** every edge carries an `Evidence` label, exactly one is OBSERVED,
and none is INFERRED.

**Reason:** section 11 says not to assume causality from correlation alone, and
this platform is in an unusually strong position to obey it -- it has no
correlation data at all. `exposure.py` has reported correlation unavailable
since L53 and still does.

A cascade graph is the most quotable artefact a stress system produces.
Volatility to spread to slippage to execution quality to margin to drawdown is
a chain every reader will find plausible, and that is the danger: **drawn from
textbook mechanisms and labelled OBSERVED it would be the most convincing false
claim in the repository**, convincing precisely because every edge is
individually reasonable.

The one OBSERVED edge is drawdown to risk restriction, because the daily loss
limit genuinely does halt new entries and there is a test for it. INFERRED is
claimed nowhere, since inference would need the correlation that does not
exist.

**Files changed:** `app/portfolio/stress.py`
**Test result:** `test_almost_every_cascade_edge_is_hypothetical`
· **Risk level:** low, and the label is the whole value


## 2026-09-06 - L67: generation stops loudly, and before it runs

**Decision:** every combinatorial cap returns a `GenerationStop` naming which
limit was hit, with an empty result, checked before generating rather than
after.

**Reason:** two separate points, and the second is the one that is easy to miss.

**Loudly**, because a generator that silently truncates produces a coverage
report whose gaps look like decisions. A caller receiving 24 of 560
combinations with no indication has been handed a partial sweep labelled as a
sweep.

**Before**, because the explosion happens during generation. Checking
afterwards means paying for it first, which is the opposite of what a cap is
for.

Recursion depth is capped on the same reasoning as L51's restart budget: a
scenario that generates scenarios is a loop, and a loop that generates work is
the one that does not stop on its own. Fourth costume for that defect shape.

**Files changed:** `app/portfolio/stress.py`
**Test result:** `test_generation_stops_when_combinations_exceed_the_limit`,
`test_recursive_generation_hits_a_circuit_breaker` · **Risk level:** low


## 2026-09-06 - L67: an allow-list with the reason, not a loosened rule

**Decision:** `stress.py` is named explicitly in L66's import guard rather than
the guard being relaxed.

**Reason:** adding this module made L66's "nothing under `app/` imports the
scenario engine" test fail, correctly -- `stress.py` imports
`app.portfolio.scenario` to reuse `ScenarioDefinition`, `Recommendation` and
the risk matrix, which section 4 explicitly asks for.

The property being protected is that **the safety architecture does not depend
on the scenario engine**, and it still holds: stress is itself unreachable, and
its own test asserts nothing imports it. The chain terminates.

The tempting fix was to widen the guard to "no *reachable* module imports it",
which would have been correct and would have made the rule harder to check and
easier to erode. A one-entry allow-list carrying the reason, plus the second
test that makes the chain terminate, states the same property in two places
that each fail independently.

**Files changed:** `tests/test_scenario_intelligence.py`
**Test result:** passed · **Risk level:** low


## 2026-09-06 - L67: certification is deliberately NOT wired to stress coverage

**Decision:** section 33 suggests certification may require minimum stress
coverage. It is not wired.

**Reason:** coverage on this deployment is 0 of 16, and it cannot be otherwise
-- no stress can meaningfully run against one instrument and one open position.
Wiring it would immediately and permanently suspend autonomy for a reason that
is true and not actionable.

**A permanent red light nobody can clear is worse than an unwired gate**, and
not because it is inconvenient: it is the condition under which people learn to
route around a control. The gate is real and belongs; it belongs the day
stresses can run.

Recorded as unwired, with the trigger stated, rather than implemented into
something that would have to be disabled within a week.

**Files changed:** none -- that is the decision
**Documented in:** `STRESS_CERTIFICATION_POLICY.md` · **Risk level:** low


## 2026-09-06 - PROJECT_STATE.json is generated, because hand-written state drifts

**Decision:** `tools/project_state.py` measures the deployment and writes
`PROJECT_STATE.json`. The file is no longer hand-edited, CLAUDE.md says so, and
`tests/test_project_state.py` runs in CI.

**Reason:** the hand-written version carried `"generated_by": "hand"` and had
drifted from the database on **six counts at once**. It reported 0 datasets
against 1 READY dataset of 638 rows, 0 training runs against 3, 0 model
versions against 1, and repeated the claim that `market_bars` was empty when the
table held 705 bars across 2 symbols.

**None of those drifts was careless.** Every one was true when it was written.
That is the whole finding: a hand-written state file is a photograph presented
as a window, and no amount of care fixes a photograph. The only durable fix is
to take it on demand.

**The rule the generator obeys is the repository's own:** a figure that could
not be measured is absent and named in `unmeasured`, never written as 0. Zero
is a measurement -- "there are no datasets" -- and `int(out or 0)` is the
natural thing to write and the exact fabrication the rest of the platform
refuses. A capability check confirms an unreachable database produces no
figures rather than sixteen zeroes.

**Files changed:** `tools/project_state.py` (new),
`tests/test_project_state.py` (new), `PROJECT_STATE.json` (regenerated),
`.github/workflows/tests.yml`, `CLAUDE.md`
**Test result:** all four checks pass · **Risk level:** low


## 2026-09-06 - a claim about a mutable table goes stale; a claim about a requirement does not

**Decision:** "`market_bars` is empty" was replaced with "`market_bars` covers
one instrument" in 69 lines across 35 files.

**Reason:** the first phrasing is a fact about a table that changes. It went
false on the first ingestion and stayed in the repository afterwards, in
thirty-five files, every one of them true when written -- including three lines
I wrote myself at L65 and L66 asserting "zero open positions" when there was
one. I did not check; I carried the claim forward.

The second phrasing is a fact about the condition that actually matters:
correlation needs a common window across two or more instruments, and one
instrument is one instrument whether the table holds 700 bars or 700,000. It
stays true until the thing it is about changes.

**This is the general form, and it is worth keeping:** when a document has to
state a limitation, state the REQUIREMENT that is unmet rather than the
observation that it is unmet. The observation has a shelf life.

Two other stale claims of the same class were corrected: "no dataset has been
built from real data" (one has -- `eurusd-h1` v1, 638 rows) and the "zero open
positions" lines.

Six occurrences were deliberately left: they are in `DECISIONS.md`,
`MIGRATION_STATUS.md` and three L66/L67 documents recording the correction as
history, where "it said the table was empty" is the accurate past tense.

**Files changed:** 35 documents and modules
**Test result:** 267 backend tests re-run over the touched areas, all passing
· **Risk level:** low


## 2026-09-06 - MIGRATION_STATUS stops pretending its first table is current

**Decision:** the levels 00-42 table is now labelled a historical snapshot, and
a three-row index at the top separates measured state, level history and next
actions.

**Reason:** it was the first thing a reader saw in the largest file in the
repository, and it contradicted the same file further down -- calling L40
partial and L42 not started where the sections below record both COMPLETE. It
also stopped twenty-five levels early and quoted a backend baseline of 980
tests, stale by roughly eighteen hundred.

The alternative was to maintain a sixty-seven-row table. That would have
reproduced the defect at greater length: **the reason the table drifted is that
it is a count, and counts drift.** Separating the three sources means each is
maintained by the thing that can maintain it -- the generator measures, the
level sections explain, the priority file decides.

No test count is restated in that header on purpose.

**Files changed:** `MIGRATION_STATUS.md` · **Risk level:** low

