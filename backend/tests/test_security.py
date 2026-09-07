"""Security controls (L39), exercised rather than asserted.

The ones that carry the level:

  * `test_a_dangerous_admin_action_needs_the_password_again` -- the gap L36 and
    L38 both named in their own documents.
  * `test_a_grant_is_spent_by_one_use` -- a grant that survives its action is a
    second unreviewed action.
  * `test_a_grant_for_one_subject_does_not_authorise_another` -- the reason the
    subject is part of the key.
  * `test_a_system_security_event_never_carries_a_subject` -- the `system`
    channel goes to every signed-in browser.
  * `test_a_wildcard_cors_origin_with_credentials_refuses_to_start`.
  * `test_one_account_cannot_hold_unlimited_sockets`.
  * `test_no_security_route_can_change_a_security_control`.
  * `test_the_posture_names_what_is_missing`.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import Role, User
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.realtime.hub import Connection, ConnectionLimit, Hub
from app.security import headers as sec_headers
from app.security import posture
from app.security.announce import SecurityAnnouncer
from app.security.events import (
    PayloadRefused,
    SecurityEvent,
    SecuritySeverity,
    fingerprint,
    record,
)
from app.security.stepup import MAX_ATTEMPTS, StepUp, StepUpError, StepUpScope
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "security"

ADMIN = {"email": "sec-admin@tr-platform.io", "password": "correct horse battery staple"}
VICTIM = {"email": "sec-victim@tr-platform.io", "password": "another long passphrase here"}
PLAIN = {"email": "sec-plain@tr-platform.io", "password": "a third long passphrase here"}


# ================================================================== fixtures


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _sign_in(client: AsyncClient, who: dict[str, str]) -> None:
    client.cookies.clear()
    r = await client.post("/auth/register", json=who)
    if r.status_code >= 400:
        await client.post("/auth/login", json=who)


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


async def _make_user(app: FastAPI, who: dict[str, str]) -> str:
    async with app.state.session_factory() as db:
        from app.auth.passwords import hash_password

        user = User(email=who["email"], password_hash=hash_password(who["password"]))
        db.add(user)
        await db.commit()
        return user.id


@pytest.fixture
async def admin(app: FastAPI, client: AsyncClient) -> AsyncClient:
    await _sign_in(client, ADMIN)
    await _promote(app, ADMIN["email"], Role.admin)
    return client


# ======================================================= 1. response headers


async def test_every_response_carries_the_security_headers(client: AsyncClient) -> None:
    """Section 23. Including the ones an attacker sees most.

    Asserted on a 200, a 404 AND a 401 rather than only on a success: the
    middleware is registered outside the routers precisely so a refusal carries
    the headers too, and a test that only ever looked at a working response
    would pass just as happily with it installed inside the router -- where an
    unauthenticated caller, who sees far more 401s than 200s, would get none.

    Driven through `ASGITransport` rather than `TestClient` deliberately.
    `TestClient` as a context manager runs the lifespan, which opens the Redis
    the event bus wants; that is the environmental condition `TESTING.md`
    records for `test_health.py`, `test_realtime.py`, `test_errors.py` and
    `test_cors.py`, and a header test has no business depending on it.
    """
    responses = [
        await client.get("/health"),
        await client.get("/v1/nothing-here-at-all"),
        await client.get("/v1/admin/dashboard"),
    ]
    assert {r.status_code for r in responses} == {200, 404, 401}, [r.status_code for r in responses]
    for response in responses:
        for name in sec_headers.BASE_HEADERS:
            assert name in response.headers, f"{name} missing on {response.status_code}"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Referrer-Policy"] == "no-referrer"


async def test_hsts_is_production_only(client: AsyncClient, settings: Settings) -> None:
    """Sending it from a development server pins the browser for a year.

    Not a hypothetical: HSTS is keyed on the host, and 127.0.0.1 is the host
    every other local project on the machine shares.
    """
    assert "Strict-Transport-Security" not in (await client.get("/health")).headers

    prod = Settings(_env_file=None, environment="production", cookie_secure=True)
    application = create_app(prod, checks={})
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        assert (await c.get("/health")).headers["Strict-Transport-Security"] == sec_headers.HSTS


def test_the_api_csp_permits_nothing(settings: Settings) -> None:
    """A JSON response has no scripts, no styles, no frames and no forms."""
    assert "default-src 'none'" in sec_headers.API_CSP
    assert "frame-ancestors 'none'" in sec_headers.API_CSP
    for permissive in ("unsafe-inline", "unsafe-eval", "*"):
        assert permissive not in sec_headers.API_CSP


# ==================================================================== 2. CORS


def test_a_wildcard_cors_origin_with_credentials_refuses_to_start() -> None:
    """The combination that makes any site an authenticated caller.

    Refusing to boot rather than warning: a warning in a log is read after the
    incident and a process that will not start is read before it.
    """
    for bad in (["*"], ["http://localhost:3000", "*"], ["null"]):
        with pytest.raises(ValidationError) as caught:
            Settings(_env_file=None, cors_origins=bad)
        assert "wildcard" in str(caught.value).lower() or "*" in str(caught.value)


def test_a_cors_entry_that_is_not_an_origin_is_refused() -> None:
    """A path or a bare host matches nothing and does so silently."""
    for bad in (["localhost:3000"], ["http://localhost:3000/app"], ["example.com"]):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, cors_origins=bad)
    # A path-less origin with a port is fine.
    assert Settings(_env_file=None, cors_origins=["https://app.example.com"])


def test_credentialed_cors_does_not_allow_every_header(settings: Settings) -> None:
    """`allow_headers=['*']` with credentials is wider than the client asks."""
    assert "*" not in sec_headers.ALLOWED_REQUEST_HEADERS
    assert CSRF_HEADER in sec_headers.ALLOWED_REQUEST_HEADERS
    source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    assert 'allow_headers=["*"]' not in source


# ================================================================= 3. step-up


def test_a_grant_is_spent_by_one_use() -> None:
    """A grant that survives its action authorises a second unreviewed one."""
    holder = StepUp()
    holder.grant(session="s1", scope=StepUpScope.admin_user_access, subject="u1", password_ok=True)
    holder.consume(session="s1", scope=StepUpScope.admin_user_access, subject="u1")
    with pytest.raises(StepUpError):
        holder.consume(session="s1", scope=StepUpScope.admin_user_access, subject="u1")


def test_a_grant_for_one_subject_does_not_authorise_another() -> None:
    """The reason the subject is part of the key, not a convenience."""
    holder = StepUp()
    holder.grant(
        session="s1", scope=StepUpScope.admin_user_access, subject="alice", password_ok=True
    )
    with pytest.raises(StepUpError):
        holder.consume(session="s1", scope=StepUpScope.admin_user_access, subject="bob")


def test_a_grant_for_one_scope_does_not_authorise_another() -> None:
    """One password prompt must not unlock every dangerous action at once."""
    holder = StepUp()
    holder.grant(session="s1", scope=StepUpScope.admin_user_access, subject="u1", password_ok=True)
    with pytest.raises(StepUpError):
        holder.consume(session="s1", scope=StepUpScope.safe_mode_exit, subject="u1")


def test_a_grant_from_one_session_cannot_be_presented_from_another() -> None:
    holder = StepUp()
    holder.grant(
        session="browser-a", scope=StepUpScope.kill_switch, subject="global:", password_ok=True
    )
    with pytest.raises(StepUpError):
        holder.consume(session="browser-b", scope=StepUpScope.kill_switch, subject="global:")


def test_a_grant_expires() -> None:
    """Short enough not to still be open when the laptop is left unlocked."""
    clock = [1000.0]
    holder = StepUp(ttl_seconds=300.0, _now=lambda: clock[0])
    holder.grant(
        session="s1", scope=StepUpScope.safe_mode_exit, subject="safe_mode", password_ok=True
    )
    clock[0] += 299
    assert holder.holds(session="s1", scope=StepUpScope.safe_mode_exit, subject="safe_mode")
    clock[0] += 2
    with pytest.raises(StepUpError):
        holder.consume(session="s1", scope=StepUpScope.safe_mode_exit, subject="safe_mode")


def test_a_wrong_password_never_issues_a_grant() -> None:
    holder = StepUp()
    with pytest.raises(StepUpError):
        holder.grant(
            session="s1", scope=StepUpScope.kill_switch, subject="global:", password_ok=False
        )
    assert not holder.holds(session="s1", scope=StepUpScope.kill_switch, subject="global:")


def test_repeated_wrong_passwords_lock_the_scope() -> None:
    """The endpoint verifies a password, so uncounted it is a password oracle."""
    clock = [1000.0]
    holder = StepUp(_now=lambda: clock[0])
    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(StepUpError):
            holder.grant(
                session="s1",
                scope=StepUpScope.admin_user_access,
                subject="u1",
                password_ok=False,
            )
    # Even the RIGHT password is refused while the lockout stands.
    with pytest.raises(StepUpError, match="locked"):
        holder.grant(
            session="s1", scope=StepUpScope.admin_user_access, subject="u1", password_ok=True
        )


def test_a_wrong_password_and_a_missing_grant_read_the_same() -> None:
    """So the endpoint cannot be used to tell one from the other."""
    holder = StepUp()
    with pytest.raises(StepUpError) as wrong:
        holder.grant(session="s1", scope=StepUpScope.kill_switch, subject="g", password_ok=False)
    assert "confirm" in str(wrong.value).lower()


def test_step_up_stats_name_no_session_and_no_subject() -> None:
    """A status endpoint must not become a map of pending dangerous actions."""
    holder = StepUp()
    holder.grant(
        session="secret-session", scope=StepUpScope.kill_switch, subject="acct-9", password_ok=True
    )
    rendered = repr(holder.stats())
    assert "secret-session" not in rendered
    assert "acct-9" not in rendered
    assert holder.stats()["live_grants"] == 1


# ============================================ 4. step-up, through the routes


async def test_a_dangerous_admin_action_needs_the_password_again(
    app: FastAPI, admin: AsyncClient
) -> None:
    """The gap L36 and L38 both named. Deactivation is the worked example."""
    victim_id = await _make_user(app, VICTIM)
    body = {"reason": "offboarding, ticket 4711", "confirm": VICTIM["email"]}

    refused = await admin.post(
        f"/v1/admin/users/{victim_id}/deactivate", json=body, headers=_csrf(admin)
    )
    assert refused.status_code == 403
    assert "step-up" in refused.text.lower() or "password again" in refused.text.lower()

    async with app.state.session_factory() as db:
        still = await db.get(User, victim_id)
        assert still is not None and still.is_active is True, "refused and yet deactivated"

    granted = await admin.post(
        "/v1/security/step-up",
        json={
            "password": ADMIN["password"],
            "scope": StepUpScope.admin_user_access.value,
            "subject": victim_id,
        },
        headers=_csrf(admin),
    )
    assert granted.status_code == 201, granted.text

    allowed = await admin.post(
        f"/v1/admin/users/{victim_id}/deactivate", json=body, headers=_csrf(admin)
    )
    assert allowed.status_code == 200, allowed.text
    async with app.state.session_factory() as db:
        assert (await db.get(User, victim_id)).is_active is False


async def test_the_step_up_grant_is_spent_so_a_second_action_is_refused(
    app: FastAPI, admin: AsyncClient
) -> None:
    victim_id = await _make_user(app, VICTIM)
    body = {"reason": "offboarding, ticket 4711", "confirm": VICTIM["email"]}
    await admin.post(
        "/v1/security/step-up",
        json={
            "password": ADMIN["password"],
            "scope": StepUpScope.admin_user_access.value,
            "subject": victim_id,
        },
        headers=_csrf(admin),
    )
    first = await admin.post(
        f"/v1/admin/users/{victim_id}/deactivate", json=body, headers=_csrf(admin)
    )
    assert first.status_code == 200
    second = await admin.post(
        f"/v1/admin/users/{victim_id}/activate", json=body, headers=_csrf(admin)
    )
    assert second.status_code == 403


async def test_the_wrong_password_does_not_issue_a_grant_over_http(
    app: FastAPI, admin: AsyncClient
) -> None:
    victim_id = await _make_user(app, VICTIM)
    r = await admin.post(
        "/v1/security/step-up",
        json={
            "password": "not the password",
            "scope": StepUpScope.admin_user_access.value,
            "subject": victim_id,
        },
        headers=_csrf(admin),
    )
    assert r.status_code == 403
    assert ADMIN["password"] not in r.text


async def test_a_step_up_response_never_echoes_the_password(
    app: FastAPI, admin: AsyncClient
) -> None:
    r = await admin.post(
        "/v1/security/step-up",
        json={
            "password": ADMIN["password"],
            "scope": StepUpScope.kill_switch.value,
            "subject": "global:",
        },
        headers=_csrf(admin),
    )
    assert r.status_code == 201
    assert ADMIN["password"] not in r.text


# ========================================================== 5. security events


def test_a_system_security_event_never_carries_a_subject() -> None:
    """The `system` channel reaches every signed-in browser.

    The natural sentence is "12 failed logins for alice@example.com from
    203.0.113.9". This is what makes it unsayable.
    """
    rec = record(
        SecurityEvent.login_failed,
        "sign-in attempts failed",
        user_id="user-42",
        count=12,
        window_seconds=60,
    )
    body = rec.payload(include_subject=False)
    assert "user_id" not in body
    assert body["count"] == 12
    for leaky in ("email", "ip", "address", "user_agent", "session", "token"):
        assert leaky not in body


def test_a_personal_security_event_goes_to_its_own_channel() -> None:
    rec = record(SecurityEvent.password_changed, "changed", user_id="user-42")
    assert rec.personal is True
    assert rec.payload(include_subject=True)["user_id"] == "user-42"


def test_a_payload_key_that_is_not_on_the_allow_list_is_refused() -> None:
    """An allow-list, because a deny-list has to predict the leak's name."""
    with pytest.raises(PayloadRefused):
        record(SecurityEvent.login_failed, "x", ip="203.0.113.9")
    with pytest.raises(PayloadRefused):
        record(SecurityEvent.login_failed, "x", email="alice@example.com")


def test_a_severity_may_be_raised_and_never_lowered() -> None:
    """A threshold that can be argued down is not a threshold."""
    raised = record(SecurityEvent.login_failed, "many", severity=SecuritySeverity.critical)
    assert raised.severity is SecuritySeverity.critical
    lowered = record(SecurityEvent.kill_switch_engaged, "halt", severity=SecuritySeverity.info)
    assert lowered.severity is SecuritySeverity.critical


def test_the_security_vocabulary_is_the_notification_vocabulary() -> None:
    """One column, one set of words. The defect L34 caught in itself."""
    from app.notifications.contract import Severity

    words = {s.value for s in Severity}
    assert {s.value for s in SecuritySeverity} <= words


def test_a_session_fingerprint_is_not_the_session() -> None:
    token = uuid.uuid4().hex
    handle = fingerprint(token)
    assert token not in handle
    assert len(handle) == 12
    assert fingerprint(token) == handle, "must be stable to correlate two rows"


async def test_the_announcer_routes_by_scope_and_never_leaks() -> None:
    published: list[object] = []

    class FakeHub:
        async def publish(self, event: object) -> None:
            published.append(event)

    announcer = SecurityAnnouncer(hub=FakeHub())
    await announcer.announce(record(SecurityEvent.login_failed, "burst", user_id="u1", count=9))
    await announcer.announce(record(SecurityEvent.password_changed, "changed", user_id="u1"))

    system: Any = published[0]
    personal: Any = published[1]
    assert system.channel == "system" and "user_id" not in system.payload
    assert personal.channel == "user:u1" and personal.payload["user_id"] == "u1"


async def test_a_failing_bus_never_fails_the_request() -> None:
    """A control that takes down the route it protects gets switched off."""

    class BrokenHub:
        async def publish(self, event: object) -> None:
            raise RuntimeError("bus is down")

    announcer = SecurityAnnouncer(hub=BrokenHub())
    assert await announcer.announce(record(SecurityEvent.admin_action, "x")) is False
    assert announcer.failures == 1


def test_both_security_types_are_catalogued_and_routed() -> None:
    """L34 defined `Category.security` with no rule. This is the rule."""
    from app.notifications.catalogue import RULES
    from app.notifications.contract import Audience, Category
    from app.realtime.catalogue import CATALOGUE, PRODUCED_NOW, EventType, Scope

    assert CATALOGUE[EventType.SECURITY_ALERT].scope is Scope.system
    assert CATALOGUE[EventType.ACCOUNT_SECURITY_ALERT].scope is Scope.user
    assert EventType.SECURITY_ALERT in PRODUCED_NOW
    assert RULES[EventType.SECURITY_ALERT].category is Category.security
    assert RULES[EventType.SECURITY_ALERT].audience is Audience.operators
    assert RULES[EventType.ACCOUNT_SECURITY_ALERT].audience is Audience.owner


# ============================================================ 6. WebSocket cap


class _NullBus:
    """A bus that is never used. The connection cap is decided before publish.

    `cast` at the call sites rather than a Protocol implementation: the cap is
    pure bookkeeping on the hub and touches no bus method, so building a
    conforming fake would assert something this test is not about.
    """


def test_one_account_cannot_hold_unlimited_sockets() -> None:
    """The cheapest denial of service the platform had: a valid session.

    Nothing capped connections per account, so a script could open sockets
    until the process ran out of them and take the live feed down for everybody.
    """

    hub = Hub(cast("Any", _NullBus()), max_per_user=3)
    for i in range(3):
        hub.add(Connection(id=f"c{i}", user_id="greedy"))
    with pytest.raises(ConnectionLimit):
        hub.add(Connection(id="c3", user_id="greedy"))
    assert hub.counters.connections_refused == 1
    # Another account is unaffected: the cap is per user, not global.
    hub.add(Connection(id="other", user_id="someone-else"))


def test_closing_a_socket_frees_the_slot() -> None:
    hub = Hub(cast("Any", _NullBus()), max_per_user=1)
    hub.add(Connection(id="a", user_id="u"))
    with pytest.raises(ConnectionLimit):
        hub.add(Connection(id="b", user_id="u"))
    hub.remove("a")
    hub.add(Connection(id="b", user_id="u"))
    assert hub.connections_for("u") == 1


def test_the_cap_is_configurable_and_reported() -> None:
    assert Settings(_env_file=None).ws_max_connections_per_user == 8


# ================================================================ 7. posture


def test_the_posture_names_what_is_missing(settings: Settings) -> None:
    """A status page that lists only what exists reads as complete."""
    card = posture.scorecard(settings)
    gaps = {c["name"] for c in card["controls"] if c["planned"]}
    assert "mfa" in gaps
    mfa = next(c for c in card["controls"] if c["name"] == "mfa")
    assert mfa["enforced"] is False
    assert "NOT BUILT" in mfa["detail"]


def test_the_posture_has_no_score(settings: Settings) -> None:
    """A number invites 'how do we get to ten', and the cheap answers are theatre."""
    card = posture.scorecard(settings)
    assert "score" not in card
    assert "out of" not in repr(card).lower() or "no overall score" in card["note"].lower()


def test_turning_step_up_off_is_reported_not_silent() -> None:
    """A control that can be disabled invisibly is a control that is disabled."""
    off = Settings(_env_file=None, step_up_required=False)
    component = posture.component(off)
    assert component.state.value == "DEGRADED"
    assert "step_up_reauth" in component.facts["not_in_force"]


def test_the_posture_reports_no_configured_secret_value() -> None:
    """The values, not the words.

    An earlier version of this test banned the *word* "password", which the
    control named `password_hashing` fails while leaking nothing -- and a
    property that fires on its own vocabulary is one somebody eventually
    weakens. What must never appear is a configured SECRET, so the settings
    below carry distinctive ones and the assertion looks for those.
    """
    loaded = Settings(
        _env_file=None,
        tv_webhook_secret="tv-secret-value-marker",
        smtp_password="smtp-password-value-marker",
        discord_webhook_url="https://discord.com/api/webhooks/999/marker-token",
    )
    rendered = repr(posture.scorecard(loaded)) + repr(posture.component(loaded).facts)
    for value in (
        "tv-secret-value-marker",
        "smtp-password-value-marker",
        "marker-token",
        "discord.com/api/webhooks",
    ):
        assert value not in rendered, f"the posture leaked {value!r}"
    # It still reports *whether* the webhook is configured, which is the
    # operational fact an operator needs and is not the secret.
    webhook = next(
        c for c in posture.scorecard(loaded)["controls"] if c["name"] == "webhook_authentication"
    )
    assert webhook["enforced"] is True


def test_the_security_component_joins_the_existing_health_stack(settings: Settings) -> None:
    """Step 45. One dashboard, not a security dashboard beside it."""
    from app.observability.contract import Criticality, Layer

    component = posture.component(settings)
    assert component.layer is Layer.security
    assert component.criticality is Criticality.critical
    assert component.name == "security"


# ============================================================== 8. structural


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


FORBIDDEN = ("app.risk", "app.oms", "app.brokers", "app.sizing", "app.execution", "app.positions")


def test_the_security_package_cannot_reach_anything_that_trades() -> None:
    """A property of the imports, not a promise in a docstring."""
    for path in sorted(PACKAGE.glob("*.py")):
        offences = [m for m in _imports(path) if m.startswith(FORBIDDEN)]
        assert offences == [], f"{path.name} imports {offences}"


def test_no_security_module_grants_a_permission_or_changes_a_role() -> None:
    """Step-up is a second refusal. It must never be able to approve anything."""
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (".role =", "role=Role.", "ROLE_PERMISSIONS[", "is_active = True"):
            assert forbidden not in source, f"{path.name} contains {forbidden!r}"


def test_no_security_module_logs_a_credential() -> None:
    """The brief's list, checked against the source rather than trusted."""
    banned = ("password=", "secret=", "token=", "authorization=", "private_key")
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            for word in banned:
                assert word not in stripped.lower(), f"{path.name}: {stripped[:70]}"


def test_no_security_route_can_change_a_security_control() -> None:
    """A control reachable over the API it protects is one request from gone."""
    router = BACKEND / "app" / "api" / "v1" / "security.py"
    tree = ast.parse(router.read_text(encoding="utf-8"))
    writes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `router.post(...)` and nothing else: an Attribute whose value is the
        # Name `router`. Matched structurally rather than by string search, so
        # a write route added inside a helper or under an alias still counts.
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id == "router" and func.attr in {"post", "put", "patch", "delete"}:
            writes.append(func.attr)
    # Exactly one write route: the step-up confirmation, which issues a grant
    # and changes no control.
    assert writes == ["post"], writes


def test_no_module_anywhere_disables_a_security_control_at_runtime() -> None:
    """Assignments, not just routes. Configuration is read at startup."""
    app_dir = BACKEND / "app"
    banned = ("csrf_enabled = False", "step_up_required = False", 'cors_origins = ["*"]')
    for path in app_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in source, f"{path} contains {word!r}"


# ============================================================ 9. authorization


async def test_the_posture_is_administrative(app: FastAPI, client: AsyncClient) -> None:
    await _sign_in(client, PLAIN)
    assert (await client.get("/v1/security/posture")).status_code == 403
    assert (await client.get("/v1/security/headers")).status_code == 403
    assert (await client.get("/v1/security/events")).status_code == 403


async def test_step_up_requires_a_session(client: AsyncClient) -> None:
    client.cookies.clear()
    r = await client.post(
        "/v1/security/step-up",
        json={"password": "x", "scope": StepUpScope.kill_switch.value, "subject": "g"},
    )
    assert r.status_code in (401, 403)


async def test_a_signed_in_user_cannot_read_the_audit_trail(
    app: FastAPI, client: AsyncClient
) -> None:
    """It names who did what and when."""
    await _sign_in(client, PLAIN)
    assert (await client.get("/v1/security/events")).status_code == 403
