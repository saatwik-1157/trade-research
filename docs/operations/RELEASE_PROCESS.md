# RELEASE_PROCESS.md

How a commit becomes a running deployment.

```
COMMIT -> LINT -> TYPE CHECK -> UNIT TEST -> INTEGRATION TEST -> SECURITY TEST
       -> BUILD -> STAMP -> VERIFY IMAGE -> TAG
       -> (manual) DEPLOY -> MIGRATE -> SMOKE -> E2E PAPER -> MONITOR
```

Everything up to TAG is automated. Everything after it is a person following
`DEPLOYMENT_GUIDE.md`, and that is deliberate — see "Why deployment is manual".

---

## CI (`.github/workflows/tests.yml`)

| Job | Runs | Gate |
|---|---|---|
| `test` | six research scripts on Python 3.10, 3.12, 3.14 | the sourcing and indicator claims |
| `backend` | `ruff check` · `ruff format --check` · `mypy` · `pytest -q` | lint, types, **2338 tests** incl. security and integration |
| `frontend` | `lint` · `typecheck` · `test` · `build` | 247 tests, and the production build |
| `images` | build both images, stamp, verify, validate compose | **`needs: [backend, frontend]`** |

The `needs:` is the "do not deploy if critical tests fail" gate. It is a
dependency in the graph rather than a step somebody can reorder or skip.

`images` verifies four things that a health check cannot:

1. **The image is stamped** and can read its own migration head. An unstamped
   image is not deployable — a release you cannot identify is one you cannot
   confirm or roll back to.
2. **The research toolkit is importable.** `app/strategies/indicators.py` loads
   `rule_backtest` and `rule_search` from `tools/` at *call* time, so an image
   missing them passes every health check and fails on the first backtest.
   This is why the build context is the repository root.
3. **Every `app.*` module imports inside the image** — catches a dependency
   declared in the development requirements and absent from the image.
4. **The image is non-root**, and **production publishes only nginx 80/443**
   (parsed out of the merged compose config).

### CI holds no secrets

The whole pipeline runs without a credential: the tests use SQLite and
`FakeBroker`, and the build takes only `GIT_COMMIT`, `BUILD_TIME` and
`RELEASE_VERSION`, none of which is secret.

That is the strongest form of "production secrets must not reach tests" —
there are none to reach them. A future deploy job must use its own credentials.

---

## Versioning

```
tag push        ->  tr-api:v1.2.3
any other push  ->  tr-api:0.0.0-<12-char sha>
```

**Never `latest`.** L41 found the deployment host holding only `:latest`
images, which each build overwrites — so there was nothing to roll back to.
Every build is now addressable by version.

`GIT_COMMIT`, `BUILD_TIME` and `RELEASE_VERSION` are baked as build args and
surface at `/health`:

```json
"release": {"version":"0.41.0-rc1","commit":"c961abfc1667",
            "built_at":"2026-09-05T14:56:35Z","stamped":true,
            "schema_head":"0025_admin_audit"}
```

`schema_head` is the revision the **code** expects, read from Alembic's own
scripts rather than hard-coded. What the **database** is at is a different
question — `alembic current` — and the deployment sequence compares the two
before starting.

## Build reproducibility

Honest assessment: **reproducible enough to identify, not bit-for-bit.**

Pinned: base images (`python:3.12-slim`, `node:24-alpine`, `postgres:16-alpine`,
`redis:7-alpine`, `nginx:1.27-alpine`), the frontend via `package-lock.json` +
`npm ci`, and every build carries its commit.

Not pinned: `backend/requirements.txt` uses floors (`numpy>=1.24`), so two
builds a month apart can resolve different patch versions. A lock file would
fix it and was not added at this level — it is a change to how every developer
installs, and it belongs in a pass that also updates the contributor docs.
**Recorded as a gap in `PRODUCTION_READINESS.md`, not glossed.**

What makes this tolerable: an image is identified by its commit, so "which
build is running" is always answerable even when "would rebuilding produce
identical bytes" is not.

---

## Why deployment is manual

There is no registry and no target host, and a pipeline that pretends to deploy
is worse than one that does not try.

Beyond that, this platform's safe deployment is a **brief controlled outage** —
stop, migrate, start, verify — rather than a rolling replacement. Two versions
running at once means two processes each holding a safe-mode latch and each
running a startup reconciliation against the same account. `DEPLOYMENT_ARCHITECTURE.md`
sets out why blue/green was evaluated and refused for this application.

Automating that sequence is worth doing when there is a target to automate
against. Automating it now would mean writing and never running it.

## Checklist for a release

1. `main` is green — all four CI jobs.
2. Tag: `git tag v0.41.0 && git push --tags`.
3. CI builds and stamps `tr-api:v0.41.0` and `tr-frontend:v0.41.0`.
4. **Note the currently deployed version** (`curl $URL/health | jq .release`).
   You cannot roll back to a version you did not write down.
5. Follow `DEPLOYMENT_GUIDE.md` § Production: back up → migration check →
   build → stop → migrate → start → health → smoke → E2E paper.
6. Watch `/v1/monitoring/events` and `/v1/recovery/status` for the first pass.
7. If anything is wrong: `ROLLBACK_RUNBOOK.md`. Engage safe mode first.
