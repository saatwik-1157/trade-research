# DEPLOYMENT.md

How the system runs today and how the platform will be deployed. Updated at
every level that changes a runtime entry point.

## Today (audit of 2026-09-02)

There is no deployment. Everything runs on one Windows 11 machine with a
logged-in MetaTrader 5 terminal.

| Entry point | Command | Notes |
|---|---|---|
| Research note | `python tools/snapshot.py TICKER --out reports/TICKER.snapshot.json` then the `trade-analyze` skill | needs network; `SEC_USER_AGENT` recommended |
| Verify a note | `python tools/verify.py reports/X.report.md reports/X.snapshot.json --strict` | no network |
| MT5 read-only | `python tools/mt5_account.py --days 730` | terminal must be running |
| Demo trading, one cycle | `python tools/mt5_paper.py --rule random --live --once` | demo only, enforced in code |
| Overnight harvest | `python tools/run_overnight.py` or `Desktop\start-trading.bat` | see `NIGHTLY.md` pre-flight |
| Ledger merge | `python tools/track_record.py --merge` | idempotent |
| TradingView receiver | `python tools/tv_webhook.py --secret "$TV_WEBHOOK_SECRET"` plus a tunnel | records only |
| Tests | `python -m pytest tests -q` | no network, no MT5 |

Operational hazards recorded in `NIGHTLY.md`: the MT5 algo-trading toggle
does not persist; a backgrounded launch from Claude Code auto mode is refused
by its classifier; stopping a backgrounded loop can orphan the child process.

CI: `.github/workflows/tests.yml` runs the six test scripts on push and PR
against Python 3.10, 3.12 and 3.14. Nothing is built or published.

## Development stack (from L02)

```bash
cp .env.example .env
docker compose up -d postgres redis     # datastores only, host 5440 / 6390
docker compose up -d --build            # + api (8000), frontend, nginx (8080)
docker compose ps
docker compose down                     # keeps the tr-postgres-data volume
```

Host ports are loopback-only and deliberately unusual because other projects
on the development machine already publish 5432–5434, 6379–6380 and 3000.
Container names are prefixed `tr-`. The API container reads
`TRADING_MODE=paper` and `LIVE_TRADING=false` from the Compose file; change
them there or in `.env`, never in the image.

First run after the stack is up (L04):

```bash
cd backend
alembic upgrade head                                   # users, auth_sessions
BOOTSTRAP_ADMIN_PASSWORD='...' python -m app.auth.bootstrap --email you@example.org
```

Registration never creates an admin; this command does, or promotes an
existing account. Set `ALLOW_REGISTRATION=false` for a closed instance.

Verified on 2026-09-02: `tr-api` healthy, `GET /health/ready` 200 through
host port 8000 with database and Redis both ok. The frontend image builds
`npm ci` inside Docker and was not built locally at L02; the local
`npm run build` was verified instead.

Two Windows-specific facts, both measured: use `127.0.0.1` in URLs (Docker
publishes IPv4 only; `localhost` resolves to `::1` first and hangs), and the
Postgres driver is asyncpg (psycopg's async mode refuses the Proactor event
loop uvicorn forces on Windows).

## Target topology

```
[Windows host with MT5 terminal]
   mt5-worker (Python)  ── Redis ──┐
                                   │
[Docker Compose, any host]         │
   nginx → frontend (Next.js)      │
         → api (FastAPI) ──────────┤
   workers (bots, replay, analytics, AI) ── Redis
   postgres
   redis
```

- The MT5 adapter worker is the one process that cannot be containerised on
  Linux; it runs as a Windows service on the host with the terminal and
  speaks to the rest over Redis and the database.
- Bots run in workers; no browser is needed for automation.
- `TRADING_MODE` and `LIVE_TRADING` are environment variables on the worker
  and the API, default `paper` and `false`.
- Secrets come from the environment or a secrets store; `.env` files are
  gitignored and never baked into images.

## Level assignments

| Item | Level |
|---|---|
| package layout, settings module, venv policy | L02 |
| supervised runner, PID/heartbeat/stop file | L20, L22 |
| Docker Compose for api/workers/postgres/redis/nginx | **L41 — done** |
| MT5 worker as Windows service | **L41 — assessed, NOT done** |
| health, alerting, restart with reconciliation | L37, L38 |

## After L41

**The deployment architecture now lives in `DEPLOYMENT_ARCHITECTURE.md`**, with
`DEPLOYMENT_GUIDE.md` for the procedure, `PRODUCTION_RUNBOOK.md` for operating
it, `ROLLBACK_RUNBOOK.md` for going back, `ENVIRONMENT_CONFIGURATION.md` for the
five environments, `RELEASE_PROCESS.md` for the pipeline and
`PRODUCTION_READINESS.md` for the scorecard. This document stays what it has
always been: the record of how the system runs today and of the research
toolkit's entry points.

Two things above are worth correcting rather than leaving to be inferred.

**The Compose stack is now three files**, not one, and a plain
`docker compose up -d` behaves exactly as it did -- `docker-compose.override.yml`
is loaded automatically and carries the development port publishes. Production
names its files explicitly and therefore publishes only nginx's 80 and 443.

**The MT5 worker was NOT made a Windows service at L41.** It was assessed and
left alone: it needs a Windows host with a logged-in terminal, no broker
adapter has ever been registered, and wrapping an unregistered adapter in a
service manager would be automating a path nothing has tested. `NIGHTLY.md`
remains the operational record for the MT5 host.

**One defect in the target topology above was found and fixed at L41.** The
nginx `/api/` block carried no WebSocket upgrade headers, so the realtime feed
was dead through the proxy -- HTTP 404 rather than 101. Every live update in
the platform was affected. It was invisible because the development stack also
publishes the API directly on `127.0.0.1:8000`, which is the address a
developer's browser uses.
