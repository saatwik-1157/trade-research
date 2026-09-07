# SECURITY.md

Security posture of the existing project and the controls the platform adds.
Updated at every level that touches authentication, secrets, execution
authority or data exposure.

## Current controls (audit of 2026-09-02)

| Control | Where | Status |
|---|---|---|
| Real-account refusal in code | `mt5_paper.assert_demo` — `trade_mode` must be 0; CONTEST, REAL and unknown values abort before an order is built | present, tested against all four cases |
| Dry-run by default | `--live` required on `mt5_paper.py`, `take_profit.py`; `run_overnight.py --paper` disables sending | present |
| Order ownership | every order carries `MAGIC = 770315`; `close_own` touches only those | present |
| Algo-trading switch required for live | `assert_demo(live=True)` checks `terminal_info().trade_allowed` | present |
| Loss and exposure caps | `--max-positions`, one per symbol, `--max-daily-loss` on the server day | present, inline in `cycle()` |
| Sizing refusal | `lot_for_risk` refuses on missing tick value/size; reports when min lot exceeds budget | present, tested |
| Webhook auth | shared secret in body, constant-time compare | present; unset secret allowed with a loud warning |
| Webhook secret redaction | stripped from keys and string values at any depth before logging | present |
| Webhook exposure | binds `127.0.0.1`; optional TradingView IP allowlist; 64 KB body cap | present |
| Account data out of git | `data/` and `reports/` gitignored; `reports/mt5_account.json` holds login, server, balance | present |
| Secrets in env only | `SEC_USER_AGENT`, `TV_WEBHOOK_SECRET` | present; no `.env` file exists |

## Authentication and authorization (L04)

| Control | Implementation |
|---|---|
| Password hashing | Argon2id (`argon2-cffi`), `MIN_PASSWORD_LENGTH = 10`; unknown emails burn a real verification so timing does not reveal existence |
| Sessions | server-side rows (`auth_sessions`): random 256-bit token in an HttpOnly, SameSite=Lax cookie; only the SHA-256 of the token is stored; 12 h TTL; revoked on logout; `Secure` in production (`COOKIE_SECURE` overrides) |
| Roles | USER (read results) < TRADER (brokers, strategies, bots, orders, AI models) < ADMIN (users, roles, platform controls); table in `backend/app/auth/permissions.py`, mirrored in `frontend/src/lib/nav.ts` |
| Protected routes | `/brokers`, `/strategies`, `/bots`, `/orders`, `/ai/models` require TRADER and answer 501 until built; `/admin/*` requires ADMIN; anonymous is 401, insufficient role is 403 |
| Admin bootstrap | registration never yields ADMIN; `python -m app.auth.bootstrap --email ...` with `BOOTSTRAP_ADMIN_PASSWORD` in the environment; the last active admin cannot be demoted |
| Registration | open by default in development (`ALLOW_REGISTRATION=true`); set false for a closed instance |
| Frontend | the browser never sees the token; `RouteGuard` redirects and gates by role for usability only, the API is the boundary; `next=` redirects accept same-origin paths only |
| Secrets | password hashes never leave the service layer; `UserOut` is built from named fields; no credential appears in logs |

### Closed at the L04 hardening pass (2026-09-02)

| Control | Implementation |
|---|---|
| Rate limiting | Fixed window on login (10/5min), registration and password reset (5/hour). Two counters per attempt, one keyed on the client address and one on the submitted identifier, so neither spraying many accounts from one address nor one account from many addresses goes uncounted. In-process by default; `RATE_LIMIT_SHARED=true` moves it to Redis, which a multi-worker deployment needs |
| CSRF | Double-submit: a readable `tr_csrf` cookie beside the HttpOnly session, echoed in `X-CSRF-Token`. Enforced on every state-changing request that carries a session. Session-*establishing* endpoints (login, register, reset) are exempt by an explicit list, because requiring a token there refuses legitimate sign-ins while a stale cookie is present; they are rate limited instead |
| Audit logging | `audit_logs` is written for login, failed login, logout, registration, role change, reset request, reset completion and rate-limit trips. The writer scrubs passwords, hashes, tokens, keys and credentials at any depth **before** storage, so a careless call site cannot leak one |
| Password reset | 256-bit token, SHA-256 stored, one hour expiry, single use, and completing a reset revokes every session the user holds. The token is never returned in a response and never logged. Requests answer 202 identically for known and unknown addresses, so the endpoint is not a user-enumeration oracle |
| User record | `updated_at` and `last_login_at`. Last login is written on success only, so a failed attempt cannot move it |
| Permissions | 15 named permissions across three roles, cumulative. A test asserts a plain USER holds none of the seven dangerous ones |

Still deferred, recorded rather than hidden: **reset delivery** (no email or
messaging channel exists; the port refuses rather than pretending, so a user
cannot currently complete a reset), email verification, session listing and
remote revoke in the UI, and login-CSRF protection for the exempt endpoints.

## Known gaps

1. **No replay protection** on the webhook; a captured alert can be resent.
2. **Weak-auth fallback:** a plain-text alert containing the secret anywhere
   authenticates. Acceptable for a log; must not reach execution.
3. **No idempotency** on orders; a duplicate intent is a duplicate order.
4. **Unknown execution status is treated as rejection** in `place()`.
5. **No kill switch** beyond Ctrl+C and the daily-loss cap.
6. ~~No API auth~~ — sessions and roles landed at L04; rate limiting and
   CSRF tokens for trading mutations remain open.
7. **No secrets policy document** beyond "use env vars".
8. **Global site-packages** shadow `urllib3` with `urllib3-future`; the
   documented fix (virtualenv) is not in use on the development machine.

## Platform rules (from `ARCHITECTURE.md` §3)

- `TRADING_MODE=paper`, `LIVE_TRADING=false` by default; never auto-enabled.
- Risk Engine veto; AI cannot bypass it; TradingView cannot reach the broker.
- Reconcile before retry; stop-alert-reconnect-reconcile-resume on disconnect;
  reconcile on restart.
- No credentials in frontend code, logs, or committed files. The frontend
  talks only to the API; the API talks to the broker worker.
- Every fake adapter labels its output as fake and the label reaches the
  journal.

## Level assignments

| Gap | Level |
|---|---|
| replay protection, weak-auth tagging | L09, L39 |
| order idempotency, unknown-status reconciliation | L19 |
| kill switch, exposure caps | L17 |
| API auth | L04, L06 |
| secrets policy, venv, dependency hygiene | L02, L39 |
| disconnect / restart reconciliation | L20, L38 |


## The AI layer's surface (L27)

Configuring how a strategy uses AI is the one place a caller supplies input that
a running bot will act on, so it is the one place worth naming explicitly.

**What a caller can set:** a mode (enum), a failure policy (enum), a scoring
method (enum), a list of `{key, version}` model references, and numeric
thresholds — each range-checked twice, by the request schema and by a CHECK
constraint on the table.

**What a caller cannot set, and there is no field for:** a model path, a file
name, a URL, a Python expression, a formula, a serialized object, or a version
that has not been validated. `scoring_method` is an enum rather than a string
precisely because an arbitrary formula is arbitrary code with extra steps.

The stored model list is user-supplied data, so `_requirements()` refuses
anything that is not a `{key: str, version: str}` object rather than coercing
it. A test feeds it `{"key": "../../etc/passwd", "version": {"exec": "..."}}`
and asserts the refusal.

**Authorization:** every route under `/v1/ai` requires `manage_ai_models`.
Changing a strategy's AI configuration changes what a running bot does, so it is
held to the same permission as training and validation rather than to a general
write permission.

**No secret can reach an AI surface.** The AI layer holds no credential, and
`AiDecision`, `AiVerdict` and the journal row carry only a model key, a version,
a feature version, numbers and prose. L24's `_contains_secret` already refuses a
credential-shaped key in a model artifact, and a validation report is asserted
to carry none.


## Model artifacts (L28)

§33 treats a model artifact as an untrusted input, and most of that concern is
answered here by construction rather than by validation.

**The artifact is structured JSON on the model version row.** L24 chose that:
these parameters are a handful of floats, and keeping them structured means a
version can be inspected, diffed and queried. The consequences for security:

* **Nothing is deserialised.** No pickle, no joblib, no `torch.load`, no
  `__reduce__`, no code path that turns bytes into behaviour. An artifact is
  parsed as JSON and read field by field into a frozen dataclass, and an
  unrecognised `kind` is refused rather than guessed at.
* **There is no upload route and no path field.** A caller cannot name a file, a
  URL or a directory, so there is nothing for a path traversal to traverse. The
  only way an artifact enters this platform is a training run. A test asserts no
  route path contains `upload`.
* **An artifact is data, never code.** A metadata field that executed something
  would need something to execute it, and nothing does.

**Integrity is not free, and is where the work is.** A JSON column can still be
edited by anything holding a database connection — a migration, a console, a bug
— and a model whose coefficients changed under it would keep predicting,
confidently and wrongly. So a `sha256` digest over the canonical encoding of the
artifact is taken at registration and re-checked before every load and on every
cache hit. A mismatch refuses the load; §7 says a model whose integrity fails is
not loaded for trading.

**A version with no recorded digest is reported as unverifiable, not as
verified.** "Never checked" and "checked and fine" must not look the same, and a
CHECK constraint stops a registered version existing without one.

**Authorization.** Reads, registration and paper deployment require
`manage_ai_models` (trader). Promotion, rollback, emergency stop and retirement
require `promote_ai_models`, which only an administrator has — the line between
producing a candidate and deciding it is the version a scope resolves to.

**No secret can reach a registry surface.** The registry holds no credential,
and a version row carries a key, a version, digests, numbers and prose. L24's
`_contains_secret` already refuses a credential-shaped key in a model artifact
at registration time.

**The `model` realtime channel is gated at TRADER**, matching the REST surface.
A channel readable by someone the API would refuse is a way around the API.

## The portfolio surface (L30)

Fourteen routes, **every one a GET**. A parsed test asserts the router registers
no other verb, because a portfolio that could act would be an execution path
wearing a reporting name.

**Ownership is scoped in the query.** `WHERE user_id = :me` on both account
tables, not a fetch-then-filter. A filter written later can have a bug; a `WHERE`
clause cannot leak through one.

**A missing account and somebody else's give the identical 404.** Distinguishing
them makes the route a membership oracle — the same reasoning `channels.py`
already records for realtime subscriptions, and the same message.

**There is no credential to redact.** `AccountState` has no `login`, `password`,
`api_key` or `token` field, and `from_broker_account` copies the broker's
balance, equity, margin and server name and nothing else — the adapter's own
`Account` carries a `login` and it is never read. Tests assert that neither the
router nor any module in `app/portfolio/` references any of those names, and that
no route response contains them.

**Nothing here can enable live trading.** Parsed: no module references
`LIVE_TRADING`, `live_trading`, `LIVE_GATES` or `live_execution_allowed`, and
none imports `app.core.settings`. `TRADING_MODE=paper` and `LIVE_TRADING=false`
remain the defaults; L30 does not touch either.

**Nothing here can execute.** Parsed: no import of `app.oms`, `app.orders`,
`app.sizing`, `app.execution`, `app.brokers`, `app.risk.engine`,
`app.risk.service`, `app.positions.manager`, `app.positions.executor` or
`app.positions.reconciler`; and no reference by name to `place`, `place_order`,
`submit_order`, `cancel_order`, `modify_order`, `close_position`,
`modify_position`, `open_position`, `send_order` or `order_send`.

**Nothing here deletes.** Parsed: no reference to `delete`, `drop_all`,
`drop_table`, `truncate` or `execute`. The one write this engine performs is an
INSERT into `portfolio_snapshots`, and it is skipped entirely when balance or
equity could not be read.

**Realtime payloads carry no credential.** The four portfolio event types are
account-scoped, so `channels.py` authorizes them with the rule it already had.
Their payloads are money, counts and states.

## The trade journal surface (L31)

**Every route is a GET.** Eight of them on `/v1/trades`, all behind
`Permission.view_journal` or `Permission.view_portfolio`.

**No webhook payload is ever served.** The TradingView block carries the
provider, the event id and the auth strength; the payload stays in
`webhook_events`, because a provider payload can contain a shared secret. A
behavioural test seeds a secret into a payload and asserts it reaches neither the
timeline nor the context.

**The export header carries no credential-shaped column.** No login, no payload,
no secret; a test asserts it.

**Nothing here can execute.** Parsed with `ast` across `app/journal/*`, the
router and the query module: no import of `app.oms`, `app.orders`, `app.sizing`,
`app.execution`, `app.brokers`, `app.risk.engine`, `app.risk.service`, any
position manager, executor or reconciler, or `MetaTrader5`; and no reference by
name to `place`, `place_order`, `submit_order`, `cancel_order`, `modify_order`,
`close_position`, `modify_position`, `open_position`, `send_order` or
`order_send`.

**Nothing here can enable live trading.** No reference to `LIVE_TRADING`,
`live_trading`, `LIVE_GATES` or `live_execution_allowed`; no import of
`app.core.settings`.

**Nothing here deletes.** No reference to `delete`, `drop_all`, `drop_table` or
`truncate`. The only writes are an INSERT into `trades` and an UPDATE of
`status` and `data_quality` on a reconciliation -- which never touches a price, a
quantity or a P&L, because those are historical facts.

**No secret name is referenced at all**: `password`, `api_key`, `secret`,
`webhook_secret` and `credential` appear nowhere in the journal modules.

**Every filter value is checked against an allow-list** where one exists, and
every identifier is bound as a parameter. An unknown `exit_reason` is a 422
rather than a silently ignored filter -- a filter that stops applying returns the
wrong set as though it were the right one.

## The analytics surface (L32)

**Eleven routes, every one a GET**, behind `Permission.view_analytics`. A parsed
test asserts the router registers no other verb.

**The package contains no write verb at all.** Parsed across `app/analytics/*`
and the router: no `add`, `commit`, `delete`, `flush`, `drop_all`, `truncate` or
`merge`. This is stronger than "it does not place orders" and deliberately so --
a read-only module that acquired a write would become an owner of state, and the
whole separation (the portfolio owns account state, the journal owns trade
history) depends on analytics never being one.

**Account ownership is scoped in the query.** `WHERE user_id = :me` on both
account tables, and a missing account and somebody else's give the identical 404.
A backtest belonging to another user is the same 404.

**An unknown filter value is a 422, never ignored.** An unknown symbol, an
unknown dimension, an unknown bucket, an unknown period and a reversed window are
all refused. A filter that silently stops applying returns the wrong set as
though it were the right one -- which is a data-exposure bug as much as a
correctness one, because the wrong set may span accounts the caller did not ask
about.

**Nothing here can execute.** No import of `app.oms`, `app.orders`, `app.sizing`,
`app.execution`, `app.brokers`, `app.risk.engine`, `app.risk.service`, any
position manager, executor or reconciler, or `MetaTrader5`; and no reference by
name to any order-placing verb.

**Nothing here can enable live trading.** No reference to `LIVE_TRADING`,
`live_trading`, `LIVE_GATES` or `live_execution_allowed`; no import of
`app.core.settings`.

**No secret name is referenced**: `password`, `api_key`, `secret`, `credential`
and `login` appear nowhere in the package, and a test asserts no response body
contains them.

## The trade review surface (L33)

**Eight routes.** Reading is gated at `view_journal`; regenerating and sweeping
at `manage_ai_models`, because both spend provider budget and section 47 asks
that reviews not be generated without a ceiling.

**Access follows the trade.** A review is readable exactly by whoever may read
its trade, checked in the query. Somebody else's trade and a missing trade give
the identical 404.

**Nothing leaves this process.** There is no LLM provider in this platform, so
nothing is sent anywhere today. When one is added, the payload it receives is
`ReviewInput.as_dict()` -- prices, quantities, identifiers and recorded
decisions. Two tests hold that line: one asserts no credential name appears in a
serialised input, the other that neither context dataclass has a field that
could hold one.

**Nothing here can execute.** Parsed with `ast` across `app/review/*` and the
router: no import of `app.oms`, `app.orders`, `app.sizing`, `app.execution`,
`app.brokers`, `app.risk.engine`, `app.risk.service`, any position
manager/executor/reconciler, or `MetaTrader5`; and no reference by name to any
order-placing verb.

**Nothing here can replace a model.** `app.ai.registry_service` is never
imported, so `promote`, `rollback` and `retire` are unreachable -- section 1's
"no automatic model replacement" is a property of the import graph.

**Nothing here can enable live trading**, and nothing here deletes: no
`LIVE_TRADING`, `live_trading`, `LIVE_GATES`, `live_execution_allowed`,
`app.core.settings`, `delete`, `drop_all`, `drop_table` or `truncate`.

**No secret name is referenced at all**: `password`, `api_key`, `secret`,
`credential` and `auth_token` appear nowhere in the package, and section 58's
logging rule is satisfied by there being nothing to log.

---

# Level 39 — the security pass (2026-09-05)

Full audit, then the controls the audit found genuinely missing. What matters
most about this section is the second half: **what is still absent**, stated
plainly, because a security document that lists only what exists reads as a
claim that nothing is missing.

## What the audit found already correct

Verified by reading the source, not by trusting the table above. Each of these
was a candidate for "add a control" and each was left alone:

| Area | Finding |
|---|---|
| Code execution | **CLEAN.** No `eval`, `exec`, `subprocess`, `os.system`, `__import__`, `pickle.load`, `yaml.load(` or `shell=True` anywhere in `backend/app`. |
| SQL injection | **CLEAN.** No f-string SQL. Every `text()` is a static index predicate plus one `text("SELECT 1")` health probe. |
| Frontend | **CLEAN.** No `dangerouslySetInnerHTML`, `innerHTML`, `eval`, `document.write`, `localStorage` or `sessionStorage` in `frontend/src`. |
| File upload | **NOT APPLICABLE.** No `UploadFile`, `multipart` or `File(` exists. There is no upload surface to harden. |
| Container | **GOOD.** `python:3.12-slim`; `useradd --uid 10001 app`, `chown -R app:app /srv`, `USER app`. Every Compose port binds `127.0.0.1`. |
| Secrets in the tree | **CLEAN.** No `.env` file exists. `git ls-files` shows no tracked `.env`, `.pem`, `.key`, credential or secret file. No private-key header, AWS `AKIA`, GitHub `ghp_` or Slack `xox` pattern. The only grep hit was a guard function's name. |
| Webhook | **GOOD.** Constant-time secret compare, 120-second replay window, idempotency key, optional IP allowlist, 64 KB cap, secret redaction at any depth, 120/60s rate limit per IP. |
| Broker credentials | **NONE STORED.** There is nothing to encrypt, because the platform holds no broker credential anywhere. The requirement lands with the first adapter that needs one. |

**No real credential was found, so none is reported and none needs rotating.**

## What was added

### Response security headers — there were none at all

Not a weak policy; none. `app/security/headers.py`, registered as middleware
*outside* the CSRF middleware so a 403 and a 500 carry them too — which are the
responses an attacker sees most.

`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri
'none'; form-action 'none'` (the API answers JSON, so total denial is correct),
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
no-referrer`, a `Permissions-Policy` denying thirteen browser features,
`Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy`, `Cache-Control:
no-store`.

**HSTS is production-only, deliberately.** Sending it from a development server
pins the browser to HTTPS for `127.0.0.1` for a year and breaks every other
local project on the machine, long after this one is deleted.

The CSP here is for the *API's* responses. The rendered frontend needs a policy
that permits its own scripts; that belongs to the proxy.

### CORS — a wildcard with credentials is now refused at startup

`allow_credentials=True` with `allow_origins=["*"]` makes any website an
authenticated caller: the browser sends the session cookie and Starlette
reflects the origin back. The settings validator refuses `*` and `null`, and
refuses any entry that is not a bare origin — **a value carrying a path matches
nothing and does so silently**, which is the failure mode worth refusing
because the symptom appears long after the deploy that caused it.

`allow_headers=["*"]` was replaced with the seven headers the frontend actually
sends.

### Step-up re-authentication — the gap L36 and L38 both named

Both levels wrote, in their own documents, that the administrative session
releasing safe mode was *"a password and a cookie"*. `app/security/stepup.py`
closes it for four action classes:

| Scope | Route |
|---|---|
| `ADMIN_USER_ACCESS` | deactivate / activate a user |
| `ADMIN_SESSION_REVOKE` | revoke another user's sessions |
| `SAFE_MODE_EXIT` | `POST /v1/recovery/safe-mode/exit` |
| `KILL_SWITCH` | `DELETE /v1/paper-trading/kill-switch` |

A grant is **scoped**, **bound to the subject**, **bound to the session**,
**valid 300 seconds** and **spent by one use**. Five wrong attempts lock the
scope for fifteen minutes; the endpoint is rate limited at 5/300s on top,
because a route that verifies a password is a password oracle if it is not
counted. A wrong password and a missing grant return identical wording.

**This is not MFA and the platform never calls it that.** It asks for the same
password again. It raises the bar for a stolen session cookie and does nothing
about a stolen password.

Ordering matters and cost a fix: the gate runs **after** every deterministic
refusal. `admin_service.check_set_active` was extracted from `set_active` so
"you cannot deactivate yourself" and "this is the last administrator" are
answered before a single-use grant is spent — otherwise an operator burns a
confirmation on an action that could never succeed, having been shown "confirm
your password" for something forbidden whoever they are.

Only the *release* direction is gated. Entering safe mode and engaging a kill
switch stay free: a control equally expensive to set and to lift is one that
gets lifted by whoever is in a hurry, and one expensive to *set* is one nobody
engages in an emergency.

### A security event vocabulary — filling L34's empty category

L34 defined `Category.security` and gave it no rule, because no event type
existed to route. Two now do, and the split is an authorization decision:

* `SECURITY_ALERT` — `system` scope, to operators, carrying **a count and a
  class and never a subject**. The catalogue says a `system` event never
  carries private figures, and a security alert is the easiest place to break
  that: the natural sentence is *"12 failed logins for alice@example.com from
  203.0.113.9"*.
* `ACCOUNT_SECURITY_ALERT` — `user` scope, to the account's owner.

Payload keys are an **allow-list**, because a deny-list has to predict the name
of the field that leaks. Severity uses L34's own five words rather than a sixth
vocabulary, and may be raised but never lowered. Session identifiers are
SHA-256 fingerprints, twelve hex characters — enough to correlate two audit
rows, not enough to be a session.

### A per-user WebSocket connection cap

The hub capped subscriptions per connection (50) and frame size (4 KB). It
capped **nothing per account**, so one script with a valid session could open
sockets until the process ran out and take the live feed down for everyone —
the cheapest denial of service the platform had. `WS_MAX_CONNECTIONS_PER_USER`
defaults to 8; the socket is closed with 4429 and the refusal is counted.

Per user rather than per address on purpose: an address is shared by an office
and spoofed by an attacker, and the socket is already authenticated by the time
the cap applies.

### A security posture, in the existing dashboard

`app/security/posture.py` reports as **one more `ComponentHealth`** in L37's
stack, not a security dashboard beside the monitoring one. Every entry is read
from configuration or from a counter; nothing is asserted. A control this
deployment cannot determine reports `UNKNOWN`, never `HEALTHY`.

**There is no score.** A number invites "how do we get to ten", and the
cheapest answers to that question are controls that move a number rather than
controls that reduce risk.

## What is still missing

| Gap | Status |
|---|---|
| **Multi-factor authentication** | **NOT BUILT.** Step-up is a second prompt, not a second factor. TOTP needs enrolment, recovery codes and a lost-device path; half-building one and writing "MFA" on a checklist produces a control bypassed the first time it is inconvenient. |
| **Managed secret store** | Secrets come from the environment. A vault is a deployment decision this repository does not make. |
| **Intrusion detection** | Failed logins and refusals are counted and alerted on. There is no behavioural detection, and calling the counters one would be a claim the code does not support. |
| **Broker credential encryption** | Nothing to encrypt yet — no credential is stored. Lands with the first adapter. |
| **Login CSRF** | Still accepted, as L04 recorded. The four auth paths stay CSRF-exempt because requiring a token there breaks signing in while an old cookie is present. Rate limiting is the compensating control. |
| **Step-up grants are per process** | Same reasoning as L38's safe-mode latch: a five-minute credential surviving a restart is one nobody revoked. A second API process needs a shared store — a row and a lease. |

## Threat model, in one table

| Threat | Control | Residual |
|---|---|---|
| Stolen session cookie | HttpOnly, SameSite=Lax, server-side revocable, 12h TTL, **step-up on the four dangerous actions** | reads and ordinary writes are still reachable |
| Stolen password | Argon2id, rate limiting, lockout | **no second factor** — the top residual risk |
| Forged TradingView alert | constant-time secret, 120s window, idempotency key, IP allowlist | a leaked secret is full webhook access until rotated |
| Replayed alert | idempotency key + unique constraints | none known |
| Cross-user data access | per-request RBAC, per-subscribe channel authorization | none known; asserted by test |
| Malicious origin | explicit CORS list, wildcard refused at startup | none known |
| XSS in a JSON response | `default-src 'none'`, nosniff | frontend policy belongs to the proxy |
| Clickjacking | `X-Frame-Options: DENY` + `frame-ancestors 'none'` | none known |
| Socket exhaustion | 8 per user, 50 subscriptions, 4 KB frames, idle close | a distributed set of accounts |
| Accidental live trading | `LIVE_TRADING=false`, all 11 `LIVE_GATES` False, no live adapter exists | none — three independent gates |
| Secret in a log | scrubber at any depth, allow-listed event payloads, no route returns one | asserted by test, not by discipline |

## Runbook

**Suspected session compromise** — `POST /v1/admin/users/{id}/revoke-sessions`
(needs a reason, the user's email, and a step-up). The user is signed out
everywhere; their account stays active and their records are untouched.

**Suspected webhook secret leak** — rotate `TV_WEBHOOK_SECRET` and restart. An
unset secret refuses every alert, which is the safe intermediate state.

**Suspected credential compromise, unknown blast radius** — `POST
/v1/recovery/safe-mode/enter` first: it needs no step-up, blocks new orders at
three server-side paths, and blocks nothing observational. Then investigate
with `GET /v1/security/events`. Leaving safe mode re-runs the whole startup
sequence and needs the password again.

**Never** rotate a secret by editing a running container's environment without
a restart; the process read it at startup.

## Verification

`backend/tests/test_security.py`, 44 tests. The structural ones assert
properties of the source rather than of a run, because a convention is
forgotten and a signature is not: the security package imports no risk engine,
OMS, adapter, sizer or position manager; no module in it grants a permission or
changes a role; no module in it names a credential; the security router has
exactly one write route (the step-up confirmation, which changes no control);
and no module anywhere in `app/` disables a security control at runtime.
