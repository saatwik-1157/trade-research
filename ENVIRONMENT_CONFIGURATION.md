# ENVIRONMENT_CONFIGURATION.md

The five environments, what separates them, and which settings decide whether
money can move.

---

## The environments

| | `ENVIRONMENT` | `TRADING_MODE` | `LIVE_TRADING` | Datastore | Broker |
|---|---|---|---|---|---|
| **development** | `development` | `paper` | `false` | Compose, loopback | none |
| **test** | `test` | `paper` | `false` | in-memory SQLite, per test | `FakeBroker` |
| **paper** | `production` | `paper` | `false` | its own PostgreSQL | none — the internal simulator |
| **demo** | `production` | `demo` | `false` | its own PostgreSQL | MT5 **demo** account |
| **production (live)** | `production` | `live` | `true` | its own PostgreSQL | a real account |

**The last row does not exist yet and cannot be reached by configuration
alone.** `TRADING_MODE=live` without `LIVE_TRADING=true` refuses to start, and
with it the platform still refuses: `live_execution_allowed` is true only when
every entry in `LIVE_GATES` is true, and **all 11 are false**. Setting both
variables produces a platform that starts, reports
`live_execution_blockers: [11 gates not built]`, and executes nothing.

Three independent things would have to change: the gates, the flags, and the
registration of a live adapter — which does not exist. That is the design, not
an oversight.

## What keeps the environments apart

**PAPER ≠ LIVE** is enforced in the execution path, not in configuration. The
mode is on the signal *and* on the order-manager registration, and the pipeline
compares them: a signal claiming `live` cannot execute through a manager
registered for `paper`, whatever the caller intended. Asserted by
`test_integration.py::test_paper_mode_cannot_reach_a_live_adapter`.

**DEMO ≠ LIVE** is enforced below the platform, in `tools/mt5_paper.assert_demo`:
the terminal's `trade_mode` must be 0, and CONTEST, REAL and unknown values all
abort before an order is built.

**TEST ≠ PRODUCTION** is enforced by `tests/conftest.py`, which deletes
`ENVIRONMENT`, `TRADING_MODE`, `LIVE_TRADING`, `DATABASE_URL` and `REDIS_URL`
from the environment before every test and builds `Settings(_env_file=None)`. A
developer who exports `LIVE_TRADING=true`, or leaves a `.env` lying about,
changes nothing about what the suite measures — and no test can reach a real
database because the URL is cleared.

## The settings that refuse to start

Fail-closed, and each fails at startup rather than in traffic:

| Setting | Refused when | Why |
|---|---|---|
| `TRADING_MODE` / `LIVE_TRADING` | mode is `live` and the flag is false | ambiguity about whether real money is in play is resolved by refusing, not by picking a reading |
| `CORS_ORIGINS` | contains `*` or `null` | with credentials, a wildcard makes any website an authenticated caller using a visitor's cookie |
| `CORS_ORIGINS` | an entry carries a path, query or fragment | a browser compares origins, so such an entry matches **nothing** — and does so silently, with the symptom appearing long after the deploy |
| production overlay | any required secret is unset | `${VAR:?...}` fails the deploy rather than falling back to a development value |

## Configuration by category

**Application** — behaviour that is the same everywhere: log level, session TTL,
worker intervals, thresholds. In `settings.py` with defaults.

**Environment** — differs per deployment and is not secret: `ENVIRONMENT`,
`DATABASE_URL` (host and port), `REDIS_URL`, `CORS_ORIGINS`, `APP_BASE_URL`,
`WORKERS_ENABLED`, `RATE_LIMIT_SHARED`.

**Secrets** — never committed, never defaulted in production, never logged:
`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `TV_WEBHOOK_SECRET`, `SMTP_PASSWORD`,
`DISCORD_WEBHOOK_URL`, `BOOTSTRAP_ADMIN_PASSWORD`. Sourced from the host
environment or the CI/CD secret store. **No custom secret store was invented.**

**Deployment** — which containers, what limits, what is published. In the
compose files.

**Feature flags** — `LIVE_GATES` in `settings.py`. Deliberately **code, not
configuration**: each entry is a claim that live execution may rely on a
mechanism, and flipping one is a reviewed change with a test behind it, not an
environment variable somebody can set at 2am. L36 refused to build a
feature-flag table for this reason.

## Where secrets are NOT

Audited at L39 and re-checked at L41:

* **Not in git.** No `.env` file exists; `git ls-files` shows no tracked
  `.env`, `.pem`, `.key`, credential or secret file.
* **Not in the frontend bundle.** Only `NEXT_PUBLIC_*` reaches the browser, and
  the only one set is `NEXT_PUBLIC_API_URL=/api`. The frontend receives no
  broker password, no MT5 password and no server secret — it has no code path
  that reads one.
* **Not in an image layer.** The Dockerfiles copy source and requirements;
  secrets arrive as runtime environment variables. Verified by running the
  built image and reading its environment.
* **Not in logs.** `app/core/logging.py` scrubs at any depth; the webhook
  gateway strips its secret from keys *and* string values before logging;
  security event payloads are an allow-list.
* **Not in an API response.** `settings.public_summary()` omits every secret,
  and no route returns one — asserted by
  `test_admin.py::test_no_admin_route_returns_a_secret` and
  `test_security.py::test_the_posture_reports_no_configured_secret_value`.
* **Not in a health endpoint.** `/health` returns mode and release identity.
  `app/core/release.py` reads a **fixed list** of variables — there is no
  "and any other `RELEASE_*`" passthrough, which is how a version endpoint
  usually ends up reporting a connection string.
* **Not in an error message.** The L02 error envelope carries a code, a
  message and a request id; no stack trace and no URL.

## CI secrets

The workflow needs none. Lint, types, tests and the image build all run with no
credential: the tests use SQLite and `FakeBroker`, and the image build takes
only `GIT_COMMIT`, `BUILD_TIME` and `RELEASE_VERSION` — none of which is
secret.

**Production secrets are therefore not available to CI at all**, which is the
strongest version of "do not expose production secrets to tests": there is
nothing to expose. If a deploy job is added later it must use its own
credentials, never the production ones.
