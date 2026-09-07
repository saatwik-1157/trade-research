# TEST_ENVIRONMENT.md

What the tests run against, and — more usefully — what they *cannot* run
against on this machine. Written at L40.

## The short version

```bash
cd backend
python -m pytest tests -q > run.txt 2>&1     # redirect; do not pipe
python -m ruff check app tests
python -m mypy app

cd frontend
npm test
npx tsc --noEmit
npm run lint
```

Two services make the difference between 49 modules passing and 45:

```bash
docker compose up -d postgres redis          # 127.0.0.1:5440 and 127.0.0.1:6390
```

## Trading mode

**Every test runs in paper mode and nothing can change that from a shell.**
`tests/conftest.py` clears `ENVIRONMENT`, `TRADING_MODE`, `LIVE_TRADING`,
`DATABASE_URL` and `REDIS_URL` from the environment before each test, and
builds `Settings` with `_env_file=None`. So a developer who exports
`LIVE_TRADING=true` in their shell — or leaves a `.env` lying about — changes
nothing about what the suite measures. That is deliberate: a safety default
that a stray environment variable can flip is not a default.

`test_integration.py::test_live_trading_is_off_by_default_and_every_gate_is_shut`
asserts it on every run rather than trusting the fixture.

## The database the tests use

**In-memory SQLite, per test, created from the models.** Not Postgres, and not
migrations:

```python
engine = create_async_engine(
    "sqlite+aiosqlite://", poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
```

`StaticPool` is load-bearing and has a consequence worth knowing. It makes
every session in one test share a single connection, which is what lets a test
write through the API and read back through the session factory. It also means
**two concurrent requests share one transaction**, so one request's rollback
rolls back the other's work. That is why the webhook concurrency test is
sequential; see `KNOWN_TEST_LIMITATIONS.md`.

Migrations are tested separately, against their own schema, in
`tests/test_migrations.py`. Nothing in the suite touches a persistent database,
and no test deletes trading history.

## The broker

`app/brokers/fake.py` — `FakeBroker(mode="paper")`. A deterministic venue that
is also the fault injector:

| Field | What it simulates |
|---|---|
| `fail_next` | the next order is rejected with the given detail |
| `unknown_next` | the venue's answer is lost — the state the platform must never retry |
| `disconnect_after` | the terminal drops after N orders |

**No test opens an MT5 terminal, and none can place a real order.** There is no
live adapter registered anywhere in the codebase, which is the second half of
the isolation and is asserted by
`test_integration.py::test_paper_mode_cannot_reach_a_live_adapter`.

## The four Redis-dependent modules

These need the Compose Redis on `127.0.0.1:6390` and fail without it:

| Module | Why |
|---|---|
| `tests/test_health.py` | readiness probes the bus |
| `tests/test_realtime.py` | the hub is the subject |
| `tests/test_errors.py` | `TestClient(app)` as a context manager runs the lifespan |
| `tests/test_cors.py` | same — identified at L40 |

The failure is always the same line and it is **environmental, and predates
L34**:

```
app/main.py:241: in lifespan
    await app.state.hub.start()
redis.exceptions.ConnectionError: Error 22 connecting to 127.0.0.1:6390
```

`Hub.start()` is L07 code. The rule that follows from it: **a test that does not
need the lifespan should not start it.** Use
`AsyncClient(transport=ASGITransport(app=app))`, which is what every module
outside those four does. `test_security.py`'s header tests were written with
`TestClient` first and moved for exactly this reason.

## Memory

The full backend suite has been killed twice by the OS on this machine for low
memory (2.6 GB free of 15.3). It is recorded here rather than worked around
because the workaround matters: **redirect, do not pipe.**

```bash
python -m pytest tests -q > run.txt 2>&1     # good
python -m pytest tests -q | tail -20         # buffers; a killed run looks like a hang
```

## CI

`.github/workflows/tests.yml` has two jobs. The research job runs six
no-network scripts across Python 3.10, 3.12 and 3.14. The `backend` job runs
lint, types and unit tests — **and deliberately nothing that needs Postgres,
Redis or MT5**, so readiness checks are proven honest against a closed port
rather than against a running service.

## Frontend

`vitest` with jsdom, 32 files. The backend is not running: `services.ts` is
mocked per test. That is a real limit and it is stated in
`KNOWN_TEST_LIMITATIONS.md` rather than described as end-to-end coverage.
