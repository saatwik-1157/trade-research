# INTEGRATION_TEST_PLAN.md

The L40 plan: what already existed, what was missing, and what was added.

## The finding that shaped the level

**Coverage per subsystem was good; coverage of the handoffs between them was
absent.** Forty-nine backend modules and thirty-two frontend files test the
parts. Almost nothing tested a signal travelling from the webhook gateway
through risk, sizing and the OMS to a venue in one database with one set of
foreign keys — which is the only place a whole class of defect lives.

Both real bugs L40 found are in that class. Neither is reachable from any
single subsystem's suite, and both had been present for many levels.

So L40 **added one file**, `backend/tests/test_integration.py`, and reused
everything else. No second framework, no second fixture set, no second fake
broker.

## Step 1 — the existing infrastructure, audited

| Layer | Where | Verdict |
|---|---|---|
| Runner, config | `backend/pyproject.toml` — `testpaths`, `asyncio_mode = "auto"` | KEEP |
| Fixtures | `backend/tests/conftest.py` (26 lines) — clears mode env vars, builds `Settings(_env_file=None)` | KEEP. It is small and it is the reason a developer's shell cannot change what the suite measures. |
| App fixture | repeated per module: in-memory SQLite + `StaticPool` + `create_app(settings, checks={}, engine=engine)` | KEEP, and reused verbatim by the new file |
| Fake venue | `app/brokers/fake.py` — also the fault injector (`fail_next`, `unknown_next`, `disconnect_after`) | KEEP. Already models every failure L40 needed. |
| Pipeline harness | `tests/test_execution.py::build()` | KEEP, mirrored as `test_integration.py::pipeline()` with the same `SPEC` |
| Frontend | vitest + jsdom, 32 files | KEEP |
| CI | `.github/workflows/tests.yml` — research job (3 Python versions) + backend job (lint, types, unit) | KEEP |
| Compose | `docker-compose.yml` — postgres 5440, redis 6390, all bound to 127.0.0.1 | KEEP |

## Step 2 — classification by subsystem

| Subsystem | Existing | Verdict | What L40 did |
|---|---|---|---|
| Auth / RBAC | `test_auth`, `test_auth_hardening` | COMPLETE | reused |
| Security (L39) | **none** | **MISSING** | added `test_security.py`, 44 tests |
| Webhook gateway | `test_webhooks` (62) | PARTIAL — no concurrency | added race coverage; **found 2 bugs** |
| Signals → execution | `test_execution` (51) | COMPLETE in isolation | added the DB-backed handoff |
| Risk | `test_risk` | COMPLETE | reused; added the ordering assertion |
| Sizing | `test_sizing` | COMPLETE | reused; added the venue-step check |
| OMS | `test_oms` | COMPLETE | reused |
| Brokers | `test_brokers` | PARTIAL — fake only | unchanged; recorded as a limitation |
| Positions | `test_positions` | COMPLETE | reused |
| Bots | `test_bots` | COMPLETE | reused |
| Journal | `test_trade_journal` | COMPLETE | reused |
| Portfolio | `test_portfolio` | COMPLETE | reused |
| Analytics | `test_analytics` | COMPLETE | reused |
| AI / models | `test_ai`, `test_model_registry`, `test_training`, `test_validation` | PARTIAL — stubs | reused; added the "AI cannot override risk" case |
| Trade review | `test_trade_review` | COMPLETE | reused |
| Notifications | `test_notifications` (74) | COMPLETE | added the isolation case |
| Discord | `test_discord` (54) | COMPLETE | reused |
| Admin | `test_admin` (40) | COMPLETE | **11 updated** for L39's step-up gate |
| Monitoring | `test_observability` (50) | COMPLETE | reused |
| Recovery | `test_recovery` (44) | COMPLETE | **3 updated**, 2 added |
| Realtime / WS | `test_realtime` | PARTIAL — needs Redis | added the connection-cap tests |
| Migrations | `test_migrations` | PARTIAL — forward only | recorded as a limitation |
| **Cross-subsystem** | **none** | **MISSING** | **added `test_integration.py`, 35 tests** |

DUPLICATED: none found. OBSOLETE: none found.

## Step 3 — the environment

`TRADING_MODE=paper`, `LIVE_TRADING=false`, enforced by `conftest.py` and
asserted on every run. Full detail in `TEST_ENVIRONMENT.md`.

## Step 4 — the pyramid, kept

L40 did **not** convert unit tests into E2E tests. The 35 new tests sit on top
of ~1,000 existing ones and assert only what the layers below cannot: joins,
ordering, and the consistency of records left behind. Where a property was
better asserted structurally than behaviourally — "the pipeline reads the clock
once", "both unique keys have a race handler" — it is, and the docstring says
that this is the weaker of the two options.

## Scenario map

Every scenario, and the test that carries it, is in `E2E_TEST_SCENARIOS.md`.
Results are in `INTEGRATION_TEST_REPORT.md`. What is *not* proven is in
`KNOWN_TEST_LIMITATIONS.md`, which is the document to read first.
