# DEPLOYMENT_GUIDE.md

How to deploy. Every command here was run at L41 against the real stack unless
marked otherwise.

---

## Prerequisites

* Docker with Compose v2.
* A host with ~4 GB free RAM (the production limits total 5.5 GB across six
  containers; they are limits, not reservations).
* For TLS: a DNS name pointing at the host and a certificate. Nothing here
  fetches or generates one.
* **Not required:** an MT5 terminal. The platform starts, is healthy, and
  refuses to trade without one — which is the correct state, not a failure.

---

## Development

```bash
cp .env.example .env
docker compose up -d --build
```

`docker-compose.override.yml` loads automatically, so the stack publishes
`127.0.0.1:5440` (postgres), `6390` (redis), `8000` (api) and `8080` (nginx).

Then, once:

```bash
docker compose exec api alembic upgrade head
docker compose exec api python -m app.symbols.seed
BOOTSTRAP_ADMIN_PASSWORD='...' docker compose exec -T api \
  python -m app.auth.bootstrap --email you@example.org
```

Registration never creates an admin; that command does.

**Test through `:8080`, not `:8000`.** The proxy is the path production takes,
and the WebSocket defect L41 found existed precisely because everyone tested
the direct port.

## Stamping a build

An image built without these reports `"stamped": false` on `/health` and is
**not deployable to production** — a release you cannot identify is one you
cannot confirm or roll back to.

```bash
export GIT_COMMIT=$(git rev-parse HEAD)
export BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
export RELEASE_VERSION=0.41.0
docker compose up -d --build
curl -s http://127.0.0.1:8080/health | jq .release
```

Verified at L41:

```json
{"service":"trade-research-platform","version":"0.41.0-rc1",
 "commit":"c961abfc1667","built_at":"2026-09-05T14:56:35Z",
 "stamped":true,"schema_head":"0025_admin_audit"}
```

---

## Production

### 1. Configuration

Create the environment file. **Every one of these is mandatory** — the
production overlay uses `${VAR:?...}` and refuses to start without them, rather
than silently falling back to a development value:

```
POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
REDIS_PASSWORD
DATABASE_URL, REDIS_URL
CORS_ORIGINS          # explicit origins; a wildcard is refused at startup
TR_SERVER_NAME        # the DNS name on the certificate
TLS_CERT_DIR          # host path holding fullchain.pem and privkey.pem
GIT_COMMIT, BUILD_TIME, RELEASE_VERSION
```

Optional, and unset means the feature is off rather than broken:
`TV_WEBHOOK_SECRET` (unset **refuses every alert**), `SMTP_*`, `DISCORD_*`,
`APP_BASE_URL`.

### 2. The deployment sequence

Step 14's order, and the order matters: **back up before migrating, and check
before applying.**

```bash
# --- 1. BACK UP. Not optional. See "if this fails" below.
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" \
  > "backup-$(date -u +%Y%m%dT%H%M%SZ).dump"

# --- 2. MIGRATION CHECK. What the database is at, and what the code expects.
docker compose exec -T api alembic current
docker compose exec -T api python -c \
  "from app.core.release import schema_head; print('code expects', schema_head())"
docker compose exec -T api alembic history -r current:head   # what would run

# --- 3. BUILD
export GIT_COMMIT=$(git rev-parse HEAD) \
       BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
       RELEASE_VERSION=0.41.0
docker compose -f docker-compose.yml -f docker-compose.prod.yml build

# --- 4. STOP. A brief controlled outage, deliberately -- see
#        DEPLOYMENT_ARCHITECTURE.md on why not blue/green.
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop api worker

# --- 5. MIGRATE, with nothing consuming signals.
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api \
  alembic upgrade head

# --- 6. START
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# --- 7. HEALTH CHECK
curl -fsS https://$TR_SERVER_NAME/health/live
curl -fsS https://$TR_SERVER_NAME/health/ready
curl -s   https://$TR_SERVER_NAME/health | jq .release   # stamped == true?

# --- 8. INTEGRATION CHECK -- the smoke tests below.
```

### 3. Smoke tests

Run against the deployed stack. **None of them sends an order.**

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Process alive | `curl -fsS $URL/health/live` | 200 `alive` |
| 2 | Dependencies | `curl -fsS $URL/health/ready` | 200, database + redis `healthy` |
| 3 | Correct build | `curl -s $URL/health \| jq .release.commit` | your commit |
| 4 | Schema | `curl -s $URL/health \| jq .release.schema_head` | matches `alembic current` |
| 5 | Trading readiness | `curl -s -o /dev/null -w '%{http_code}' $URL/health/trading` | **503 is correct** with no adapter |
| 6 | Mode | `curl -s $URL/health \| jq '.trading_mode,.live_trading'` | `"paper"`, `false` |
| 7 | **WebSocket through the proxy** | see below | **HTTP 101** |
| 8 | Webhook refuses an unsigned alert | `POST $URL/api/v1/webhooks/tradingview` with no secret | 401 |
| 9 | Datastores are private | `nc -z HOST 5440; nc -z HOST 6390` | refused |
| 10 | Workers running | `docker compose logs worker \| grep worker_started` | notification + monitoring |

Check 7 is the one people skip and is the one that was broken:

```bash
curl -isS -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  -H "Cookie: tr_session=<a real session>" \
  "$URL/api/v1/realtime/ws"
# 101 = the proxy forwards the upgrade.
# 404 = it does not, and every live update in the platform is dead.
```

### 4. Post-deployment paper trading test

Step 30, end to end, in paper mode. Verified at L41 against the running stack:

```bash
SECRET="$TV_WEBHOOK_SECRET"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BODY="{\"secret\":\"$SECRET\",\"ticker\":\"OANDA:EURUSD\",\"action\":\"buy\",
       \"time\":\"$NOW\",\"strategy\":\"<a registered strategy key>\",
       \"price\":\"1.10000\",\"sl\":\"1.09500\",\"tp\":\"1.11000\",\"bar_time\":\"$NOW\"}"

curl -sS -X POST "$URL/api/v1/webhooks/tradingview" -H 'Content-Type: application/json' -d "$BODY"
curl -sS -X POST "$URL/api/v1/webhooks/tradingview" -H 'Content-Type: application/json' -d "$BODY"
```

Expected, and observed:

```
1st: {"status":"accepted","detail":"signal recorded","signal_id":"197b6075-..."}
2nd: {"status":"duplicate","detail":"already received; no second signal was created",
      "signal_id":"197b6075-..."}          <- the SAME id
```

Both responses carry `"note":"a recorded signal, not a trade; nothing was
executed"`. **The webhook records; it does not execute.** Execution is a
separate, operator-started worker (`POST /v1/execution/start`), and with no
broker adapter registered it produces no order at all.

Confirm no order was created:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT count(*) FROM orders WHERE created_at > now() - interval '5 minutes';"
```

---

## If a step fails

Step 32's order. **Never assume a failed deployment means no order was
submitted.**

1. **Stop unsafe execution first.** `POST /v1/recovery/safe-mode/enter` needs
   no step-up, blocks order submission at three server-side paths, and blocks
   nothing observational.
2. **Preserve state.** Do not `docker compose down -v`. Do not reset anything.
3. `docker compose logs api worker --since 15m`.
4. `alembic current` — is the schema where you think it is? A migration that
   failed part-way is the dangerous case.
5. `GET /v1/recovery/status` — what did the startup sequence find?
6. `GET /v1/orders?status=unknown` — any order whose venue state was never
   established.
7. `GET /v1/positions` and `POST /v1/recovery/reconcile` (which reports; it
   never repairs).
8. Roll back only once you know the schema state — see `ROLLBACK_RUNBOOK.md`.
9. Re-verify `/health/ready` and `/health/trading`.
10. **If anything is unresolved, leave safe mode engaged.** It is the correct
    resting state and nothing about it degrades over time.
