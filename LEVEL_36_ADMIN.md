# Admin panel and system control centre (L36)

Built 2026-09-05. Administrative visibility and user access — and deliberately
nothing else, because every trading control already has an authoritative home.

---

## 1. The audit: most of it existed

| Component | Where | Verdict |
|---|---|---|
| RBAC: 3 roles, 16 named permissions, `DANGEROUS` set | `app/auth/permissions.py` | **KEEP**, reused and served |
| `require_permission` / `require_role` gates | `app/auth/deps.py` | **KEEP**, every new route uses them |
| Server-side sessions, revocable, SHA-256 stored | `app/auth/models.py` | **KEEP**, reused for revocation |
| `/admin/users`, role change, last-admin protection | `app/admin/router.py` (L04) | **KEEP**, mounted unchanged |
| `audit_logs` table + `audit.record` + `_scrub` | `app/core/audit.py` (L04) | **KEEP + MODIFY** — see §5 |
| `GET /v1/admin/audit-logs` | `app/api/v1/admin.py` (L06) | **KEEP + MODIFY** — more filters |
| Double-submit CSRF on state-changing routes | `app/auth/csrf.py` | **KEEP**, covers the new routes |
| Rate limiter, two backends | `app/auth/ratelimit.py` | **KEEP**, reused on the writes |
| Pagination with a hard ceiling, allow-listed sorts | `app/api/pagination.py` | **KEEP**, reused |
| Bot control (`/v1/bots/{id}/disable`, limits, preflight) | L22 | **KEEP**, *not wrapped* — see §2 |
| Model registry verbs (register/deploy/promote/rollback/retire) | L28 | **KEEP**, *not wrapped* |
| Broker status and reconcile | L10 | **KEEP**, *not wrapped* |
| Risk limits and kill switches | L17 | **KEEP**, *not wrapped* |
| Notification channels, Discord status and test | L34, L35 | **KEEP**, surfaced as status |
| `LIVE_GATES` — a code-level gate table | `app/core/settings.py` | **KEEP**, reported, never settable |
| Feature flags, anywhere | — | **do not exist**, and L36 did not add them (§7) |
| MFA / step-up re-authentication | — | **do not exist**, recorded as a gap (§8) |
| `app/admin/service.py`, the new routes, the panel | — | **ADD** |

---

## 2. The decision that shapes everything: no second door

Section 1 says the panel must not become a second trading engine, section 10
says to operate through existing services, and section 49 says it must never
bypass RiskEngine, OMS, PositionManager, BrokerAdapter, Strategy Engine or
BotManager.

The strongest reading of those, and the one L36 took: **the admin API has no
route that touches any of them.**

| To do this | Use | Owned by |
|---|---|---|
| pause or stop a bot | `POST /v1/bots/{id}/disable` | BotManager (L22) |
| set a bot's limits | `PATCH /v1/bots/{id}/limits` | L22 |
| promote or roll back a model | `POST /v1/ai/models/.../promote` | registry (L28) |
| reconcile a broker | `GET /v1/brokers/{id}/reconcile` | adapter (L10) |
| change a risk limit | the risk surface | RiskService (L17) |
| test Discord | `POST /v1/notifications/discord/test` | L35 |

Each already enforces its own permission and is already the authoritative path.
A second door in front of one would be a second authorization surface to keep in
step with the first, and **the one that drifts is always the one nobody is
watching**. The dashboard reports and links; `GET /v1/admin/contract` serves the
table above so a client can check where each control lives.

Three tests hold this:

* `test_the_admin_surface_cannot_reach_anything_that_trades` — an AST walk
  proving the admin modules import no risk engine, order manager, broker
  adapter, sizing service or execution pipeline. There is no object in scope
  through which one could act.
* `test_the_admin_router_writes_only_user_access` — the complete list of
  POST/PATCH/PUT/DELETE paths is exactly three, all about a user's access.
* `test_there_is_no_route_that_enables_live_trading` — no assignment to
  `live_trading`, `trading_mode` or a `LIVE_GATES` entry anywhere in the module.

---

## 3. What the panel does do

| Route | Gate | What |
|---|---|---|
| `GET /v1/admin/dashboard` | `access_administration` | Counts and states, each read from the owning system |
| `GET /v1/admin/permissions` | `access_administration` | The role/permission tables, from `app/auth/permissions.py` |
| `GET /v1/admin/integrations` | `access_administration` | Which integrations are configured. Status only |
| `GET /v1/admin/configuration` | `access_administration` | What is configuration, and what a browser may change (none of it) |
| `GET /v1/admin/contract` | `access_administration` | What this surface may do, and what it cannot |
| `GET /v1/admin/users/search` | `access_administration` | Paginated, searchable, sortable |
| `GET /v1/admin/users/{id}` | `access_administration` | One user, with accounts and bots |
| `GET /v1/admin/users/{id}/sessions` | `access_administration` | Sessions, without tokens |
| `POST /v1/admin/users/{id}/deactivate` | `manage_users` | Access off. Every record kept |
| `POST /v1/admin/users/{id}/activate` | `manage_users` | Access on |
| `POST /v1/admin/users/{id}/revoke-sessions` | `manage_users` | Force re-authentication |
| `GET /v1/admin/audit-logs` | `access_administration` | Filterable by actor, action, resource, environment, date |
| `GET /v1/admin/audit-logs/actions` | `access_administration` | Action counts, plus the retention and immutability position |

**Two permissions, not one** (§5, least privilege). Reading the panel is
`access_administration`; changing somebody's access is `manage_users`. An
administrator who needs to look at the dashboard does not need to be able to
lock somebody out.

---

## 4. Dangerous actions cost something

Section 30. Every write takes:

* **a reason** of at least 8 characters, enforced by the schema and recorded in
  the audit trail — read by whoever asks, months later, why this happened;
* **a confirmation phrase** that is the subject's own email address, typed
  exactly. Not a yes/no prompt: *a confirmation you can click without reading is
  not a confirmation*, and a misdirected request fails rather than succeeding
  against the wrong row.

Plus three refusals:

* an administrator **cannot deactivate their own account** — the action wants a
  second person's name on it;
* the **last active administrator cannot be deactivated** — the same reasoning
  L04 already applied to demotion: a platform with no administrator cannot be
  administered back;
* deactivating an already-inactive user is a **409**, not a silent no-op.

---

## 5. Deactivation preserves everything

Section 13. Deactivation sets `is_active = false` and revokes live sessions. It
does **not** delete trades, journal entries, analytics, strategies, accounts or
bots, and a test asserts each survives.

**If the user holds open positions or enabled bots, that is reported and not
acted on**:

> this user still has 1 open position(s) and 1 enabled bot(s). Deactivating them
> does NOT close a position or stop a bot — nothing here liquidates anything.

A test seeds exactly that state, deactivates, and asserts the position is still
`open` and the bot still enabled. Closing somebody's position is a trading
decision; section 13 says not to invent emergency liquidation behaviour, and
this surface does not make trading decisions at all.

---

## 6. The audit trail

**Extended, not duplicated.** Section 31 asks for `admin_audit_logs`; section 51
says not to create duplicates. `audit_logs` has carried who, what, which
resource, when, from where and the request id since L04 and is read by the API
since L06 — so it **is** the admin audit trail, and migration `0025_admin_audit`
extends it rather than shadowing it with a second table. A platform with two
audit tables has two partial answers to "what happened".

Added: an `environment` column (section 35 lists it as a *filter*, and a key
inside JSON is not one), an index on `resource_type`, and two composite indexes
`(actor_user_id, occurred_at)` and `(action, occurred_at)`.

`audit.record_admin` requires the reason **in its signature** rather than by
convention, and writes `before` and `after` state through the same `_scrub` as
everything else — so a state snapshot cannot carry a password, hash, token or
credential into the table even if a caller assembled one carelessly. A test
asserts `_scrub` removes exactly that from a before/after shape.

Six new action names rather than `admin_action` for everything, because §33 asks
the trail to say what happened and "admin_action with the detail in a blob" is
"bot changed" wearing a different hat.

**Immutability** (§32): there is no DELETE and no UPDATE against `audit_logs`
anywhere in the application, and a test greps every module to keep it that way.
An administrator cannot erase their own trail through the API they administer.

**No `severity` column**, and section 35 lists one. Declined with a reason: an
audit row records that something happened; grading how bad it was is a
monitoring judgement, it would be assigned by the same code that writes the row,
and a field that is uniformly "info" is not a filter anybody can trust.

---

## 7. Feature flags: refused, with the reason

Section 29 says to create a controlled feature-flag system *if genuinely
required*. It is not, and L36 did not.

This platform's flags are `LIVE_GATES` — **code, not configuration**, each entry
flipping only in the level that builds the mechanism and proves it, in a
reviewed pass — and settings, which are **environment, not database**. A
database-backed flag table is a way to change behaviour without a deployment or
a review, which is exactly the property the gate table exists to prevent.

`GET /v1/admin/configuration` says so, reports `runtime_editable: []`, and lists
every gate with whether it is built. There is no field anywhere in the panel
that edits configuration, and a frontend test asserts the page renders zero
input elements on that tab. Section 28 forbids a browser field that accepts
`DATABASE_URL`; the safest implementation of that is no field at all.

---

## 8. Security

* **Backend authorization, always.** A test walks every admin GET route as an
  unauthenticated caller (401), a plain user (403) and a trader (403), and
  asserts a trader's deactivate attempt is refused *and* left the target
  untouched.
* **CSRF** covers the new state-changing routes through the existing
  double-submit middleware; a test asserts a POST without the header is 403.
* **Rate limiting**: 20 writes per minute per administrator, through the
  existing limiter.
* **No secret is returned by any route.** A test builds an app configured with
  an SMTP password, a TradingView secret and a Discord webhook, walks every
  admin GET route, and asserts none of them — nor the database URL, nor the
  Redis URL — appears in any response.
* **No hash, ever.** `UserOut` and the admin's own row shapes are built from
  named fields, so a column added tomorrow does not ship the hash.
* **Sessions without tokens**: the browser holds the token, the row holds its
  SHA-256, and neither is returned.
* **MFA does not exist.** Section 54 says to document it as a gap rather than
  pretend; `GET /v1/admin/permissions` and `/contract` both say `NOT
  IMPLEMENTED` in as many words, and the panel renders it.
* **No impersonation.** Section 56 says not to implement it unless the product
  requires it. It does not, and there is none.

---

## 9. Frontend

`/admin`, five tabs, reusing the existing shell, design system, API client and
auth hooks.

* **The environment banner is first and unmissable** (§8). Paper renders neutral;
  a live configuration renders with a critical border. The list of gates that
  are not built is expandable, so "live is off" arrives with its reasons.
* **Dangerous controls are visually distinct** and gated by a dialog that stays
  disabled until the reason is long enough and the email matches exactly. The
  backend enforces both regardless; the dialog makes the requirement visible.
* **Empty is empty** (§41, §69). No seeded users, no sample bots, no example
  audit rows.
* **The overview names where each control lives** and links to it, rather than
  offering a button that would be a second door.
* `/admin` is `partial` in `nav.ts`: the panel is real; the trading controls it
  points at live elsewhere on purpose.

---

## 10. Tests

`backend/tests/test_admin.py`, 40 tests. `frontend/src/components/AdminPanel.test.tsx`, 19.

| Test | Section |
|---|---|
| `test_the_admin_surface_cannot_reach_anything_that_trades` | 10, 49, 66 |
| `test_there_is_no_route_that_enables_live_trading` | 9, 66 |
| `test_no_route_deletes_or_edits_an_audit_row` | 32 |
| `test_the_admin_router_writes_only_user_access` | 10 |
| `test_every_admin_route_refuses_a_plain_user_and_a_trader` | 37, 63, 65 |
| `test_a_state_changing_admin_route_needs_csrf` | 46 |
| `test_a_dangerous_action_needs_a_reason_and_the_right_phrase` | 30 |
| `test_an_admin_cannot_deactivate_themselves` | 6 |
| `test_the_last_administrator_cannot_be_deactivated` | 12 |
| `test_deactivating_a_user_deletes_nothing` | 13 |
| `test_an_open_position_is_reported_and_never_liquidated` | 13 |
| `test_an_administrative_write_is_audited_with_a_reason_and_before_after` | 31, 33, 34, 67 |
| `test_no_admin_route_returns_a_secret` | 57, 65 |
| `test_an_empty_platform_reports_zeros_not_examples` | 41, 69 |
| `test_no_configuration_is_editable_from_a_browser` | 28, 29 |
| `test_the_live_gates_are_reported_and_all_closed` | 8, 9, 41 |
| `test_mfa_is_recorded_as_a_gap_rather_than_implied` | 54 |
| `test_user_search_rejects_an_unknown_sort_field` | 42, 61 |

---

## 11. Known limitations

1. **No MFA and no step-up re-authentication.** An admin session is a password
   and a cookie. Documented in the API, the panel and here.
2. **No impersonation**, deliberately (§56).
3. **No bulk actions**, deliberately (§44). Individual controlled operations
   only.
4. **No audit retention policy.** Nothing in this platform deletes on a timer,
   and §52 asks for a documented policy; the numbers are an operator decision
   that has not been made. The endpoint says so.
5. **Admin sessions have the same lifetime as any other** (§55). The platform
   has one `SESSION_TTL_HOURS`; a shorter privileged lifetime would need a
   second session policy, which is a real change to L04 rather than a setting.
6. **Object-level scoping for admins is not implemented** (§38). Every
   administrator sees every user; this platform has no tenancy or team model to
   scope against, and inventing one here would be inventing a product decision.
