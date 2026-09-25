# Model registry and model lifecycle (Level 28)

The registry is the authoritative answer to one question:

> **Which model versions exist, what happened to them, and which are eligible
> for use?**

---

## 1. What was already here

L28 is a level of consolidation rather than construction. `model_versions` has
existed since L05 and carried `draft` / `validated` / `promoted` / `retired`
since then; L24 wrote provenance onto it, L25 wrote candidates into it, L26
judged them, and L27 built the eligibility boundary that L28 was always meant to
take over.

| Component | Where | Verdict |
| --- | --- | --- |
| `model_versions`, `models`, the status CHECK | L05, extended at L24 | **KEEP + MODIFY** — four statuses added, fourteen nullable columns, two CHECKs |
| `ModelRegistry` (in-process, holds loaded objects) | `app/ai/registry.py` (L24) | **KEEP** — it is a cache of loaded models, not a lifecycle |
| `register_version()` writing a `draft` | `app/ai/service.py` (L24) | **KEEP** — the entry point into the lifecycle |
| `model_from_version()` | `app/ai/loader.py` (L26) | **KEEP** — the one deserialiser |
| `validation_runs` and its verdict | L26 | **KEEP** — it is the registration gate |
| `app/ai/eligibility.py` | L27 | **KEEP + MODIFY** — L27 wrote it against L26's verdict and said the switch to `model_versions.status` would be one function; this is that switch |
| `AuditLog` | L05 | **KEEP** — every transition writes one, beside the model-shaped view |
| L07 realtime hub and catalogue | L07 | **KEEP + MODIFY** — one scope and eight event types added |
| The lifecycle, artifacts, deployments, resolution, comparison | *(absent)* | **ADD** |

**Nothing was replaced and nothing was removed.** No second registry, model
loader, version table, prediction format or job queue was created — §47's list,
checked item by item in `PROJECT_AUDIT.md` §26.

---

## 2. The lifecycle

Eight states. The brief lists ten; four are folded or declined, and
`app/ai/lifecycle.DECLINED` records the reason for each — because *"we thought
about it and decided against"* and *"we forgot"* look identical in a schema.

```
   draft ──────► validated ──────► registered ──────► paper ──────► promoted
     │               │                 │  ▲             │  │           │
     │               │                 │  │             │  │           │
     ▼               ▼                 ▼  └─────────────┘  │           │
  rejected        rejected          retired   (deployment  │           │
  (terminal)      (terminal)       (terminal)   ended)     │           │
                                                            ▼           ▼
                                                      rolled_back ◄─────┘
                                                            │
                                                            ▼
                                                   registered / retired
```

| State | Means | Existing name? |
| --- | --- | --- |
| `draft` | a trained candidate | yes — the brief's CANDIDATE |
| `validated` | L26 returned PASS or CONDITIONAL. About the **evidence** | yes |
| `registered` | the artifact loads, its digest matches, the lineage is complete, the features are compatible. About the **artefact** | new |
| `paper` | deployed to paper trading for a scope | new |
| `promoted` | the version a scope resolves to | yes — the brief's ACTIVE |
| `rejected` | refused. Terminal | new |
| `rolled_back` | withdrawn because something was wrong | new |
| `retired` | deliberately withdrawn. Terminal | yes |

### The states that were declined

| Brief's state | Why not |
| --- | --- |
| CANDIDATE | already exists as `draft`. A second word for one state is how a reader comes to believe they are two. |
| VALIDATING | **nothing could set it.** L26 deliberately writes no model status, and `validation_runs.status` already says a run is running. The copy nobody updates is the one somebody reads. |
| FAILED | a failed validation is on `validation_runs`; the version it judged becomes `rejected`. Two terminal failure states on the version would differ only in which system said no. |
| ACTIVE | already exists as `promoted`, in the CHECK since L05. Renaming would rewrite the history of every row that held it. |

`test_every_declared_state_is_reachable` walks the transition table and asserts
that every declared status can be entered. A state nothing can reach makes the
vocabulary a wish list.

### Two properties worth reading off the table

**There is exactly one edge into `promoted`, and it starts at `paper`.** That is
§12 — *never Training → Validation PASS → LIVE* — and it is the transition table
that enforces it rather than a check in a function. A test enumerates the
sources of that edge and asserts the list is `[paper]`.

**`validated` does not serve inference.** Validation says the evidence supports
the candidate; registration says the artifact loads and still matches its
feature contract. Both are checks, both can fail independently, and serving from
a version that passed only the first is the gap §16 closes.

---

## 3. Registration — the gate

`register()` moves `draft → validated → registered` in one call, because they
are one decision with two halves that must both hold. Both are recorded as
separate lifecycle events, so the history shows which gate a version got through
and which one it stopped at.

**Half one — L26's verdict.** The most recent *completed* validation run is read
fresh from the table, not from a column on the version: the column is what
registration is about to write, and reading what you are about to write is not a
check. PASS and CONDITIONAL pass; FAIL is refused; **BLOCKED is refused in its
own words** — *"a check could not be evaluated, so nothing was established
either way"* — because that leads to a different next action than a failure
does.

**Half two — the registry's own checks.** The feature set version matches what
this deployment computes; every feature the version declares is one the engine
knows; the artifact is inspected, hashed, and then **actually loaded**, because
a digest proves the bytes did not change and only loading proves they were ever
a model.

**A candidate that fails half two is `rejected`, not left as a draft.** The
failure is recorded with its reason rather than leaving a row somebody retries
without knowing why it failed last time.

---

## 4. Artifact integrity

**The artifact is structured JSON on the version row, not a file.** L24 decided
that: the parameters of these three families are a handful of floats each, and
keeping them structured means a version can be inspected, diffed and queried.
§34 asks to reuse existing storage; this *is* it.

That answers most of §33 by construction:

* **Nothing is deserialised.** No pickle, no joblib, no `torch.load`, no
  `__reduce__`. An artifact is parsed as JSON and read field by field into a
  frozen dataclass by `app/ai/loader.py`, which refuses an unrecognised `kind`.
* **There is no upload route and no path field.** A caller cannot name a file, a
  URL or a directory, so there is nothing for a path traversal to traverse. The
  only way an artifact enters this platform is a training run. A test asserts no
  route contains `upload`.
* **An artifact is data, never code.** A metadata field that executed something
  would need something to execute it, and nothing here does.

What is *not* free is integrity, and that is what the digest is for.

```
artifact (dict)  ──►  canonical JSON (sorted keys, no whitespace)  ──►  sha256
```

**Sorted keys matter.** Without canonicalisation the check would fire the first
time SQLAlchemy round-tripped a row through JSONB, which reorders keys — and the
fix would have been to delete the check.

**The digest covers the artifact, not the row.** Metrics, status, timestamps and
deployment history all change legitimately; the fitted parameters do not, and §6
says a change to them is a new version. Hashing the whole row would make the
check fire on every ordinary write and be switched off within a week.

**A version with no recorded digest is reported as *unverifiable*, not as
verified.** "Never checked" and "checked and fine" must not look the same. Two
CHECK constraints put that in the schema:

```sql
status IN ('draft','validated','rejected') OR artifact_sha256 IS NOT NULL
status IN ('draft','validated','rejected') OR validation_run_id IS NOT NULL
```

---

## 5. Deployments, scope and the NULL problem

`model_deployments` answers *"which version does this scope resolve to?"*
separately from `model_versions.status`, and the separation is the point: a
status says what a version **is**, a deployment says where it is **used**. One
version can be active on one strategy and superseded on another; a column on the
version could not say that.

A scope has three axes plus an environment. **`None` on an axis means "not
restricted on it"** — a recorded absence, not a wildcard somebody typed. Empty
strings are refused, because `''` and `None` would be two spellings of one fact.

### The defect the tests found

The first version of the unique index covered
`(model_key, strategy_key, symbol, timeframe, environment)` with a partial
`WHERE status = 'active'`. It did not work, and the test that inserted a second
active deployment got no error:

> **NULLs are DISTINCT in a unique index.**

So any number of rows could share the *unrestricted* scope — which is the most
common one. The guarantee would have been absent exactly where it mattered most
and present everywhere it was easy to test.

The fix is a `scope_key` column: the three axes as one NOT NULL string,
`"strategy|symbol|timeframe"`, derived by `Scope.key()` so no caller can write
an inconsistent one. The nullable columns stay as they are — they are what a
reader and a query use, and "not restricted" must remain expressible as NULL.
The unique index covers `(model_key, scope_key, environment)`.

Two administrators promoting at once now produce one winner and one
`IntegrityError`, which is §31 — a `SELECT`-then-`INSERT` would have produced
two active versions and no error at all.

### Superseding is not retiring

When a newer version is deployed, the one it replaces returns to `registered`,
**not** to `retired`. §14's example says otherwise, and the reason for the
deviation is §20 and §22 together: retiring the previous version on every
promotion makes every rollback a resurrection of a terminal state, and §22 says
retirement should be a deliberate, separate act. The deployment row records the
supersession; the version stays eligible.

---

## 6. Resolution

```
Request(model_key, strategy_key, symbol, timeframe, environment)
        │
        ├─ active deployments for this model and environment
        ├─ keep those whose scope COVERS the request
        ├─ sort by specificity: strategy, then symbol, then timeframe
        ▼
    one deployment  ──►  its version  ──►  five checks:
                                            status serves inference
                                            names the validation run that gated it
                                            artifact digest matches
                                            feature set matches
                                            scope is compatible
        ▼
    Resolution, carrying every check it made
```

**Never `latest`.** A resolution answers *what is deployed here*, not *what is
newest*. A model that changed because someone registered a new version, without
anyone deploying it, is a strategy nobody can reproduce.

**A narrower deployment does not answer a broader question.** A model deployed
for EURUSD is not an answer to a question that named GBPUSD, and it is not an
answer to a question that named no symbol either — the caller would get a EURUSD
model for an unspecified instrument.

### Caching

`ModelCache` is keyed by **version id, never by scope**, and that is the whole
design. A scope's answer changes on every promotion and rollback; a version's
artifact does not, because §6 makes a changed model a new version. So a
lifecycle change needs no invalidation — the resolution runs first and asks for
a different key. §30's stale-authorisation problem cannot arise.

The digest is re-checked on every cache hit. §45 says correctness over caching
speed: hashing a few hundred bytes is cheaper than serving a trading decision
from an artifact that changed under the process.

---

## 7. Level 27 goes through the registry

§28 requires it. `RegistryResolver` replaces `ModelResolver` for a running paper
bot: every model it returns has been through `resolution.resolve`, so it has
passed validation, artifact verification and a compatibility check.

**Resolved once, when the bot starts.** The AI pipeline is synchronous and must
not open a session per signal (§45) — and it makes the version a run used a
**fixed fact for the life of that run**, which is what §37 and §39 need in order
to say which model produced a decision.

**A version mismatch is refused, never substituted.** If a strategy names
`trade_probability v2.1` and the registry resolves `v2.3` for that scope, the
bot refuses to start rather than quietly serving the other version — because
§37 needs a decision to name the model that made it, and a substitution would
make every such record wrong.

`ModelResolver` is kept for backtests and tests that supply their own models, and
its docstring now says which is which.

---

## 8. Audit and lineage

**Every transition goes through one function.** `_transition` checks the move,
writes the status, records a `model_lifecycle_events` row **and** an `AuditLog`
row, and publishes. A second path that skipped any of those would produce a
history with holes in it, and a history with holes is read as a history.

Both tables, not one: the platform already has an audit log an administrator
reads across every resource type, and the lifecycle table is the model-shaped
view of the same facts, indexed for the questions this level asks.

Every row records **both halves** of the transition, the actor and the reason —
so *"who made v2.3 active and what was active before"* is one row rather than an
inference from timestamps. `promote`, `rollback`, `stop` and `retire` all refuse
an empty reason: an audit entry that does not say why is a timestamp.

`lineage()` walks the whole chain — training run, dataset version and
fingerprint, feature and label versions, preprocessing, code version, training
configuration, validation run and verdict, and every deployment — and reports
what is **missing** rather than filling it. An incomplete lineage that looks
complete is worse than one that says what is missing.

---

## 9. Comparison

`compare(a, b)` leads with the reasons two versions may not be comparable —
different datasets, feature sets, label sets, preprocessing or evaluation
periods — **before** the numbers, not in a footnote under them. §18's sentence
is the shape of the module: *do not compare metrics generated from incompatible
datasets without clearly labeling the difference.*

Every delta is `b − a` and carries `lower_is_better`, because a log loss falling
by 0.05 and an AUC falling by 0.05 are opposite events. A metric measured on
only one side is `None`, never `0.0`: a missing figure is not a tie.

Sample sizes are shown beside every figure. A profit factor from 7 trades and
one from 700 are not the same claim, and a table renders them identically.

**No verdict.** This says what two versions measured. It does not say which is
better.

---

## 10. Authorization

§24, mapped onto the existing RBAC rather than inventing six permissions where
the codebase has one granularity:

| Action | Permission | Role |
| --- | --- | --- |
| read the registry, lineage, history, resolution | `manage_ai_models` | trader |
| register a validated candidate | `manage_ai_models` | trader |
| deploy to paper | `manage_ai_models` | trader |
| **promote** | `promote_ai_models` | **administrator** |
| **rollback** | `promote_ai_models` | **administrator** |
| **emergency stop** | `promote_ai_models` | **administrator** |
| **retire** | `promote_ai_models` | **administrator** |

One new permission, granted only to `ADMIN`. A trader may produce a candidate
and register it; deciding that it is the version a scope resolves to is a
different decision.

---

## 11. The realtime defect this level found

§27 asks to reuse the existing Redis/WebSocket infrastructure. Auditing it found
that L25's and L26's events had **never been published**:

```python
async def publish(self, event: Event) -> None:      # one argument
    ...
await self.hub.publish(event, {"job_id": job_id})   # two, since L25
```

Every training and validation event raised a `TypeError`, which the surrounding
`except Exception` caught and logged as a warning. The events never reached the
bus and nothing noticed — because *"a subscriber that never fires is
indistinguishable from a market that never moved"*, which is the sentence
`Hub.publish` itself uses to explain why it refuses an uncatalogued type.

Fixed: both services now construct a real `Event`, and eight types plus a
`model` scope joined the catalogue. Two progress types (`TRAINING_JOB_UPDATED`,
`VALIDATION_RUN_UPDATED`) rather than one per stage — a training run publishes a
dozen stage changes and cataloguing each would make the vocabulary a log format
— and one type per lifecycle transition, on the reasoning that keeps
`ORDER_FILLED` apart from `ORDER_CANCELLED`.

The `model` scope needed an authorization rule, gated at TRADER to match
`manage_ai_models` on the REST surface: a channel readable by someone the API
would refuse is a way around the API.

---

## 12. Trading safety

§49, and each item is structural rather than intended:

* **No path to live.** The only edge into `promoted` starts at `paper`, and it
  is the transition table that says so.
* **`promoted` is not live execution.** It means *the version this scope
  resolves to*. Whether anything executes is decided by `TRADING_MODE`,
  `LIVE_TRADING` and the ten live gates — none of which this package reads or
  writes. A test parses every registry module for those names *as identifiers*,
  not as text, so the docstrings that explain the rule cannot fail the test that
  checks it.
* **No venue.** No registry module imports `app.execution`, `app.oms`,
  `app.risk`, `app.sizing`, `app.brokers`, `app.orders`, `app.positions`,
  `app.paper` or `app.bots`. Parsed with `ast`.
* **No training.** The registry accepts a candidate; it does not create one. It
  imports nothing from `app.training`.
* **No deletion.** No `db.delete` anywhere in the package, and the deployment
  foreign key is `RESTRICT` so the database refuses one too.
* **No `DELETE` route**, and no route path contains `delete`.

Verified after the change: `TRADING_MODE=paper`, `LIVE_TRADING=false`,
`live_execution_allowed=false`, and all ten `LIVE_GATES` still false.

---

## 13. API

| Method | Path |
| --- | --- |
| GET | `/v1/ai/registry` — the lifecycle, the transitions, and what it will not do |
| GET | `/v1/ai/models/{model}/versions` |
| GET | `/v1/ai/models/{model}/versions/{version}` — lineage, artifact check, history |
| POST | `/v1/ai/models/{model}/versions/{version}/register` |
| POST | `/v1/ai/models/{model}/versions/{version}/deploy` |
| POST | `/v1/ai/models/{model}/versions/{version}/promote` *(admin)* |
| POST | `/v1/ai/models/{model}/versions/{version}/rollback` *(admin)* |
| POST | `/v1/ai/models/{model}/versions/{version}/stop` *(admin)* |
| POST | `/v1/ai/models/{model}/versions/{version}/retire` *(admin)* |
| GET | `/v1/ai/models/{model}/deployments` |
| GET | `/v1/ai/models/{model}/history` |
| GET | `/v1/ai/models/{model}/resolve` |
| GET | `/v1/ai/models/{model}/compare?a=…&b=…` |

No `DELETE` anywhere under `/v1/ai`.

---

## 14. What Level 28 does not do

* **No model has been through the lifecycle on real data.** `market_bars` is
  empty here. Every test runs against seeded synthetic series, and on that data
  L26's validation legitimately returns FAIL — which the registry then refuses,
  and that refusal *is* §9 working.
* **No object storage.** These artifacts are kilobytes of JSON. When a family
  arrives whose parameters are megabytes it will need object storage, and that
  is a decision to take then — L24 said the same thing and it is still true.
* **No automatic promotion of any kind**, scheduled or otherwise. Every
  transition is an explicit act by a named actor with a recorded reason.
* **No drift-triggered rollback.** L29's monitoring may detect and alert; the
  decision stays a human's, which is what §26 of L29's own brief will require.
* **No multi-model deployment for one scope.** §14 permits it "unless the
  architecture explicitly supports" it, and this one does not: the unique index
  forbids a second active deployment, deliberately.
