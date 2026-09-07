# Monitoring, observability and system health (L37)

Built 2026-09-05.

    OBSERVE -> MEASURE -> DETECT -> REPORT

and never `OBSERVE -> MODIFY TRADING`.

---

## 1. The audit: four of the five layers already existed

Section 4 asks for logs, metrics, correlation and health. Three of those were
already there, and the fourth was the gap.

| Component | Where | Verdict |
|---|---|---|
| JSON structured logging with levels | `app/core/logging.py` (L02) | **KEEP**, reused |
| Request ids on every request, response and log line | `app/core/errors.py` (L02) | **KEEP** — correlation already existed |
| Correlation ids on every bus event | `app/core/events.py` (L07) | **KEEP** |
| Secret scrubbing | `app/core/audit.py` `_scrub` (L04) | **KEEP**, and a second scrubber for incident payloads |
| Three-state health checks, weighted aggregation | `app/core/health.py` (L02) | **KEEP + EXTEND** — see §3 |
| `/health`, `/health/live`, `/health/ready` | `app/main.py` (L02) | **KEEP**, untouched |
| Worker heartbeats and staleness detection | `app/workers/base.py` (L02) | **KEEP**, read |
| Hub counters (connections, delivered, dropped) | `app/realtime/hub.py` (L07) | **KEEP**, read |
| Broker adapter health and connection states | `app/brokers/` (L10) | **KEEP**, read |
| Market data provider statuses | `app/marketdata/service.py` (L08) | **KEEP**, read |
| RiskService status and kill switches | `app/risk/service.py` (L17) | **KEEP**, read |
| **Model** monitoring: drift, calibration, alerts | `app/monitoring/` (L29) | **KEEP**, consumed — never recomputed |
| `system_events` table: component, event type, level, correlation id, payload | `app/models/ops.py` (L05) | **KEEP + USE** — created at L05 and **never written to**. It is the incident table §43, §62, §63 and §76 ask for |
| Compose healthchecks for postgres, redis, api | `docker-compose.yml` | **KEEP** |
| Prometheus, OpenTelemetry, Grafana, Sentry, Datadog | — | **do not exist**. §3: reuse if present. None was |
| A metrics registry | — | **ADD** (§2 of this document explains why not Prometheus) |
| `app/observability/*`, `/v1/monitoring/*`, the dashboard | — | **ADD** |

**No second stack.** No new logging system, no second event bus, no second
health implementation. The collectors call L02's own check functions, so
`/health/ready` and the dashboard cannot disagree.

---

## 2. Why not Prometheus

Section 3 says to reuse an existing observability stack and to implement a
minimal extensible foundation if none exists. None existed.

`prometheus_client` would add a dependency, a global default registry and a
scrape endpoint to a process that has none of those. `app/observability/metrics.py`
gives the same three instrument types in about ninety lines with no dependency,
and the numbers are already served over the API the admin panel reads. A
deployment that wants a scrape endpoint writes an exposition formatter over
`Registry.snapshot()`; nothing else changes.

**Cardinality is enforced rather than requested** (§50). `trade_id`,
`order_id`, `request_id`, `correlation_id`, `user_id` and anything ending in
`_id` are **refused at record time** with `CardinalityRefused`. A metric that
reaches 200 label combinations stops creating series and counts the overflow.
An unbounded label set is a memory leak that looks like observability.

---

## 3. Five states, not two

Section 11 is explicit that a component must not be forced into
healthy/unhealthy, and the two extra states are the ones that earn their place:

| State | Means |
|---|---|
| `HEALTHY` | observed working |
| `DEGRADED` | observed working, but not properly |
| `UNHEALTHY` | observed not working |
| `UNKNOWN` | **not observed**. A probe failed, or the thing it reads does not exist. Never treated as healthy |
| `NOT_CONFIGURED` | deliberately absent. A platform with no SMTP server is correctly configured and does not send email |

`HealthStatus` (L02's three) is **not replaced** — `/health/ready` still derives
its HTTP code from it, and `from_health` is the one translation between the two
so they cannot drift.

`aggregate` is weighted, never averaged (§39): a critical component unhealthy
makes the platform unhealthy; anything else critical not healthy makes it
degraded; an important component degraded makes it degraded; **an optional
component never moves the overall state at all** — §12: an unconfigured
integration must not make a working platform report unhealthy.

---

## 4. What is monitored

Twenty or so components across seven layers, each read from the system that
owns it:

* **Infrastructure** — database and Redis (L02's own checks, plus a latency
  band the three-state check has no vocabulary for), the event bus (from the
  hub's own `bus_healthy`, not from a URL).
* **Workers** — one component per registered worker, from `WorkerRegistry`.
  `registered and not started` is `NOT_CONFIGURED`, not a fault.
* **Queues** — the notification delivery table, which is the one real queue this
  platform has. Depth, oldest pending row, failures. Named honestly: there is
  no Celery, no RQ and no dead-letter queue here, and inventing entries for
  queues that do not exist would be fake health.
* **Market data** — provider usability, and freshness measured from the newest
  stored `bar_time`. A platform that has ingested nothing reports `UNKNOWN`
  with *"no bar has ever been stored, so freshness is UNAVAILABLE rather than
  0"* — §21 in as many words, and exactly this machine's state.
* **Trading** — broker adapters (through the registry's own `health()`), the
  OMS (working orders and, separately, orders whose broker state could not be
  established), positions needing reconciliation, the RiskEngine's own status,
  and bot runs with no recent heartbeat.
* **AI** — active deployments and open model alerts, read from L28's registry
  and L29's tables. Drift is not recomputed here; §31 forbids it.
* **Notifications** — every channel's state from L34's registry, the consumer,
  and the delivery failure rate.
* **The monitor itself** (§67) — when it last collected, how many probes have
  failed. A monitor that cannot detect its own failure is decoration.

---

## 5. Incidents: hysteresis, then two destinations

**A state change is not an incident** (§45). `Tracker` requires two consecutive
observations before raising and two before clearing. On a fifteen-second
interval that is a real fault reported within thirty seconds and a 100ms blip
reported not at all. A test flaps a component six times and asserts zero
incidents.

The first sighting is a **baseline, never an incident**: a process that started
while something was already down should show it on the dashboard, not page
somebody as though it had just happened.

**A recovery is announced** (§43, §45), so the record reads *raised at 09:00,
cleared at 14:20* rather than leaving a gap — the rule
`app/monitoring/alerts.py` recorded for L29.

An incident becomes:

1. **a `system_events` row** — the table L05 created for exactly this
   ("operational events: connects, disconnects, reconciliations, halts") and
   which nothing had ever written to. §76 asks not to create tables when the
   infrastructure already stores these records. **No migration was needed.**
2. **one `SYSTEM_ALERT` on L07's bus**, which L34's consumer grades, routes
   through each recipient's preferences, and L35 delivers to Discord.

§46 says monitoring must not send Discord or email directly. `incidents.py`
imports neither, and a test greps for `EmailChannel`, `DiscordWebhookChannel`,
`NotificationService(` and `smtplib` across the package.

**No new event type was invented.** `SYSTEM_ALERT` has been catalogued since
L07; the incident vocabulary travels in its payload and in
`system_events.event_type`. A test asserts the package publishes exactly one
catalogued type.

**One L34 change**: `SYSTEM_ALERT`'s audience moved from `everyone` to
`operators`. L34 catalogued it as a user-facing platform notice; what L37
actually publishes onto it is infrastructure health, and §56 says ordinary
users should see only what their permissions cover — a read-only user cannot
act on a degraded event bus. `Audience.everyone` stays in the enum for a
genuine platform-wide notice.

---

## 6. Trading safety

Section 40 asks for a distinct verdict derived from the authoritative
components and forbids a UI developer inventing it. It is derived in
`service.trading_safety` from `database`, `risk_engine`, `broker`, `oms`,
`positions` and `market_data.freshness`, and it always carries its reasons.

| Verdict | When |
|---|---|
| `BLOCKED` | an authoritative component is UNHEALTHY |
| `UNKNOWN` | one could not be observed, or was not collected |
| `DEGRADED` | one is degraded |
| `SAFE` | all of them are healthy |

**It is a report, not a gate**, and the response says so. Nothing in the
platform reads it before trading: the RiskEngine vetoes, the OMS reconciles,
the adapter refuses. If it returned `SAFE` while the risk engine was blocking,
the platform still would not trade — which is the property that makes it safe
to compute a summary at all.

---

## 7. What monitoring cannot do

`app/observability/` imports no risk engine, no order manager, no sizing
service, no execution pipeline and no broker adapter class. Two tests hold it:
an AST walk over every module for a forbidden import, and a second walk
asserting the package never calls `submit_order`, `place_order`,
`cancel_order`, `close_position`, `connect`, `disconnect`, `reconnect`,
`restart`, `start`, `stop`, `engage_kill_switch` or `release_kill_switch`.

That second list is also **§47**: L37 detects, L38 recovers. This layer reports
`RECONCILIATION_REQUIRED` and worker staleness and performs neither recovery.
A monitor that restarts the thing it watches cannot tell you it failed to
restart it.

**A failing probe is one UNKNOWN component, not a failed pass.** Every collector
runs under a timeout and a catch; the failure becomes an `UNKNOWN` component
carrying an error category, and the rest of the pass continues. §66: monitoring
failure must not stop anything.

---

## 8. API and permissions

Two tiers, using the RBAC that already exists (§56, §59).

| Route | Gate |
|---|---|
| `GET /health`, `/health/live`, `/health/ready` | public — a status word and the mode, nothing else (§58) |
| `GET /v1/monitoring/summary` | signed in |
| `GET /v1/monitoring/contract` | signed in |
| `GET /v1/monitoring/components`, `/components/{name}` | `manage_system_settings` |
| `GET /v1/monitoring/events` | `manage_system_settings` |
| `GET /v1/monitoring/metrics` | `manage_system_settings` |
| `GET /v1/monitoring/thresholds` | `manage_system_settings` |
| `GET /v1/monitoring/uptime` | `manage_system_settings` |
| `POST /v1/monitoring/collect` | `manage_system_settings` |

§59 suggests `monitoring.read`; adding a permission means placing it in the
role table, and `manage_system_settings` already means *may see how this
deployment is put together*. No new permission was added.

L29's **model** monitoring stays at `/v1/ai/monitoring`. Two different
questions, and §31 says L37 consumes L29's results rather than recomputing
them; `ai.models` is one component here.

**Uptime is refused** (§64): this platform stores state *transitions*, not a
continuous sample series, so a percentage would describe the process rather
than the platform. The endpoint says "insufficient history" instead of
computing one.

---

## 9. Thresholds

Every number is in `app/observability/thresholds.py` (§44) and served by the
API. A threshold scattered across five collectors is five numbers that drift,
and the one an operator reads is never the one the alert used. A test greps the
collectors for a hard-coded bar.

**None of them is a trading limit**, and the response says so: they decide when
the platform *says* something. The RiskEngine decides what may be traded, and
this package cannot reach it.

Three are settable per deployment (`MONITORING_INTERVAL_SECONDS`,
`MARKET_DATA_STALE_SECONDS`, `QUEUE_BACKLOG_WARNING`); the rest are code.

---

## 10. Frontend

`/monitoring` was **extended, not duplicated** (§33). `ServiceHealth` has shown
per-service reachability since L03 and is kept; `SystemHealth` is the collected
view above it.

* Overall state, trading safety **with its reasons**, trading mode and live
  trading — always visible (§41).
* Component list grouped by layer, expandable to what was observed (§61). Five
  states rendered distinctly: `NOT_CONFIGURED` neutral, `UNKNOWN` amber.
* Recent incidents with level and component (§62).
* Detail needs the admin role and says so, rather than rendering an empty box;
  a 403 from the backend renders the same message.
* **The frontend derives nothing.** Every state and the safety verdict come
  from the backend, and an unreachable API renders an error rather than a green
  board (§74's "no fake health data").

---

## 11. Tests

`backend/tests/test_observability.py`, 50 tests.
`frontend/src/components/SystemHealth.test.tsx`, 14.

| Test | Section |
|---|---|
| `test_monitoring_cannot_reach_anything_that_trades` | 72, 81 |
| `test_no_route_restarts_reconnects_or_retries_anything` | 47 |
| `test_monitoring_publishes_no_new_event_type` | 81 |
| `test_monitoring_never_sends_a_notification_directly` | 46 |
| `test_a_brief_failure_produces_no_incident` | 45 |
| `test_a_sustained_failure_produces_one_incident_and_then_a_recovery` | 43, 45, 71 |
| `test_an_escalation_within_bad_is_still_reported` | 45 |
| `test_an_optional_component_never_makes_the_platform_unhealthy` | 12, 39 |
| `test_health_is_not_averaged` | 39 |
| `test_an_empty_platform_reports_what_is_absent_rather_than_healthy` | 65 |
| `test_stale_market_data_is_measured_not_assumed` | 20, 21 |
| `test_an_unresolved_order_is_reconciliation_required_not_failed` | 17 |
| `test_a_failing_probe_degrades_one_component_and_not_the_pass` | 66, 67 |
| `test_trading_safety_is_unknown_when_something_could_not_be_observed` | 40 |
| `test_an_identifier_may_not_be_a_metric_label` | 50, 70 |
| `test_a_metric_stops_growing_at_the_series_cap` | 50 |
| `test_the_public_health_endpoint_reveals_nothing_detailed` | 58, 73 |
| `test_detailed_monitoring_requires_authorization` | 56, 59, 73 |
| `test_no_monitoring_route_returns_a_secret` | 7, 73 |
| `test_uptime_is_refused_rather_than_invented` | 64 |
| `test_a_secret_in_a_fact_never_reaches_a_stored_incident` | 7, 53 |

**A real bug was found by writing them.** `Tracker.observe` updated its recorded
state on every healthy observation, which erased the "it was down" the recovery
threshold was counting towards — so a recovery could never have been announced.
Caught by `test_a_sustained_failure_produces_one_incident_and_then_a_recovery`
and fixed.

---

## 12. Known limitations

1. **No tracing spans.** §51 asks for spans over the execution path where
   tracing exists. There is no tracer, and adding OpenTelemetry is the
   competing stack §3 warns about. Correlation ids already thread request →
   event → log, which is the part that answers "where did it go".
2. **No frontend error reporting.** §37 asks to reuse an existing provider;
   there is none, and adding Sentry is a vendor decision, not a level.
3. **Latency is measured for the health probes, not for the trading path.**
   §9 asks for per-stage instrumentation of webhook → signal → … → journal.
   Most of those stages have no producer yet, and instrumenting a path nothing
   walks would be measuring nothing.
4. **`market_data.age` is the age of the newest stored bar**, not feed latency.
   §21 asks for source → ingestion → processing timestamps; only the first
   exists, so the other two report UNAVAILABLE by being absent.
5. **The metrics registry is per process.** Two API processes hold two
   registries. Correct for this deployment; a scrape endpoint and an aggregator
   are what a multi-process deployment would need.
6. **No log retention policy** (§48). Logs go to stdout and whatever collects
   them; `system_events` records transitions rather than heartbeats, so it
   grows with incidents rather than with time — but no period has been chosen.
7. **Uptime is not computed** (§64), deliberately.
