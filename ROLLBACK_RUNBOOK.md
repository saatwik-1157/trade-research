# ROLLBACK_RUNBOOK.md

How to go back, and the cases where you cannot.

---

## The rule

**Roll the application back freely. Roll the schema back only when you have
read the migration.**

The application is a container: replacing it is seconds and reverses cleanly.
The database is not, and a downgrade that drops a column destroys whatever was
written into it since the upgrade — which for this platform is trade history.

## Before you can roll back at all

**You need a previous image tag, and `latest` is not one.** This was found at
L41: the images on the deployment host were tagged only `:latest`, which each
build overwrites, so there was nothing to roll back *to*.

Every build must be tagged with its release version. CI does this
(`tr-api:<tag>` or `tr-api:0.0.0-<short-sha>`, never `latest`). A manual build
must do the same:

```bash
docker build -f backend/Dockerfile \
  --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
  --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --build-arg RELEASE_VERSION=0.41.0 \
  -t tr-api:0.41.0 .
```

Confirm what is deployed and what you can return to:

```bash
curl -s "$URL/health" | jq .release      # what is running
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedSince}}' | grep tr-api
```

If `stamped` is `false`, you do not know what is running. Fix that before you
need to roll back, not during.

---

## Case 1 — application only, schema unchanged

The common case, and the easy one.

```bash
# 1. Stop new work. Safe mode needs no step-up and blocks order submission.
curl -sS -X POST "$URL/api/v1/recovery/safe-mode/enter" \
  -H 'Content-Type: application/json' -b "$COOKIES" -H "X-CSRF-Token: $CSRF" \
  -d '{"reason":"rolling back to 0.40.0 after a failed deploy"}'

# 2. Point the tag at the previous release and bring it up.
RELEASE_VERSION=0.40.0 docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml up -d api worker

# 3. Confirm it is actually the old build.
curl -s "$URL/health" | jq .release.commit

# 4. The startup sequence has re-run by now. Read what it found.
curl -s "$URL/api/v1/recovery/status" -b "$COOKIES" | jq

# 5. Release safe mode -- which re-runs the whole sequence and needs the
#    password again (L39). It clears only the latches whose condition has
#    actually gone, and tells you which are still holding.
curl -sS -X POST "$URL/api/v1/security/step-up" ... scope=SAFE_MODE_EXIT
curl -sS -X POST "$URL/api/v1/recovery/safe-mode/exit" ...
```

**Do not skip step 4.** A rollback is a restart, a restart re-runs
reconciliation, and reconciliation is where you learn whether the failed deploy
left an order whose venue state was never established.

---

## Case 2 — the schema moved

Read the migration before doing anything.

```bash
docker compose exec -T api alembic current
docker compose exec -T api alembic history -r current:-1
sed -n '/def downgrade/,$p' backend/alembic/versions/<revision>.py
```

Then classify it:

| Migration shape | Reversible? | Do |
|---|---|---|
| Added a table | **Yes**, losslessly if empty | old app ignores it; **prefer leaving it** |
| Added a nullable column | **Yes** | leave it. The old code does not select it. |
| Added an index or a constraint | **Yes** | leave it unless it blocks the old code |
| Widened a CHECK (e.g. 0024) | **Downgrade LOSES DATA** | see below |
| Dropped or renamed a column | **No** | restore from backup |
| Backfilled or transformed data | **No** | restore from backup |

**The strong preference is to roll the application back and leave the schema
forward.** Every migration in this repository is additive or widening, and the
old application ignores what it does not know about. A schema downgrade is the
riskier operation in almost every case.

### The worked example: migration 0024

Its downgrade narrows `notifications.severity` from five values back to three
and *collapses* the extra ones:

```sql
UPDATE notifications SET severity = 'INFO'     WHERE severity = 'SUCCESS';
UPDATE notifications SET severity = 'CRITICAL' WHERE severity = 'ERROR';
```

That is **lossy and irreversible**. Every SUCCESS notification becomes INFO
permanently. The migration is honest about it in its own docstring, which is
why it is worth reading rather than trusting the word "downgrade".

Roll the app back to before L34 and leave the schema at 0024. The old code
writes lowercase severities, the widened CHECK still accepts them, and nothing
is lost.

### If you must restore

```bash
# 1. Safe mode, and stop the application. Nothing may write during a restore.
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop api worker

# 2. Restore into a NEW database and compare, before touching the live one.
createdb -U "$POSTGRES_USER" trade_restore_check
pg_restore -U "$POSTGRES_USER" -d trade_restore_check backup-<stamp>.dump
psql -U "$POSTGRES_USER" -d trade_restore_check -c "SELECT count(*) FROM trades;"
```

Then follow `BACKUP_RESTORE.md`'s fifteen-step order. **Reconciliation is step
8 of it and is not optional**: a restored database believes things about the
venue that are as old as the dump.

> **Open blocker.** There is no automated backup and no tested restore. The
> `pg_dump` above is a manual command somebody has to have run.
> `PRODUCTION_READINESS.md` records this as BLOCKED, and it is the reason the
> platform is not production-ready for live trading.

---

## Case 3 — the frontend only

Independent of the backend and always safe to roll back on its own; it holds no
state and no secret.

```bash
RELEASE_VERSION=0.40.0 docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml up -d frontend
```

---

## Case 4 — configuration only

The fastest rollback and the one most often needed, because a bad
`CORS_ORIGINS` or a wrong `DATABASE_URL` fails the deploy at startup.

Keep the previous environment file. Restore it and `up -d`. Note that several
settings **refuse to start** rather than misbehave — a wildcard CORS origin, an
origin carrying a path, and `TRADING_MODE=live` without `LIVE_TRADING=true` are
all startup failures by design, so a bad value shows up immediately rather than
in production traffic.

---

## What a rollback never does

* **It never re-enables live trading.** `LIVE_TRADING=false` is in the
  production overlay, and rolling back to any previous release rolls back to a
  release where it was also false.
* **It never clears safe mode automatically.** Leaving it engaged after a
  rollback is correct: the release changed, and nothing has verified the
  account since.
* **It never deletes trade history**, and no step in this document does.
