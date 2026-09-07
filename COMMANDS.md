# COMMANDS.md

Standing commands for this project, and the loop they belong to. Keep this
file: it is the contract between what you type and what happens.

```
00_MASTER_CONTEXT -> 01_AUDIT -> PROJECT AUDIT -> START MIGRATION
      -> LEVEL 02 -> TEST -> LEVEL 03 -> TEST -> ... -> LEVEL 42 -> FINAL AUDIT
```

Every level ends with **STOP**. Nothing advances to the next level on its own;
you say when.

---

## Level commands

| Command | Meaning |
|---|---|
| `START AUDIT` | Run the repository audit and write the four audit documents. Never modifies code. |
| `START MIGRATION` | Begin the ladder at the earliest incomplete level. |
| `START NEXT LEVEL` | Do the earliest incomplete level in `MIGRATION_STATUS.md`, then stop. |
| `START LEVEL NN` | Do that level out of order. The progress log records that it was taken out of order and which levels remain unstarted. |
| `RUN FINAL AUDIT` | Level 42. Verifies every area, writes `FINAL_AUDIT.md`, and refuses to call the project production-ready while critical items remain. |

## Verification commands

| Command | What runs |
|---|---|
| `RUN ALL TESTS` | Research suite, backend suite, frontend suite, plus lint and type checks on both stacks. |
| `RUN FULL INTEGRATION TEST` | Level 40. End to end through the pipeline in paper or demo only, including the failure cases. |
| `RUN SECURITY AUDIT` | Level 39. Auditing plus a search for exposed secrets. |
| `CHECK PROJECT STATUS` | Read back `MIGRATION_STATUS.md` and `PROJECT_PROGRESS.md` with current test counts. |
| `CHECK ARCHITECTURE` | Compare the code against `ARCHITECTURE.md` and report drift. |
| `CHECK FOR DUPLICATE CODE` | Look for second implementations of something that already exists. |
| `CHECK FOR SECURITY ISSUES` | Read-only pass over the security surface. |
| `CHECK TRADING SAFETY` | Verify the invariants in the table below. |
| `PREPARE DEPLOYMENT` | Level 41. Compose files, health checks, restart policies, production build verified locally. |

---

## The safety invariants, and how to check them

`CHECK TRADING SAFETY` verifies each of these and reports the ones that are
not yet built rather than passing them silently.

| Invariant | Where it lives now |
|---|---|
| `TRADING_MODE=paper`, `LIVE_TRADING=false` by default | `backend/app/core/settings.py`, tested |
| Live execution blocked until every gate is built | `LIVE_GATES`; `/health` lists the blockers |
| Real accounts refused in code | `tools/mt5_paper.assert_demo`, unaffected by any setting |
| Risk engine veto | **not built** (L17) |
| Kill switches | **not built** (L17) |
| Order idempotency | `orders.intent_id` is unique; the OMS that uses it is **not built** (L19) |
| Never retry an uncertain order | Contract exists for closes (L21); order side **not built** (L19) |
| Broker reconciliation | **not built** (L10, L38) |
| Restart recovery | **not built** (L38) |
| Webhook replay protection | **not built** (L09) |
| AI cannot bypass risk | AI layer **not built** (L24, L27) |
| Backtest and replay leakage prevention | `rule_backtest.simulate()` enters on the next bar's open and books the loss on an ambiguous bar |
| Monitoring never replaces a live model | `app/monitoring/findings.FORBIDDEN_ACTIONS`, tested |

---

## Running the suites

**Always run backend commands from `backend/`.** Ruff's configuration lives
there, and a run started at the repository root reformats `tools/` and
`tests/`, which are deliberately outside its scope.

```bash
# Research toolkit (unchanged by the migration)
python -m pytest tests -q

# Backend
cd backend
ruff check . && ruff format --check . && mypy && pytest -q

# Backend including migration tests (needs a throwaway database)
docker compose up -d postgres
docker exec tr-postgres psql -U trade -d trade -c "CREATE DATABASE trade_test"
TEST_DATABASE_URL="postgresql+asyncpg://trade:trade@127.0.0.1:5440/trade_test" pytest -q

# Frontend
cd frontend
npm run lint && npm run typecheck && npm test && npm run build
```

## Running the platform

```bash
cp .env.example .env
docker compose up -d postgres redis
cd backend
alembic upgrade head
BOOTSTRAP_ADMIN_PASSWORD='choose a long one' python -m app.auth.bootstrap --email you@example.org
python -m app.symbols.seed
python -m app.symbols.sync_mt5          # contract specs, from a live terminal only
uvicorn app.main:app --port 8000
```

The research toolkit is unchanged and keeps its own entry points; see
`README.md` and `NIGHTLY.md`.

---

## House rules these commands obey

1. **Inspect before building.** Every level starts by reading what exists.
2. **Never duplicate.** If something already does the job, reuse, refactor or
   wrap it. Where a second implementation is unavoidable, a test locks the two
   together (see the BH-FDR test in `tests/test_monitoring.py`).
3. **Never fake.** No simulated broker execution presented as real, no
   invented market data, predictions, backtest results or connections. A fake
   adapter labels every value it produces.
4. **Refuse rather than default.** A missing contract spec, an unknown
   floating P&L, a sample too small to measure: each returns a refusal naming
   what is missing.
5. **"Not measured" is not "fine".** Monitoring reports insufficient data as
   its own state, and a level's absence is reported rather than skipped.
6. **No destructive migration without justification**, written into the
   migration file, with a guard that refuses to run if the premise is false.
7. **Stop at the end of a level** and report what was built, what was found,
   and what was deliberately left out.
