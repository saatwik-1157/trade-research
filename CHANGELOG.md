# CHANGELOG.md

Notable changes, newest first. Levels 00–17 predate this file; their record is
`PROJECT_PROGRESS.md`, which keeps the full narrative including what was
measured and what was refused. This file is the summary view.

Format: one section per level or dated change. Added / Changed / Fixed /
Refused. "Refused" is a first-class category here — a thing the platform
declines to do is a feature of it, and losing that record is how a refusal
quietly becomes a default.

---

## Level 40 — Integration testing (2026-09-05)

### Added
- `backend/tests/test_integration.py` (35): the cross-subsystem suite. Webhook
  to venue in one database with the correlation chain asserted at every link;
  duplicate handling at both the gateway and the pipeline; rejection at six
  layers, each asserting the venue received nothing; the mandatory unknown-order
  case; paper/live isolation; data consistency; UTC storage; no future leakage.
- `INTEGRATION_TEST_PLAN.md`, `INTEGRATION_TEST_REPORT.md`,
  `E2E_TEST_SCENARIOS.md`, `TEST_ENVIRONMENT.md`, `KNOWN_TEST_LIMITATIONS.md`.

### Fixed
- **Migrations 0024 and 0025 could not be applied to PostgreSQL.** `op.f()` was
  missing on four already-final constraint names, so the naming convention
  prefixed them twice. SQLite's `batch_alter_table` hides it; PostgreSQL
  refuses. The upgrade path from 0023 to head was broken on the only engine the
  platform is deployed on. Found by running `test_migrations.py` against
  Postgres for the first time -- those three tests had been skipping since L02.
- A concurrently re-delivered webhook answered **500** instead of `duplicate`.
  The `webhook_events.idempotency_key` insert was the one unguarded flush on the
  path. TradingView retries on 5xx, so the condition the gateway exists to
  absorb was answered in the way most likely to produce another delivery.
- `StaleDataError` on the following flush, surfaced only once the above stopped
  raising: the rollback left both rows in the identity map as persistent.
  Fixed with a savepoint and an explicit expunge.
- A webhook took **2.06 seconds** when Redis was down. The bus client inherited
  redis-py's retry defaults. Bounded to 0.5s connect / 2.0s operation; measured
  at **0.53s per request** afterwards.
- The L39 security-headers middleware doubled WebSocket flakiness (8 flaky
  failures against 2). Rewritten from `BaseHTTPMiddleware` to pure ASGI, which
  adds no task group and returns immediately for non-HTTP scopes.
- L39's step-up gate ran before the deterministic refusals, burning a
  single-use grant on actions that could never succeed.
- The L39 CORS validator accepted an origin carrying a path, which matches
  nothing and does so silently.

### Changed
- 14 pre-existing tests updated to satisfy L39's step-up gate by obtaining a
  real grant. **None was weakened, skipped or deleted.**

### Refused
- **A true-concurrency webhook test.** SQLite with `StaticPool` shares one
  connection, so a concurrent version fails for a harness reason that does not
  exist on Postgres. Made sequential, with the race asserted against the source
  and the limitation written into `KNOWN_TEST_LIMITATIONS.md` with the command
  to run it properly.
- **A coverage percentage.** None was measured, so none is claimed.
- **Fixing the 16 pre-existing `mypy` errors and 12 unformatted files.** All are
  in L34-L38 code this level did not touch. CI gates on both, so **CI is red
  and was red before L39** -- reported rather than quietly absorbed.

---

## Level 39 — Security (2026-09-05)

### Added
- `backend/app/security/`: response headers, step-up re-authentication, a
  security event vocabulary, an announcer, the posture, and the enforcement
  helper. `app/api/v1/security.py` with four routes, one of them a write.
- Security headers on **every** response including errors -- there were none at
  all. CSP `default-src 'none'`, nosniff, `DENY`, `no-referrer`, a
  Permissions-Policy denying thirteen features, COOP/CORP, `no-store`. HSTS in
  production only.
- Step-up re-authentication on the four dangerous action classes: user access,
  session revocation, safe-mode exit, kill-switch release. Scoped, bound to the
  subject and the session, 300 seconds, **single-use**, five-attempt lockout,
  rate limited.
- `SECURITY_ALERT` (system scope, a count and a class, never a subject) and
  `ACCOUNT_SECURITY_ALERT` (user scope), filling L34's empty
  `Category.security`.
- A per-user WebSocket connection cap (`WS_MAX_CONNECTIONS_PER_USER`, default
  8). Nothing had capped connections per account.
- A security posture as one more component of L37's health stack.
- `backend/tests/test_security.py` (44).

### Changed
- CORS: a wildcard origin with credentials is now **refused at startup**, and
  `allow_headers=["*"]` replaced with the seven the frontend sends.

### Refused
- **Calling step-up "MFA".** It asks for the same password again. TOTP needs
  enrolment, recovery codes and a lost-device path; a half-built second factor
  is a control bypassed the first time it is inconvenient.
- **A security score.** A number invites "how do we get to ten", and the
  cheapest answers move the number rather than reduce risk.
- **A second dashboard, a second audit log, a second auth system.** The posture
  is a `ComponentHealth`; the events are `audit_logs`; authentication is L04's.
- **Any route that changes a security control.** Configuration is read at
  startup; changing one is a deployment.

---

## Level 38 — Recovery, resilience and reconciliation

FAILURE, SAFE STATE, RECOVERY, RECONCILIATION, VALIDATION, SAFE RESUME -- and
never DISCONNECTED to EXECUTING.

### Added
- `app/recovery/` -- `contract.py` (the recovery ladder, safe-mode reasons,
  step results), `safe_mode.py` (the latch, its reasons and the guard),
  `reconciliation.py` (the existing reconcilers, called in a defined order),
  `bots.py` (the `SafetyCheck` L22 left a seat for), `manager.py` (the startup
  sequence, the latch decision and the audit trail).
- **The startup sequence**, run in the lifespan before anything consumes a
  signal: 14 steps, ending in the operational mode this process will run in.
  It never raises and never enables live trading.
- **Safe mode**: a latch that closes automatically on a condition the sequence
  found, names that condition as its reason, blocks new orders, automated
  execution and bot recovery, and opens only for an authorized person after the
  sequence has been re-run and the condition has actually cleared.
- Eight routes on `/v1/recovery`: status, sequence, reconciliation, events,
  reconcile, safe-mode enter and exit, contract.
- `RecoveryPanel` on `/monitoring`, between L37's health board and L03's
  service list.
- `RECOVERY_ARCHITECTURE.md` and `BACKUP_RESTORE.md`.
- `RECOVERY_STARTUP_CHECKS`.
- 44 backend tests, 13 frontend tests.

### Changed
- `ExecutionPipeline` takes an optional safe-mode latch and checks it at gate
  zero, in front of every existing gate and never instead of one.
- `POST /v1/orders` refuses with a 409 naming the condition while the latch is
  shut.
- `BotSupervisorWorker` is given a real `SafetyCheck`. L22's default refused
  everything; this does not lower that bar -- every branch is a refusal, and
  L22's own gates still run afterwards.
- `Outcome.safe_mode` was added, and added to `NO_ORDER`.
- `docker-compose.yml`: `restart: unless-stopped` on all five services.

### Fixed
- Adding `Outcome.safe_mode` without adding it to `NO_ORDER` made
  `created_order` report True for a refusal that sent nothing -- the execution
  log would have claimed an order was created when the pass stopped at gate
  zero. Found by the test that exercises the gate.

### Refused
- Rebuilding any reconciler. Four already existed, each written by the level
  that owns the state it compares, and every one reports rather than repairs.
  `app/recovery/reconciliation.py` contains no comparison logic.
- Repairing anything. An unexpected venue position is not closed and a missing
  one is not re-opened: the first may be a trade somebody made by hand, and the
  second is how a crash between send and log becomes two positions.
- Retrying an unknown order. It is settled by asking the venue, through the
  OMS's own route, which is not wrapped here.
- Automatic reconnection. A disconnected adapter is detected, latches safe
  mode, and waits for a person; a reconnect loop belongs with a live adapter,
  which does not exist.
- Migrating on boot. The sequence checks the schema and refuses; N processes
  racing to migrate is a worse failure than one that will not start.
- A backup script this project cannot test. `BACKUP_RESTORE.md` documents what
  must be backed up, the restore order, and an honest RPO and RTO -- which on
  this deployment are "since the last manual dump" and "however long a person
  takes".
- Releasing safe mode by unsetting a flag. The exit route re-runs the sequence
  and clears only the latches whose condition has gone.
- Persisting the latch. A stored flag set by a process that has since died is a
  platform that will not trade for a reason nobody can find.

---

## Level 37 — Monitoring, observability and system health

OBSERVE, MEASURE, DETECT, REPORT -- and never OBSERVE, MODIFY TRADING.

### Added
- `app/observability/` -- `contract.py` (five component states, layers,
  criticality, trading safety, error categories), `thresholds.py` (every number
  in one place), `metrics.py` (counters, gauges and histograms with enforced
  cardinality), `collectors.py` (per-component probes that read the owning
  system), `incidents.py` (hysteresis, `system_events` rows, one SYSTEM_ALERT),
  `service.py` (one collection pass) and `worker.py`.
- Nine routes on `/v1/monitoring`: summary, contract, components,
  components/{name}, events, metrics, thresholds, uptime and a collect trigger.
- `SystemHealth` on `/monitoring`, above the `ServiceHealth` panel that has been
  there since L03.
- `MONITORING_ENABLED`, `MONITORING_INTERVAL_SECONDS`,
  `MARKET_DATA_STALE_SECONDS`, `QUEUE_BACKLOG_WARNING`.
- `LEVEL_37_MONITORING.md`.
- 50 backend tests, 14 frontend tests.

### Changed
- `SYSTEM_ALERT`'s notification audience moved from `everyone` to `operators`.
  L34 catalogued it as a user-facing platform notice; what L37 publishes onto it
  is infrastructure health, and a read-only user cannot act on a degraded event
  bus.
- `/monitoring` keeps its `partial` status: the collected view is real, the
  detail needs the admin role, and several components report NOT_CONFIGURED
  because this deployment genuinely has no broker adapter or feed.

### Fixed
- `Tracker.observe` updated its recorded state on every healthy observation,
  which erased the "it was down" that the recovery threshold counts towards --
  so a recovery could never have been announced. Found by writing the test that
  asserts one incident and then one recovery.

### Refused
- A second monitoring stack. No Prometheus, no OpenTelemetry, no Sentry: the
  audit found none to reuse, and ninety lines of registry with no dependency
  serves the same three instrument types over the API the panel already reads.
- A second health implementation. The collectors call L02's own check
  functions, so `/health/ready` and the dashboard cannot disagree.
- A new incident table. `system_events` was created at L05 for exactly this and
  had never been written to. No migration was needed.
- A new event type. `SYSTEM_ALERT` has been catalogued since L07; the incident
  vocabulary travels in its payload.
- Sending email or Discord from monitoring. It publishes one event and stops
  thinking about it; a test greps the package for a provider.
- Restarting, reconnecting or retrying anything. L37 detects, L38 recovers, and
  a test asserts the package never calls those verbs.
- An uptime percentage. This platform stores transitions, not samples, so it
  says "insufficient history" rather than computing one.
- A new permission. `manage_system_settings` already means "may see how this
  deployment is put together".
- An identifier as a metric label. Refused at record time, not documented as
  discouraged.

---

## Level 36 — Admin panel and system control centre

Administrative visibility and user access -- and deliberately nothing else,
because every trading control already has an authoritative home.

### Added
- `app/admin/service.py` -- the dashboard aggregation, user search and detail,
  session listing and revocation, activation, the RBAC table, the integration
  status and the configuration report.
- Eleven routes on `app/api/v1/admin.py`: dashboard, permissions, integrations,
  configuration, contract, user search, user detail, sessions, activate,
  deactivate, revoke-sessions, plus audit action counts.
- `audit.record_admin`, which requires the reason in its signature and writes
  before/after state through the same scrubber as everything else.
- Six audit action names, so the trail says what happened rather than
  "admin_action" with the detail in a blob.
- `AdminPanel` on `/admin`: environment banner, overview, users, audit trail,
  integrations, roles and configuration.
- Migration `0025_admin_audit`.
- `LEVEL_36_ADMIN.md`.
- 40 backend tests, 19 frontend tests.

### Changed
- `audit_logs` gains an `environment` column and three indexes. Extended, not
  shadowed by a second table: section 31 asks for `admin_audit_logs` and section
  51 says not to duplicate, and this table has been the admin trail since L04.
- `/admin` is `partial` rather than `planned` in `nav.ts`.

### Refused
- A second door to any trading control. Pausing a bot, promoting a model,
  reconciling a broker and changing a risk limit stay on the surfaces that own
  them; the panel links to them and a test asserts the admin modules import
  none of those services.
- A one-click "enable live trading", with or without safeguards. `LIVE_GATES`
  is code; there is no route that sets one, and a test greps for the
  assignment.
- A feature-flag table. This platform's flags are LIVE_GATES (code, not
  configuration) and settings (environment, not database). A database-backed
  flag is a way to change behaviour without a deployment or a review.
- Any field that edits configuration from a browser. A frontend test asserts
  the configuration tab renders zero input elements.
- A `severity` column on the audit trail. An audit row records that something
  happened; grading it is a monitoring judgement, and a column that is
  uniformly "info" is not a filter anybody can trust.
- Bulk dangerous actions (section 44) and impersonation (section 56).
- A yes/no confirmation. Dangerous actions take a reason of real length and the
  subject's own email typed back.
- Deleting anything on deactivation. Access is removed; trades, journal,
  analytics, strategies, accounts and bots are kept, and an open position is
  reported loudly rather than closed.

---

## Level 35 — Discord integration

A webhook, an embed, and nothing else. Discord is a destination for
notifications the platform already created.

### Added
- `app/notifications/channels/discord.py` — `DiscordWebhookChannel`,
  `build_embed`, `verification_embed`, webhook-shape validation and failure
  classification. It replaces the body of `discord_channel_from()`; nothing
  around it changed shape.
- `GET /v1/notifications/discord` — state, transport, scope, counters, how to
  enable, how to rotate. Never the URL.
- `POST /v1/notifications/discord/test` — administrators only, rate limited,
  CSRF-protected, audited.
- `DiscordSettings` on `/community`.
- `DISCORD_ENABLED`, `DISCORD_WEBHOOK_URL`, `DISCORD_USERNAME`,
  `DISCORD_TIMEOUT_SECONDS`, with `.env.example` placeholders and Compose
  pass-through from the host environment.
- `LEVEL_35_DISCORD.md`.
- 54 backend tests, 10 frontend tests.

### Changed
- `/community` is `partial` rather than `planned` in `nav.ts`.
- `docker-compose.yml` passes the L34 and L35 secrets through from the host
  rather than carrying them.

### Refused
- A Discord bot. A webhook covers outbound notification with no gateway
  connection and no token with guild-wide reach (section 4).
- Slash commands. `/portfolio` would be a read of private trading data
  authorized by a Discord identity this platform has never seen (section 35).
- A new HTTP client dependency. `urllib.request` in a thread, the argument
  `email.py` already makes for `smtplib`.
- Per-user webhooks. That is a bearer credential per user, which needs
  encryption at rest and a key decision nobody has made; storing it in plaintext
  because the feature was convenient would be worse than not having it.
- Returning the webhook URL redacted. A redacted secret is still a statement
  about the secret's shape.
- A field in the browser to type a webhook into. A test asserts the page renders
  no input elements at all.
- Retrying a 404. The webhook was deleted; retrying it never drains the queue.
- Guessing at a backoff when Discord sent `retry_after`.
- A synthetic TRADE_CLOSED to test the wiring. The test message says it is a
  test and names no trade, symbol, price or account.
- A new migration, delivery status, preference column or event type. L34's
  schema already had room, which is what building the seat was for.

---

## Level 34 — Notifications and alerting

One event bus, one notification service, one record per person per event — and a
message that never contains a figure the platform did not record.

### Added
- `app/notifications/contract.py` — the vocabulary: five severities, ten
  categories, three channels, six delivery statuses, and the envelope an adapter
  is handed. It carries no credential, so nothing an adapter logs can leak one.
- `app/notifications/catalogue.py` — one `Rule` per event type. Every type in it
  already exists in `app/realtime/catalogue.py`; L34 invents no event. Fourteen
  more are listed in `NOT_NOTIFIED` **with the reason**.
- `app/notifications/templates.py` — the message. A figure the event did not
  carry does not appear, as a zero or otherwise.
- `app/notifications/dedup.py` — the event key (same event → one notification)
  and the condition key (same problem → one notification per cooldown). Severity
  is in the condition key, so a WARNING becoming a CRITICAL is never suppressed.
- `app/notifications/preferences.py` — defaults, resolution, and the floor a
  user cannot go below.
- `app/notifications/service.py` — the object that ties those together.
- `app/notifications/worker.py` — `NotificationConsumer` (a second subscriber on
  the one bus, never a second bus) and `NotificationDeliveryWorker`.
- `app/notifications/channels/` — `InAppChannel`, `EmailChannel` with
  `EmailProvider`/`SMTPEmailProvider` behind it, and the Discord **seat**.
- `app/api/v1/notifications.py` — 9 routes. None of them creates a notification.
- `NotificationBell` in the top bar and `NotificationCenter` on `/alerts`.
- Migration `0024_notifications`: one table altered, two created, nothing
  dropped.
- `LEVEL_34_NOTIFICATIONS.md`.
- 74 backend tests, 23 frontend tests.

### Changed
- `NOTIFICATION_CREATED` joins `PRODUCED_NOW`. The type and its `user` scope
  have been catalogued since L07 waiting for exactly this producer; no new type,
  no new scope, no new authorization rule.
- `notifications` gains nine columns and keeps every existing one, including
  `channel`, which L29 has written since it was added.
- `ModelMonitor._persist` now writes L34's severity vocabulary and sets
  `category`, imported from `app.notifications.contract` rather than spelled out
  again — one vocabulary in the column instead of two.
- `/notifications` is no longer a 501 in `app/api/pending.py`.
- `/alerts` is `partial` rather than `planned` in `nav.ts`: every control is
  real, and most categories have no producer yet.

### Fixed
- **`app/auth/reset.py`'s delivery port, open since L04.** Its docstring has
  said delivery *"needs the notification engine (level 34)"* for thirty levels.
  `NotificationResetDelivery` implements it. With no SMTP host configured it
  refuses exactly as `UnconfiguredDelivery` did.

### Refused
- A second event bus. The consumer subscribes to L07's.
- A `POST` that creates a notification. A test asserts none exists: a fabricated
  trading alert is unrepresentable rather than merely discouraged.
- Switching off in-app delivery of ERROR and CRITICAL. Refused with the reason
  rather than clamped, in the API *and* as a CHECK constraint.
- Defaulting an unresolved trading environment to `paper`. It is stored and
  shown as `unknown`.
- Saying "order failed" for `ORDER_UNKNOWN`. The message says the state could
  not be established and is being reconciled.
- Claiming the RiskEngine blocked trading unless its own payload said so.
- Deleting L29's recipient-less `notifications` rows to tidy up the table.
- Retrying a refused SMTP authentication. It is a configuration error, not
  weather.
- A retention policy invented on the spot. Documented, not implemented.

---

## Level 33 — AI trade review and post-trade intelligence

What happened on one completed trade, assessed from what was recorded — and
honest about which half of the record it is allowed to use.

### Added
- `app/review/contract.py` — `Rating`, `Compliance`, `Outcome`, `ReviewStatus`,
  and `Evidence` with its three kinds. `observed()` requires a source.
- `app/review/context.py` — **`DecisionContext` and `OutcomeContext`**, the wall
  the level turns on.
- `app/review/checks.py` — the deterministic assessors. Four take the decision
  context and nothing else.
- `app/review/provider.py` — `ReviewProvider` (a one-method Protocol) and
  `DeterministicProvider` (the default, and not a language model).
- `app/review/validation.py` — structural and referential checks, plus the
  derived confidence.
- `app/review/service.py` — eligibility, context assembly, assessment,
  validation, storage, versioning and the bounded sweep.
- `app/review/patterns.py` — aggregate shapes read from L32 analytics, with
  sample sizes on every one.
- `app/api/v1/reviews.py` — 8 routes.
- `TradeReview` on the trade detail page, and three catalogue event types for
  L34.
- Migration `0023_trade_reviews`.
- `LEVEL_33_AI_TRADE_REVIEW.md`.
- 57 backend tests, 10 frontend tests.

### Changed
- The trade detail panel now renders the review beside the timeline.

### Fixed
- A safety test that caught `db.rollback()` and `WorkerRegistry.register`.
  Narrowed to the registry's specific verbs plus an import check — a rule that
  flags unrelated code is a rule people learn to ignore.

### Refused
- **An LLM integration.** No provider exists and no key is configured; choosing a
  vendor, a cost model and a data-egress policy is not this level's decision.
- **Letting a provider return a rating, a price or a model version.** There is
  nowhere on `Narration` to put one.
- **Using post-entry information to judge a decision.** Four assessors are not
  given it.
- **Labelling a trade non-compliant because its strategy record is missing**, or
  poor because its risk record is. UNKNOWN, with the missing field named.
- **A self-reported confidence.** Derived from completeness and coverage.
- **Storing an unvalidated review.** An invented price, symbol or model version
  fails it; it is never repaired.
- **Reviewing a close the venue never confirmed.**
- **Overwriting a review.** A regeneration adds a version.
- **Writing into `journal_entries.ai_review`.** That is a user's note column.
- **Estimating MAE, MFE or market context.** Null, with the reason.
- **Claiming a pattern from a small sample**, or acting on one at all.
- **A scheduled worker.** The sweep is a route; starting a worker is an operator
  action.

---

## Level 32 — Analytics and performance analytics

Performance over completed trades, from the systems that already own them.

### Added
- `app/analytics/metrics.py` — **the** metric definitions. Win rate, profit
  factor, expectancy, payoff ratio, deviation, downside deviation, Sharpe and
  Sortino per trade, the t-statistic, streaks, percentiles and distributions,
  with `Unit` as a type and `INSUFFICIENT_DATA` as a value.
- `app/analytics/equity.py` — the realized and account curves, kept apart, and
  drawdown periods with open ones left open.
- `app/analytics/windows.py` — the date presets, the buckets, and the timezone
  policy, served rather than restated.
- `app/analytics/service.py` — `Scope`, and the account, trade, breakdown,
  time, execution, exposure and comparison domains.
- `app/api/v1/analytics.py` — 11 GET routes, replacing the `/analytics/summary`
  501 stub.
- `AnalyticsDashboard` and a real `/analytics` page, with an inline-SVG equity
  curve and no charting library.
- `LEVEL_32_ANALYTICS.md`.
- 91 backend tests, 10 frontend tests.

### Changed
- **`backtest/runner.compute_metrics` and `training/metrics.economic` now
  delegate to the shared definitions.** The audit found three copies of one
  formula; this is the extraction. Neither output shape changed — including
  `runner.py`'s `NOT_AVAILABLE` sentinel, which the backtest API has served since
  L14 — and 98 existing tests pass unchanged.
- `runner.MIN_TRADES_FOR_RATIO` is now imported rather than redeclared, so the
  backtest and the live journal answer "is this ratio worth reading?" the same
  way.
- `/analytics` nav entry from `planned` to `partial`.

### Refused
- **Pooling two units.** `Series.concat` raises rather than adding points to
  currency — the metals-points lesson as a type instead of a comment.
- **Assigning R to a trade that never recorded its planned risk.** It is excluded
  from the R series; deriving R from the realised loss makes every loser exactly
  −1R by construction.
- **A profit factor of infinity.** A run of winners has an undefined one, and a
  large number reads as a strong edge from a sample that never fell.
- **A Sharpe from fewer than 20 trades**, a median from fewer than 5, a
  deviation from one, and a ratio from zero variance.
- **A recovery factor with no drawdown.** Dividing by zero is undefined, not
  impressive.
- **Merging two environments into one curve.** `combine()` exists only to raise.
- **Reading an account balance change as profit.** No cash-movement table exists,
  so the realized curve is the performance figure and the account curve says why.
- **Reporting an open drawdown as recovered** at the last point in the series.
- **Inventing latency from a missing timestamp**, or summing per-fill slippage in
  points into an account-currency cost total.
- **Trimming an outlier.** The largest figures this repository produced were the
  unit errors, and a winsorised distribution would have hidden them.
- **Claiming a cause from a comparison.** Both sample sizes and the observational
  reading are in the payload.
- **Annualising a ratio**, and **a non-zero risk-free rate** — both stated
  assumptions rather than silent ones.
- **Caching, materialised views and a precomputed daily table.** Measured: 252
  rows, sub-10ms summaries, and a cache adds an invalidation path that could
  serve a stale equity figure as current. `calculation_ms` is on every summary so
  the decision is revisitable with evidence.
- **A second backtest simulator, a second portfolio state, and a second trade
  history.**

---

## Level 31 — Trade journal and trade lifecycle history

What happened, why, and what the system knew at the time.

### Added
- `app/journal/lifecycle.py` — `TradeStatus`, the exit vocabulary (L21's eight
  plus `manual_exit`, `broker_exit`, `unknown`), and `Holding` for exact
  durations.
- `app/journal/closes.py` — the weighted exit and the realized figure, with the
  double-count made impossible rather than discouraged.
- `app/journal/timeline.py` — `derive`, which assembles the chronology from nine
  already-timestamped tables and stores nothing.
- `app/journal/quality.py` — ten checks that detect and flag, and never correct.
- `app/journal/context.py` — the strategy, AI, risk, sizing, execution and cost
  blocks, each read from the row written at the time.
- `app/journal/service.py` — `record_close`, `record_pending` (the sweep),
  `mark_reconciliation_required`, `timeline_for`, `context_for`, `closes_for`.
- `app/services/journal.py` — statistics and CSV export.
- Four sub-routes and two collection routes on `/v1/trades`, plus twelve filters
  and identifier search.
- Three account-scoped event types: `TRADE_RECORDED`, `TRADE_UPDATED`,
  `TRADE_RECONCILIATION_REQUIRED`.
- `TradeJournal` and `TradeDetail` on a real `/journal` page.
- `TRADE_JOURNAL_ARCHITECTURE.md`.
- Migration `0022_trade_journal`: 13 nullable columns, one CHECK, seven foreign
  keys, three indexes. No new table.
- 62 backend tests, 9 frontend tests.

### Changed
- `Trade` gained status, both account columns, five attribution references, the
  exit reason, requested-versus-actual entry, fees, currency and a data-quality
  block. Every one nullable; the 252 imported rows are untouched.
- `app/paper/service.py` now links the position, the account, the strategy
  version, the exit reason and the currency onto the trade it writes — and
  records the ambiguity rather than guessing when one simulated close cannot be
  matched to one open row.
- `list_trades` gained account, strategy, bot, exit reason, status, AI model,
  model version, AI mode and identifier search.
- `/journal` nav entry from `planned` to `partial`.

### Fixed
- **The live path wrote no trade at all.** `PositionManager._book` books
  `realized_pnl` onto the position and nothing ever created a `trades` row — so a
  demo or live position closing left the journal untouched, and nothing noticed,
  because an empty journal looks exactly like an account that has not traded.
  `record_pending` closes it.
- **`op.drop_constraint("ck_trades_status", …)` produced
  `ck_trades_ck_trades_status`.** Fourth occurrence of the pre-prefixed-name
  trap, and the first on a DROP rather than a CREATE. Only the round-trip
  downgrade against a real PostgreSQL reaches that statement.
- **`/trades/{trade_id}` shadowed `/trades/statistics`.** Starlette matches in
  registration order, so the parameterised route captured `statistics` as a trade
  id and answered 404.

### Refused
- **A second trade history.** No new table; `trades` was extended.
- **A `trade_events` table.** The timeline is derived, so it cannot fall behind
  or disagree with its sources.
- **Recomputing realized P&L from the weighted exit.** It is summed from the
  closes; booking both is §14's double-count.
- **`cancelled` and `rejected` as trade statuses.** A cancelled order never
  opened a position.
- **Finalising a close the venue did not confirm.** `unknown` stays `unknown`; a
  mismatch becomes `reconciliation_required`.
- **A row with substituted values.** No confirmed close means no trade, with the
  reason recorded.
- **Correcting a data-quality finding.** Flagged, never repaired: a silently
  repaired record looks clean and is wrong.
- **Serving a webhook payload.** It can carry a shared secret.
- **Sharpe, Sortino and significance.** L32's, and `CLAUDE.md` records why a live
  t-statistic on a small sample is the most misleading number this project has
  produced.
- **MAE/MFE, entry slippage in points, and currency conversion.** Each needs data
  this deployment does not have, and an approximation would be indistinguishable
  from a measurement.

---

## Level 30 — Portfolio management and exposure

What is held, what it is worth, and how sure we are of the answer.

### Added
- `app/portfolio/state.py` — `AccountState` with every monetary field optional,
  `Freshness`, `Source`, `Reconciliation`, and `PortfolioHealth` as a
  precedence: ERROR > RECONCILIATION_REQUIRED > STALE > WARNING > HEALTHY, with
  the reasons returned beside the state.
- `app/portfolio/exposure.py` — `notional_of` (refuses without a measured
  contract size), `mark_to_market` (through the platform's single conversion),
  `Bucket` carrying gross AND net, `compute`, `open_risk`, `correlation_note`.
- `app/portfolio/pnl.py` — `unrealized_of` (all-or-nothing), `Drawdown` whose
  peak only ever rises, `margin_utilisation`.
- `app/portfolio/events.py` — `changes_between`: what one refresh implies, and
  nothing else.
- `app/portfolio/service.py` — the assembler, the account resolvers for both
  environments, and `NOT_SUPPLIED`.
- `app/api/v1/portfolio.py` — 14 GET routes, replacing the `/portfolio/summary`
  501 stub.
- Four account-scoped event types in L07's catalogue: `PORTFOLIO_UPDATED`,
  `EXPOSURE_UPDATED`, `DRAWDOWN_ALERT`, `PORTFOLIO_HEALTH_CHANGED`.
- `PortfolioDashboard` and a real `/portfolio` page.
- `PORTFOLIO_ARCHITECTURE.md`.
- 90 backend tests, 8 API tests, 7 frontend tests.

### Changed
- `portfolioService` in the frontend went from a hardcoded `unavailable` to real
  calls; `accountsService` is new beside it.
- `/portfolio` nav entry from `planned` to `partial` — not `available`, which
  would have broken the project-wide invariant that nothing yet claims to be, and
  which would have overstated a level whose live-broker and correlation paths are
  unexercised here.

### Fixed
- The tz-awareness seam at the trading-day boundary, before it shipped.
  `day_start` (L17) takes aware UTC; this engine works in naive UTC. Passing one
  through unconverted would have had `.astimezone(UTC)` read it as local time and
  started the trading day at 18:30 the previous evening on a UTC+5:30 machine —
  the bug `CLAUDE.md` documents, reached from a new direction.
- The L29 test count in `TESTING.md`: 1700 was an estimate, 1688 was the run.

### Refused
- **A notional without a measured contract size.** Reported as uncomputable and
  counted, never assumed. The metals-points error made unrepresentable.
- **A currency breakdown built from ticker names.** Withheld entirely when any
  held symbol lacks a recorded base or quote; splitting a six-letter ticker works
  for the majors and is wrong for everything else.
- **A partial unrealized total.** `None` for the whole account when any position
  could not be marked, with the unmarkable symbols named.
- **Zero risk for an unstopped position.** `None`, because zero would say the
  position cannot lose.
- **Stale values presented as current.** A disconnected broker reports every
  figure `null` with a reason, never the last values seen.
- **A snapshot of an unreadable account.** Not written at all — a fabricated
  point would enter the equity curve as a real one.
- **`agrees` for a reconciliation that never ran.**
- **Repairing a discrepancy.** Compared and reported; settling stays with L21's
  reconciler.
- **A correlation engine.** §27 says not to fake one and `market_bars` covers one instrument.
- **Currency conversion.** No FX rate source is wired, and an invented rate is
  the swap-unit error again.
- **An equity chart drawn from nothing.**
- **A migration.** `portfolio_snapshots` already had every column.

---

## Level 29 — AI model monitoring and drift detection

Is the model behaving consistently with the conditions it was validated under?
And: monitoring may never act on the answer.

### Added
- `app/monitoring/collect.py` — the piece that was missing. Reads
  `ai_decisions` (L27), `model_deployments` (L28) and `trades` (L19). Computes
  nothing.
- `app/monitoring/baselines.py` — an explicit, identified reference, never
  silently changed.
- `app/monitoring/health.py` — six states by precedence. No score.
- `app/monitoring/alerts.py` — fingerprint, cooldown, deduplication, recovery.
- `app/monitoring/config.py` — thresholds and windows, all configurable.
- `app/monitoring/service.py` — one run: collect, check, derive, snapshot,
  alert.
- Four checks in `checks.py`: `latency`, `availability`, `confidence_drift`,
  `possible_concept_drift`.
- `model_monitoring_snapshots` and `model_alerts`, migration `0021`.
- Six routes under `/v1/ai/monitoring`.
- Three realtime types on the `model` scope.
- Frontend: model health, alerts and snapshots on the AI Lab.
- 42 backend tests, 10 frontend tests.

### The decisions worth knowing
- **`INSUFFICIENT_DATA` outranks `HEALTHY`**, in the precedence and in a CHECK
  constraint. Too little data must never become a false pass.
- **`OFFLINE` outranks everything.** A model that is not serving cannot be
  healthy or degraded.
- **`DEGRADED` is reserved for the model.** A feature distribution moving is the
  market moving, and it warns rather than degrades.
- **Concept drift is inferred from one pattern only** — outcomes moved, inputs
  did not — and the reverse case says "NOT concept drift" explicitly.
- **A severity change is a new alert.** The fingerprint includes the severity so
  WARNING → CRITICAL cannot be suppressed.
- **`availability` excludes `AI_DISABLED`** from the denominator: counting it
  would make turning the AI off look like perfect uptime.
- **An unresolved decision is not a loss.** A risk veto says nothing about
  whether the model was right.

### Fixed
- A constraint name 67 characters long. PostgreSQL truncates identifiers at 63
  and appends a hash, so the model's name and the migration's diverged. Caught
  by the drift test.

## Level 28 — Model registry and model lifecycle

The authoritative answer to "which model versions exist, what happened to them,
and which are eligible for use?".

### Added
- `app/ai/lifecycle.py` — eight states and the transition table that is the
  whole truth about what may follow what.
- `app/ai/artifacts.py` — the `sha256` digest over the canonical artifact,
  taken at registration and re-checked before every load.
- `app/ai/registry_service.py` — register, deploy, promote, roll back, stop,
  retire; lineage; history. Every transition through one function.
- `app/ai/resolution.py` — deterministic scope resolution and a cache keyed by
  version rather than scope.
- `app/ai/comparison.py` — two versions side by side, leading with the reasons
  they may not be comparable.
- `model_deployments` and `model_lifecycle_events`, migration `0020`, plus
  fourteen nullable columns and two CHECKs on `model_versions`.
- Thirteen routes under `/v1/ai`. No DELETE anywhere.
- `Permission.promote_ai_models`, administrator only.
- Frontend: the model registry, deployments, lifecycle history and the
  lifecycle table itself on the AI Lab.
- 59 backend tests, 17 API tests, 11 frontend tests.

### Changed
- `app/ai/eligibility.py` now reads the registry status first — the switch L27
  said would be one function.
- L27's paper bots resolve through `RegistryResolver`, once per run.
- The realtime catalogue gained a `model` scope and eight event types.

### The decisions worth knowing
- **Eight states, four declined in code with reasons.** `VALIDATING` was
  declined because nothing could set it.
- **One edge into `promoted`, from `paper`.** §12 as a table, not a check.
- **`validated` does not serve inference.** Two different checks; both must
  hold.
- **Superseding is not retiring.** The replaced version returns to `registered`
  so a rollback is not a resurrection of a terminal state.
- **The cache is keyed by version, never by scope**, so a lifecycle change needs
  no invalidation.
- **A version substitution is refused, not served.** A decision must name the
  model that made it.

### Fixed
- **NULLs are distinct in a unique index**, so the one-active-per-scope
  constraint did not hold for the unrestricted scope. Found by the test that
  tried to insert a second active deployment and got no error. Fixed with a
  derived NOT NULL `scope_key`.
- **L25's and L26's realtime events had never published.** `Hub.publish` takes
  one argument; both services passed two, and the `except Exception` swallowed
  every `TypeError`. The events never reached the bus and nothing noticed.
- Migration 0020 passed a pre-prefixed constraint name to `drop_constraint` —
  the same defect as 0014, 0015 and 0019, now on a third constraint kind. The
  rule has no exception: every constraint kind, bare name, always.
- The transition table had no edge for a deployment ENDING, so supersession and
  emergency stop both failed. `paper → registered` and `promoted → registered`
  added.

## Level 27 — AI strategy integration

The seat `app/execution/ai.py` has held since L16 is now filled. AI enhances a
strategy decision; it never becomes the final authority over trading risk.

### Added
- `app/ai/decision.py` — the L27 vocabulary: `AiMode` (four values),
  `Decision`, `ScoringMethod`, `AiThresholds`, `ModelRequirement`,
  `AiStrategyConfig`, `SignalContext`, `AiDecision`. `AiPolicy` moved here from
  `app/execution/ai.py`, which re-exports it.
- `app/ai/integration.py` — the deterministic pipeline: resolve, check
  compatibility, prepare features, infer, validate the output, apply the
  policy, produce a decision. It sends nothing.
- `app/ai/eligibility.py` — may this version be used? A boundary, not a
  registry: it reads L26's verdict and will read L28's status.
- `app/ai/strategy_config.py` — the database half: load, save, journal.
- `app/execution/ai.py` — `IntegrationFilter`, the mode-aware seat.
  `ModelBackedFilter` is KEPT and still tested.
- `ai_strategy_configs` and `ai_decisions`, migration `0019`.
- Six routes under `/v1/ai/integration`. No PUT, no DELETE.
- Backtest: `apply_ai_filter`, and an `ai` block on `BacktestResult`.
- Frontend: the AI Strategy Center on the AI Lab — the pipeline, the journal
  and the configuration table.
- 59 backend tests, 13 API tests, 9 frontend tests.

### Changed
- `PaperEngine` builds a normalized `SignalContext` from the SAME `Candles`
  object the strategy read, and hands that to the seat rather than the raw
  signal. The AI layer sees what it is permitted to use.
- `ExecutionPipeline` does the same for an externally-arriving signal.
- `PaperService` takes the model registry and builds the seat for a bot whose
  strategy is configured for an AI mode.

### The decisions worth knowing
- **AI_DISABLED means there is no AI object**, not a filter that accepts
  everything — otherwise a disabled deployment and a model that agrees with
  everything look identical in the counters.
- **AI_ADVISORY records NEUTRAL, never ACCEPT.** Somebody will count agreement.
- **The default scoring formula is `min(strategy, ai)`**, the only one of three
  that cannot let a confident AI rescue a weak signal.
- **A window reaching past the signal is refused, not trimmed.** A trim turns a
  caller's bug into a silently different evaluation.
- **A flat bar is never offered to the AI.** There is no branch that could turn
  a 0 into a ±1.
- **AI confidence is not allowed risk.** A 99% probability produces the same
  four-field verdict as a 51% one.

### Fixed
- Migration 0019 first passed a pre-prefixed unique-constraint name, producing
  `uq_ai_strategy_configs_uq_ai_strategy_configs_...`. The same defect
  migrations 0014 and 0015 had; caught by the drift test against PostgreSQL.
- The integration service reported `PIPELINE_ERROR` for an out-of-range
  probability, because L24's `Prediction` refuses one in its own constructor.
  Now caught by type and reported as `INVALID_OUTPUT` naming the model and the
  field.

## Level 26 — AI validation engine

Whether a trained candidate is valid enough to be *considered*. Not whether it
is good, and not whether to deploy it.

### Added
- `app/validation/` — `config` (thresholds and the verdict rule), `statistics`
  (permutation null, AUC, bootstrap, clustered t), `economics` (the model's
  decisions through `tools/rule_backtest.simulate`), `checks` (fifteen named
  checks), `report`, `service` (the background job).
- `app/ai/loader.py` — reads a stored artifact back into a model that predicts.
  L24 wrote `artifact_of()` and nothing had ever read it back.
- `app/datasets/service.build_validation_loader` — `(bars, dataset)` from one
  read of the raw layer, so the economic evaluation runs over exactly the bars
  the model was scored on.
- `validation_runs` table, migration `0018`. One table; nothing altered.
- Six routes under `/v1/ai/validation`. No PATCH, PUT or DELETE.
- Frontend: a Validation panel on the AI Lab, with the full report table.
- 75 backend tests, 11 API tests, 7 frontend tests.

### The decisions worth knowing
- **The verdict is not a score.** Precedence, not arithmetic:
  `BLOCKED > FAIL > CONDITIONAL > PASS`. A test asserts the payload carries no
  score key, so a composite cannot be added later without a failure.
- **BLOCKED is not FAIL.** FAIL says the candidate is not good enough; BLOCKED
  says we could not tell. A missing check is never a passing one.
- **A PASS promotes nothing.** `model_versions.status` is never written, and
  migration 0018 deliberately declines to add a `validated` status that nothing
  could set.
- **One simulator.** The economics come from the engine every measured figure in
  `CLAUDE.md` came from, with the spread charged and entry at the next bar's
  open.
- **The model against its own shuffled self.** Beating a baseline is not
  evidence — this project's own sweep had the *random* rule at t = 1.76 against
  the best real candidate's 0.83.
- **Calibration is a WARNING.** A miscalibrated model can still rank correctly;
  the warning constrains how the number may be read, not whether it is usable.

### Fixed
- `checks.temporal` merged row counts and index bounds under the same keys, so
  `evidence["train"]` was a list where the sentence beside it said a count.
- Migration 0018 first declared `sa.JSON()` where the model uses JSONB on
  PostgreSQL. Caught by the drift test, which only runs against a real database.

## 2026-09-04 - Verification against real services

The first run of this platform against the PostgreSQL and Redis it deploys on.
Every gate before this ran on SQLite with Redis absent.

**Fixed**

- **`positions.status` was `VARCHAR(8)`** while L21 added `partially_closed`
  (16 chars) and `reconciling` (11). SQLite ignores VARCHAR length, PostgreSQL
  enforces it - so every position test passed and a partial close would have
  failed in production. Widened to `String(24)`.
- **`training_runs.status` was `VARCHAR(16)`** against L25's
  `validation_pending` (18). Widened to `String(32)`. This is how the first one
  was found.
- **Migrations 0014 and 0015 passed pre-prefixed names to `drop_constraint`**,
  so their downgrades were broken. The naming convention adds the prefix; every
  migration from 0007 onward passes the bare name.
- **Three `Money` columns were created `Numeric(18,8)`** where their models
  declare `Numeric(18,4)`. Corrected forward by migration `0016`.
- **The backend image could not run the toolkit.** The Dockerfile now builds
  from the repository root and copies `tools/` to `/srv/tools`, docker-compose
  sets the context, and a `.dockerignore` keeps ~580MB of node_modules and
  measured data out of it.

**Added**

- `tests/test_models.py::test_every_status_value_fits_its_column` - walks every
  `col IN (...)` CHECK against its column's declared length, on every table, so
  the widest class of these cannot recur silently.
- Migrations `0016_money_precision` and `0017_status_column_widths`.
- `.dockerignore`.

**Verified**

- Backend **1417 passed, 0 failed, 0 skipped** - the first fully green run.
- All 17 migrations apply, downgrade and re-apply against PostgreSQL with zero
  autogenerate drift; the development database migrated 0009 -> 0017 in place.
- The image builds and every previously-broken import runs inside it.
- The AI chain end to end over HTTP against PostgreSQL: 700 bars stored
  idempotently, a READY dataset of 638 rows with 6/6 leakage checks, and a
  training job ending at `validation_pending` with a draft candidate. Promoted
  models: 0.

---

## 2026-09-04 - Level 25: AI Training Engine

**Added**

- `app/training/config.py` - a versioned, hashed training configuration.
  `SplitConfig` is configurable and there is no shuffle field anywhere.
- `app/training/gates.py` - data-quality gates that read what L23 already
  measured. A dataset that is not READY cannot be trained on.
- `app/training/trainers.py` - a majority baseline fitted before every
  candidate, class weighting, and a deterministic weighted logistic fit with
  early stopping on the validation segment.
- `app/training/metrics.py` - ML metrics and economic metrics as two
  dictionaries that are never merged; calibration from L29's implementations.
- `app/training/service.py` - queue, run, cancel, wait, shut down. Background
  tasks bounded by a semaphore, on the pattern the backtester has used since
  L14. No new queue.
- `/v1/ai/training`: engine contract, create, list, get, metrics, cancel - on
  the existing AI router rather than a new one.
- Migration `0015`: nine columns, two statuses, two CHECK constraints, and
  `model_version_id` relaxed to nullable.
- 51 backend tests, 12 API tests, 4 frontend tests.

**Changed**

- `/ai-lab` gains a Training panel above Models, Datasets and Features.
- `training_runs.model_version_id` is nullable. A job is created when it is
  QUEUED and the candidate does not exist until the fit finishes; the old NOT
  NULL made a queued row impossible to write.
- `DatasetConfig` carries `provider_symbol`, so a dataset can be rebuilt from
  its manifest without guessing which provider symbol produced its rows.
- The application's shutdown stops training beside replay and paper.

**Fixed**

- **The fit starved the event loop.** A synchronous CPU-bound loop inside a
  background `asyncio` task blocks every other request for as long as it runs.
  A background task is not enough; the fit now runs on a worker thread.
- **The cancel flag was looked up by job id.** The task's done-callback pops
  that entry, so a cancelled job's worker thread found no handle, read "not
  cancelled" and ran the fit to completion - burning CPU long after the job was
  recorded as cancelled. The flag is now owned by the job.

**Refused**

- **Training promotes nothing.** A successful run ends at
  `validation_pending` and writes a DRAFT version. There is no `completed`
  status that could be read as approval, no PATCH/PUT/DELETE on the router, and
  the string `"promoted"` does not appear in the package.
- **A dataset that is not READY cannot be trained on**, and one that moves
  under a running job fails it. The fingerprint is re-derived after the fit.
- **No hyperparameter search.** This repository's own 36-cell bracket sweep
  scored the RANDOM rule at 1.76 in-sample against the best real candidate's
  0.83. A search is a machine for producing winners that do not survive, and
  building one before L26's correction machinery would build that trap.
- **Nothing is resampled.** Class weighting instead: duplicating rows in a time
  series creates the same bar twice at one timestamp.
- **The test segment is never consulted for any decision**, including early
  stopping.
- **A cancelled job is never recorded as trained**, and a failed one leaves no
  candidate behind.
- **An unknown model family or class weight is refused, not ignored.** A caller
  who asked for weighting should not silently get none.
- No arbitrary code, model path or dataset path is accepted from a caller.

---

## 2026-09-04 - Level 24: AI Models

**Added**

- `app/ai/contract.py` - one `Prediction` shape for every model family, six
  refusal statuses, and a type in which a refusal cannot carry a value.
- `app/ai/base.py` - the gate every model passes: fitted, feature version,
  required features, freshness. In one place and in a fixed order.
- `app/ai/regime.py` - five regimes plus UNKNOWN, from four boundaries fitted
  as quantiles of the training segment rather than chosen as constants.
- `app/ai/probability.py` - logistic regression in the standard library, whose
  coefficients are the explanation rather than a surrogate for it.
- `app/ai/anomaly.py` - a robust z-score detector using median and MAD.
- `app/ai/registry.py` - resolution by explicit version; `latest()` is a
  separate call, and a duplicate `(key, version)` is refused.
- `AiPolicy`, `AiGate` and `ModelBackedFilter` in `app/execution/ai.py`, beside
  the seat, so the dependency runs one way.
- `/v1/ai`: models, versions, one model, vocabulary, predictions, and one
  `predict` verb that places nothing.
- Migration `0014`: model provenance, a promoted-provenance CHECK, a UNIQUE
  `prediction_key`, and a refusal-carries-no-value CHECK.
- 56 backend tests, 11 API tests, 4 frontend tests.

**Changed**

- `/ai-lab` shows loaded models beside the datasets and the feature registry.
- `numpy>=1.24` is declared in `backend/requirements.txt`. It has been imported
  by the backend since L12 and was never declared.
- The `/ai/models` and `/ai/predictions` pending groups are gone, and with them
  the last pre-v1 alias: `LEGACY_PATHS` is now empty and a test asserts it.

**Fixed**

- **A refused prediction was stored as the JSON literal `null`, not SQL NULL.**
  SQLAlchemy's `JSON` serialises Python `None` that way by default, so the
  CHECK that exists to stop a refusal carrying a value failed on a refusal.
- **A prediction id did not cover its input.** Two different feature vectors
  scored at one timestamp produced the same id and collapsed into one row; a
  test recorded three predictions and found one.
- A test that grepped source text for a forbidden import failed on a docstring
  explaining the rule. Rewritten to parse imports.

**Refused**

- **No scikit-learn.** The models are a few dozen lines each, a logistic
  model's coefficients ARE the explanation, and the audit had just found that
  the backend image does not carry numpy either. Adding a framework would
  deepen a deployment gap for no measured gain.
- **A model with no parameters answers MODEL_UNAVAILABLE.** There is no default
  weight vector: a default answers 0.5 for everything, and 0.5 is a number a
  caller will act on.
- **AI_REQUIRED and no answer means no trade.** A model that could not answer
  is not a model that agreed.
- **A feature version the model was not fitted on blocks inference.**
  Compatibility must be declared; the default is to refuse.
- **A missing feature is a MODEL_INPUT_ERROR naming it.** Nothing is
  substituted anywhere in the package.
- **A duplicate model version is refused.** A caller holding "regime v1.0" must
  be holding the same thing tomorrow.
- **A probability is P(a named label), never P(profit)**, and `calibrated` is
  false until something measured it.
- **An anomaly score is not a probability and not a severity.** A rare bar is
  not a bad bar: this layer labels, and risk decides.
- **A credential-shaped key in a model's parameters is refused.**
- Nothing is trained, promoted or deployed. There is no PATCH, PUT or DELETE on
  `/v1/ai`, and the registry is empty at startup.

---

## 2026-09-04 - Level 23: AI Data Pipeline

**Added**

- `app/datasets/features.py` - 22 causal features over normalised bars,
  computed from `app.strategies.indicators` rather than a second indicator
  engine. Every one is dimensionless.
- `app/datasets/labels.py` - forward return, direction, a path-dependent
  bracket outcome, and MFE/MAE in ATR units. All read bars strictly after T.
- `app/datasets/leakage.py` - six automated checks. A failure blocks the
  dataset at CLEAN.
- `app/datasets/scaler.py` - normalisation whose `fit()` takes a split and
  reads only the training segment.
- `app/datasets/splits.py` - chronological split and expanding walk-forward
  folds. Nothing shuffles, and there is no parameter that would.
- `app/datasets/resampling.py` - OHLCV aggregation with timestamp-derived
  boundaries and incomplete candles marked.
- `app/datasets/quality.py` - a data-quality score built on L08's existing
  measurements.
- `app/datasets/builder.py` and `service.py` - alignment, versioning, the
  manifest and the fingerprint.
- `/v1/datasets`: list, get, manifest, checks, the feature registry, the label
  registry, an engine-contract route, and one build verb.
- Migration `0013`: `feature_sets`, `label_sets`, `datasets`, `dataset_checks`.
- 68 backend tests, 14 API tests, 3 frontend tests.

**Changed**

- `/ai-lab` is a real page for the first time: a dataset table whose leakage
  column is explicit, and the feature registry served by the backend rather
  than mirrored in the browser.
- `PlannedPage` gains `PlannedSections`, so a page can have working panels
  above planned ones without a second copy of the "not built" grid.
- `tests/test_models.py` expects 45 tables, up from 41.

**Refused**

- **A dataset cannot be marked READY by anyone.** The builder decides, and it
  cannot reach READY while a leakage check fails. `/v1/datasets` has no PATCH,
  PUT or DELETE, and a test walks the route table to keep it that way.
- **No raw price level is offered as a feature.** A model trained on `sma_20`
  learns the price of the instrument, and pooling price-scaled quantities
  across symbols is the arithmetic error the metals run already measured.
- **`spread_points` has no default.** A WIN computed without the spread is a
  label for a market nobody trades in, and cost drag is the only effect this
  repository has measured to significance.
- **A bar containing both barriers is AMBIGUOUS.** Bar data cannot order two
  prices inside a bar; guessing is what put both exits on the wrong side of a
  fill in the live log.
- **The last `horizon` rows are dropped, never filled.** A fabricated label
  teaches a model that the end of a dataset is a kind of market.
- **Out-of-order bars are refused rather than sorted.** Sorting would hide an
  upstream ordering problem that every causal guarantee here depends on.
- **A real market anomaly is kept.** A crash is data. Only corrupt rows - a
  bar that is not a candle - block a series.
- **Nothing is trained, deployed or replaced.** The package imports no model
  and no ML framework, asserted by parsing every module.

---

## 2026-09-04 - Level 22: Autonomous Bot Manager

**Added**

- `app/bots/state.py` - a 9-state run machine, shared with the paper service
  rather than duplicated. `MAY_TRADE` is the single-element set `{running}`.
- `app/bots/limits.py` - per-bot limits that combine with the account's by
  taking the more restrictive figure in every field, so a bot cannot widen
  what its account allows. `cooldown_seconds` is the one field where the
  larger number wins, because a longer cooldown is the stricter one.
- `app/bots/supervisor.py` - measures each run's heartbeat, marks a silent one
  `crashed`, and plans a restart without acting on it.
- `app/bots/worker.py` - `BotSupervisorWorker`, on the existing `Worker` base.
- `app/execution/ai.py` - the AI seat, moved out of the paper engine.
- `/v1/bots`: list, get, events, limits, disable, enable, supervise,
  restart-plan, and `preflight`, which runs every start gate and starts
  nothing.
- Migration `0012`: `paused`, `recovering` and `disabled` run states, five
  per-bot limit columns, and the disable flag with its reason and timestamp.
- 36 backend tests, 13 API tests, 2 auth tests, 1 frontend test. Backend
  totals go from 1140 passed / 15 failed / 3 skipped to **1187 / 15 / 3**; the
  15 are the same `redis.exceptions` in both, because Docker was not running.

**Changed**

- `/bots` is a real page and a real table. The heartbeat column shows the
  measured age and marks a stale one, because every other column on a dead
  bot's row still reads as healthy.
- `app/paper/engine.py` re-exports `AiFilter` and `AiVerdict` from their new
  home, so no existing caller changed.
- The `/bots` row is gone from `app/api/pending.py`. The group is built.
- `GET /bots` (unversioned) is 404. The alias carried a 501 for the whole
  group; once the group is real it would be a second path to one
  implementation, which is what `/orders` was removed for at L06.

**Fixed**

- **A paused bot and a bot shutting down were the same database row.**
  `PaperService.pause_bot` set its in-memory status to `paused` and wrote
  `stopping` to `bot_runs`, because the table had no value for `paused`. After
  a restart nothing could tell the two apart, and they need opposite
  treatment: one should stay paused, the other is an orphan.
- **A circular import introduced at L20.** Importing `app.paper.service`
  before anything else raised `ImportError: cannot import name 'AiFilter' from
  partially initialized module 'app.paper.engine'`. The test suite's import
  order happened never to hit it. Fixed by moving the AI seat rather than
  reordering imports - a module that two pipelines depend on cannot live
  inside one of them.
- **An authorization regression this level shipped and its own verification
  caught.** `GET /v1/bots` was a 501 stub gated on `manage_bots`; the router
  replacing it asked only for a logged-in user, so a plain USER could list
  every bot, its mode, its limits and why it last stopped. Reads now ask for
  the same permission as the writes. The rule is now a test: a built group
  inherits the gate of the stub it replaces.
- Five pre-existing `mypy` errors in `tests/test_positions.py` and
  `tests/test_execution.py`, from L20 and L21. A type gate known to be red
  stops being read.
- `frontend/src/lib/nav.ts` described `/orders`, `/positions` and `/bots` as
  `planned` - documented as "shell only; every control disabled" - which
  stopped being true at L19, L21 and L22. All three are now `partial`.

**Refused**

- **Recovery refuses by default.** With no safety check wired to the
  supervisor, every restart attempt is refused and says so. A supervisor that
  restarted everything because nobody had told it not to would be the most
  dangerous default in the system.
- A `halted` run is not recoverable. A kill switch is a decision somebody
  made, and recovering out of it automatically is the bot-level bypass the
  brief forbids.
- `plan_restart` never starts a bot. A `stopped` bot stays stopped, a `paused`
  one stays paused, and a `running` row whose process is gone is marked
  `crashed` for a human or the supervisor to consider.
- A live-mode bot fails preflight while `LIVE_TRADING` is false, and the
  refusal names the setting rather than the request - a caller retrying with
  different JSON should learn that the answer will not change.
- `disable` closes no position, and says so in its response rather than
  leaving an operator to assume it does.
- `BotCounters` are read from the database on every check and never cached. An
  in-memory counter comes back as zero after a restart, handing a bot that had
  spent 9 of 10 daily trades a fresh ten.

---

## 2026-09-04 - Level 21: Position Management

**Added**

- `app/positions/broker_executor.py` - closes a demo or live position through
  the account's order manager and the broker adapter. This is the gap
  `MIGRATION_STATUS.md` recorded against L21.
- `app/positions/reconciler.py` - reads the venue, records every
  disagreement, and corrects only the unambiguous.
- `BreakEvenPolicy` and `PartialTakeProfitPolicy`, both off by default.
- `PositionManager.close_now` for a decision the caller made, sharing one
  confirmation contract with `process` via `_act`.
- Migration `0011`: four position states, `initial_quantity`,
  `closed_quantity`, `realized_pnl`, and what the venue reports its
  protective levels to be.
- `POST /v1/positions/{id}/protect` and `POST /v1/positions/reconcile`.
- 32 backend tests, 15 API tests, 1 frontend test.

**Changed**

- `POST /v1/positions/{id}/close` is built. Paper closes in the simulator;
  demo and live go through the order manager. The mode alone selects the
  executor.
- The most protective stop proposal wins when two policies propose one, rather
  than whichever ran first.
- The positions table shows what the venue holds beside what the platform
  intends, and marks a level the venue is not holding.

**Fixed**

- `initial_quantity` defaulting to zero would have made every position claim
  to have opened at nothing. Backfilled in the migration, at creation, and in
  the manager before a fill is booked.

**Refused**

- Clamping a close larger than the position. Refused in three places.
- Removing a protective stop through a null field. An API where a missing
  field deletes a stop is an API where a typo does.
- Inventing an exit price for a position the venue no longer holds.
- Concluding anything when the venue cannot be read.
- Adopting an unexpected broker position as ours.
- Storing unrealised P&L.
- Deleting `UnavailableExitExecutor`, which is still the right answer for a
  mode with no adapter.

---

## 2026-09-03 - Level 20: Automated Execution Engine

**Added**

- `app/execution/` - the orchestrator for externally-arriving signals.
  `outcome.py` (the shared vocabulary), `pipeline.py` (the gates, none of them
  reimplemented), `worker.py` (a supervised loop, so execution does not depend
  on a browser).
- `app/api/v1/execution.py` - start, stop and status. A control plane; there
  is no route that takes a payload and trades it.
- `tests/test_execution.py` - 48 tests, including the end-to-end paper trade
  and the live safety test.

**Changed**

- `Outcome` and `NO_ORDER` moved from `app/paper/engine.py` to
  `app/execution/outcome.py` and are re-exported from their old home, so both
  pipelines report in one vocabulary and their counters can be added together.
  Nine values were added for the ways an EXTERNAL signal can fail.
- `SIGNAL_CREATED`, published since L09 and consumed by nothing, now has
  exactly one consumer.

**Refused**

- Widening `PaperEngine` to consume external signals. It produces signals from
  bars; the orchestrator consumes signals produced elsewhere. One class doing
  both is two jobs wearing one name.
- Consuming a signal whose execution is unresolved. `execution_unknown`,
  `no_venue`, `spec_incomplete` and `strategy_error` leave the row in `new`,
  because those conditions clear and finishing them would throw a signal away
  because a dependency was briefly down.
- Executing a weakly-authenticated alert. The gateway records it; acting on it
  is a higher bar than hearing it.
- Executing inside the webhook request.
- Starting the worker at boot.
- Inventing an entry price when the alert carries none.
- Touching `tools/run_overnight.py` or `take_profit.py`.

---

## 2026-09-03 - Level 19: Order Management System

**Added**

- `app/oms/` - the lifecycle, shared by every execution mode. `state.py` (the
  12-state machine), `fills.py` (partial-fill accounting), `order.py`
  (`ManagedOrder`), `service.py` (`OrderManager`, the only path from an
  approval to a venue), `events.py`, `repository.py`, `registry.py`.
- Migration `0010_oms_lifecycle`: four order states, fill accounting,
  provenance, lifecycle stamps and the audit-trail columns.
- `POST /v1/orders` (built), `POST /v1/orders/{id}/cancel`,
  `POST /v1/orders/{id}/reconcile`, `GET /v1/orders/oms/status`.
- `tests/test_oms.py` - 63 tests where the broker-bound lifecycle had none.
- A real orders table and a working order ticket in the frontend.

**Changed**

- The order state machine moved from `app/paper/oms.py` to `app/oms/state.py`
  and is re-exported from its old home, so every existing caller and test is
  unchanged. A test asserts the two modules hold the same object.
- The ten `ORDER_*` event types that L07 declared and named L19 as the
  producer of now have one. Events are queued and drained after the state is
  durable, so a slow bus cannot sit inside a broker interaction.
- `orders` and `order_events` gained the columns the lifecycle had been
  reconstructing.

**Fixed**

- **The audit trail could not be ordered.** Four transitions completing inside
  one clock reading carry identical `occurred_at`, and the primary key is a
  uuid. `order_events.sequence` makes the trail readable; without it, it was
  not an audit trail.

**Refused**

- Collapsing `failed`, `rejected` and `unknown` into one state. The difference
  is exactly what decides whether a re-send is safe.
- Retrying an `unknown` submission. Its exit set contains no sendable state,
  so the refusal is structural rather than a rule someone must follow.
- Concluding anything when the venue cannot be read. A reconciler that decides
  an order is lost because the network was down is worse than one that refuses
  to decide.
- Truncating an overfill, and applying a repeated deal id.
- A `DemoOMS` and a `LiveOMS`. One manager; the mode is which adapter an
  operator registered.
- Claiming limit and stop orders execute as pending orders. `OrderRequest` has
  no price field, so they currently reach the venue as market orders, and that
  is stated rather than papered over.
- Flipping any live gate. `order_idempotency` and
  `unknown_status_reconciliation` are built and held false.

---

## 2026-09-03 - Level 18: Position Sizing Engine

**Added**

- `app/sizing/service.py` - counters, latency and logging around the engine,
  exposed as a `status()` dict in the shape `app.risk.service` already uses.
- `app/api/v1/position_sizing.py` - `POST /calculate`, `GET /modes`,
  `GET /status`. The calculate route runs the Risk Engine's side-effect-free
  preview alongside the size, so a caller sees the veto it would meet.
- `tests/test_sizing.py` - 59 tests where the module had none.
- `frontend/src/components/SizingCalculator.tsx` - a real calculator on
  `/risk`. Every figure comes from the backend; the browser computes nothing.
- `sizing_refusals` on a backtest result: entries the engine could not size.

**Changed**

- Risk sizing is wired into the **backtester**, through the same engine the
  paper pipeline uses. Equity is walked forward from closed trades; the stop
  distance is `atr[entry-1]`, the figure `simulate()` itself used. Both are
  pinned by tests as the no-look-ahead guarantee.
- `value_per_price_unit` moved to `app/symbols/precision.py` and is
  re-exported from `app/paper/portfolio.py`. Sizing had an inline copy of the
  same conversion; there is now one. It also now refuses a non-positive
  `tick_value`, which previously made a price move worth nothing silently.
- Sizing floors through `app/symbols/precision`, the same implementation the
  broker layer uses, instead of a private `_round_to_step`.
- The paper engine passes the bracket as levels as well as a distance, so
  direction is validated in the live pipeline.

**Fixed**

- **A quantity below the venue minimum was raised TO the minimum**, recording
  the overshoot in a `gap` string while still returning a usable volume. That
  silently risks more than the configured budget. It now refuses, naming what
  the minimum lot would have cost.
- **Stop direction was never validated.** `abs(entry - stop)` makes a target
  indistinguishable from a stop, so a long order with its stop above its entry
  sized normally.
- The backtest runner accumulated unrounded floats for its own walk while the
  equity curve accumulated rounded ones, so the two balances drifted.

**Refused**

- Raising a size to the broker minimum. Refusing is the only answer that keeps
  the configured risk true.
- Correcting a stop on the wrong side. Both available corrections change what
  the caller asked for.
- Risk sizing in **replay**: its engine is proved trade-for-trade identical to
  `simulate()`, and varying the size per position changes that engine rather
  than configuring it. The request body still accepts the field and refuses it,
  rather than accepting and ignoring it.
- Four per-instrument calculator classes that would all compute
  `distance / tick_size x tick_value`.
- A 30th realtime event type. Sizing rides in the order payload, the risk
  decision and the metrics.

---

## 2026-09-03 — TradingView autonomy: audit, architecture and plan

Documentation only. No code changed; all three suites green before and after by
virtue of nothing having moved (research 34, backend 900 passed / 3 skipped,
frontend 77).

**Added**

- `TRADINGVIEW_ARCHITECTURE.md` — the TradingView → execution chain, the
  Autonomous Trading Orchestrator design, a KEEP/MODIFY/ADD decision for every
  capability the autonomous-builder brief names, and the A0–A9 ladder.
- `TRADINGVIEW_DISCOVERY.md` — the four legitimate sources, what each can
  actually carry, the `data/tradingview/` inbox, and the access this project
  will not attempt.
- `STRATEGY_SPECIFICATION.md` — the canonical `TradingViewSpec`, and the Pine
  subset that can reach it, declared explicitly.
- `STRATEGY_COMPILER.md` — the spec → `StrategyDefinition` mapping, the
  behaviour-preservation report, and the six refusals.
- `STRATEGY_VALIDATION.md` — the G1–G9 gate ladder, what each gate proves, and
  what VALIDATED does not mean.
- `DECISIONS.md` — the engineering decision log (brief §36).
- `CHANGELOG.md` — this file.
- `PROJECT_STATE.json` — machine-readable project state (brief §42).

**Found**

- A saved L13 strategy definition cannot be backtested, replayed or
  paper-traded: all three runners resolve strategies through
  `registry.create()`, which knows only classes registered at import, and a
  `BuiltStrategy` is built from data. Scheduled as **A3**, ahead of the
  compiler, because everything downstream of compilation routes through those
  three call sites.
- No TradingView → MT5 shortcut exists anywhere in the repository, verified by
  import graph. There is nothing to unpick.

**Refused**

- Building the alert → execution bridge now. It needs L19's order state machine
  and L18's sizing modes; building it first would mean a second OMS or an
  execution path predating its own guard. Reported as blocked per brief §41.
- Widening the indicator catalogue beyond SMA, EMA, RSI and ATR to make a Pine
  script compile.
- Any TradingView access beyond the four legitimate sources: no protected-script
  reading, no account authentication, no scraping, no rate-limit or CAPTCHA
  circumvention.
- Approximating an unsupported Pine construct. `request.security`, loops,
  arrays, user functions, `var`, session filters and pyramiding are recorded and
  the compile is refused.

**Unchanged, deliberately**

`TRADING_MODE=paper`, `LIVE_TRADING=false`, and seven of the ten `LIVE_GATES`
false. Nothing in this plan flips any of them, and the promotion past PAPER
stays an operator act.
