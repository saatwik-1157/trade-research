"""Rate limiting, CSRF, audit logging, permissions and password reset.

The L04 gaps, each closed and each tested for the property that matters
rather than for the mechanism.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from app.auth import reset as reset_service
from app.auth import service
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import PasswordResetToken, Role, User
from app.auth.permissions import (
    ADMIN_PERMISSIONS,
    DANGEROUS,
    TRADER_PERMISSIONS,
    USER_PERMISSIONS,
    Permission,
    has_permission,
    min_role_for_permission,
    permissions_for,
)
from app.auth.ratelimit import InMemoryRateLimiter, RateLimit
from app.core import audit
from app.core.settings import Settings
from app.db.base import Base
from app.models.ops import AuditLog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.main import create_app  # isort: skip

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
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


async def audit_actions(app: FastAPI) -> list[str]:
    async with app.state.session_factory() as db:
        return [r.action for r in (await db.scalars(select(AuditLog).order_by(AuditLog.id))).all()]


# ------------------------------------------------------------- permissions


def test_nothing_dangerous_is_granted_to_a_new_account() -> None:
    # Registration yields USER. A USER must not hold a permission that can
    # move money, start work, or change who can.
    assert not (USER_PERMISSIONS & DANGEROUS)
    for permission in DANGEROUS:
        assert not has_permission(Role.user, permission), permission


def test_roles_are_strictly_cumulative() -> None:
    assert USER_PERMISSIONS < TRADER_PERMISSIONS < ADMIN_PERMISSIONS
    assert permissions_for(Role.admin) == ADMIN_PERMISSIONS


def test_execution_needs_trader_and_administration_needs_admin() -> None:
    assert min_role_for_permission(Permission.submit_orders) is Role.trader
    assert min_role_for_permission(Permission.manage_risk_settings) is Role.trader
    assert min_role_for_permission(Permission.manage_users) is Role.admin
    assert min_role_for_permission(Permission.view_markets) is Role.user


# ------------------------------------------------------------ rate limiting


async def test_limiter_blocks_after_the_limit_and_reports_a_retry() -> None:
    limiter = InMemoryRateLimiter()
    limit = RateLimit(limit=3, window_seconds=60)
    for _ in range(3):
        assert (await limiter.hit("login", "ip:1.2.3.4", limit)).allowed
    blocked = await limiter.hit("login", "ip:1.2.3.4", limit)
    assert not blocked.allowed
    assert blocked.retry_after > 0
    assert "too many attempts" in blocked.detail
    # A different key is unaffected.
    assert (await limiter.hit("login", "ip:5.6.7.8", limit)).allowed


async def test_repeated_failed_logins_are_rate_limited(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await client.post("/auth/logout", headers={CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""})

    statuses = []
    for _ in range(12):
        r = await client.post("/auth/login", json={**ALICE, "password": "wrong wrong wrong"})
        statuses.append(r.status_code)

    assert 429 in statuses, "brute force was never throttled"
    blocked = next(s for s in statuses if s == 429)
    assert blocked == 429
    # The 429 arrives before the attempts run out entirely.
    assert statuses.index(429) <= 10


async def test_a_rate_limited_attempt_is_audited(app: FastAPI, client: AsyncClient) -> None:
    for _ in range(12):
        await client.post("/auth/login", json={"email": "x@y.io", "password": "no"})
    assert "rate_limited" in await audit_actions(app)


# -------------------------------------------------------------------- CSRF


async def test_a_session_request_without_the_csrf_header_is_refused(
    client: AsyncClient,
) -> None:
    r = await client.post("/auth/register", json=ALICE)
    assert r.status_code == 201
    assert client.cookies.get(CSRF_COOKIE), "no readable CSRF cookie was issued"

    # httpx replays cookies but not headers: this is exactly the shape of a
    # cross-site request that rides the session cookie.
    forged = await client.post("/auth/logout")
    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "csrf_failed"


async def test_the_same_request_succeeds_with_the_header(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    token = client.cookies.get(CSRF_COOKIE)
    ok = await client.post("/auth/logout", headers={CSRF_HEADER: token or ""})
    assert ok.status_code == 204


async def test_a_mismatched_token_is_refused(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    bad = await client.post("/auth/logout", headers={CSRF_HEADER: "not-the-token"})
    assert bad.status_code == 403


async def test_safe_methods_and_sessionless_requests_are_exempt(client: AsyncClient) -> None:
    # GET never needs a token.
    assert (await client.get("/health")).status_code == 200
    # Sign-in establishes a session rather than using one, so it is exempt.
    assert (await client.post("/auth/login", json=ALICE)).status_code in (200, 401, 429)


# ----------------------------------------------------------- audit logging


async def test_login_logout_and_failure_are_all_recorded(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    token = client.cookies.get(CSRF_COOKIE) or ""
    await client.post("/auth/logout", headers={CSRF_HEADER: token})
    await client.post("/auth/login", json={**ALICE, "password": "wrong wrong wrong"})
    await client.post("/auth/login", json=ALICE)

    actions = await audit_actions(app)
    for expected in ("register", "logout", "login_failed", "login"):
        assert expected in actions, f"{expected} was not audited"


async def test_a_role_change_records_who_did_it_and_what_changed(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert user is not None
        user.role = Role.admin.value
        await db.commit()
        subject = User(email="bob@tr-platform.io", password_hash="x", role=Role.user.value)
        db.add(subject)
        await db.commit()
        subject_id = subject.id

    token = client.cookies.get(CSRF_COOKIE) or ""
    r = await client.patch(
        f"/admin/users/{subject_id}/role",
        json={"role": "trader"},
        headers={CSRF_HEADER: token},
    )
    assert r.status_code == 200

    async with app.state.session_factory() as db:
        entry = await db.scalar(select(AuditLog).where(AuditLog.action == "role_changed"))
    assert entry is not None
    assert entry.details == {"from": "user", "to": "trader", "subject_email": "bob@tr-platform.io"}
    assert entry.actor_user_id is not None


def test_the_audit_writer_scrubs_secrets_at_any_depth() -> None:
    scrubbed = audit._scrub(
        {
            "email": "a@b.io",
            "password": "hunter2",
            "nested": {"api_key": "abc123", "keep": 1},
            "list": [{"reset_token": "zzz"}],
        }
    )
    assert scrubbed["email"] == "a@b.io"
    assert scrubbed["password"] == audit.REDACTED
    assert scrubbed["nested"]["api_key"] == audit.REDACTED
    assert scrubbed["nested"]["keep"] == 1
    assert scrubbed["list"][0]["reset_token"] == audit.REDACTED


async def test_a_failed_login_never_records_the_password(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "a@b.io", "password": "s3cret-value"})
    async with app.state.session_factory() as db:
        rows = (await db.scalars(select(AuditLog))).all()
    assert rows
    assert "s3cret-value" not in str([r.details for r in rows])


# -------------------------------------------------------- password reset


async def test_reset_request_is_indistinguishable_for_unknown_accounts(
    client: AsyncClient,
) -> None:
    await client.post("/auth/register", json=ALICE)
    await client.post("/auth/logout", headers={CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""})

    known = await client.post("/auth/password-reset/request", json={"email": ALICE["email"]})
    unknown = await client.post(
        "/auth/password-reset/request", json={"email": "nobody@tr-platform.io"}
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json(), "the response reveals whether an account exists"


async def test_the_reset_token_never_appears_in_the_response(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.post("/auth/password-reset/request", json={"email": ALICE["email"]})
    body = r.text
    async with app.state.session_factory() as db:
        row = await db.scalar(select(PasswordResetToken))
    assert row is not None, "no token was created"
    # Only the hash is stored, and neither it nor any token is echoed.
    assert row.token_hash not in body
    assert len(row.token_hash) == 64


async def test_a_reset_completes_once_and_revokes_every_session(app: FastAPI) -> None:
    async with app.state.session_factory() as db:
        user = await service.register(db, ALICE["email"], ALICE["password"], allow=True)
        raw_a, _ = await service.create_session(db, user, timedelta(hours=1))
        raw_b, _ = await service.create_session(db, user, timedelta(hours=1))

        delivered: list[tuple[str, str]] = []

        class Capturing:
            name = "test"

            async def send(self, email: str, token: str) -> None:
                delivered.append((email, token))

        assert await reset_service.request_reset(db, ALICE["email"], Capturing()) is True
        _, token = delivered[0]

        await reset_service.complete_reset(db, token, "a brand new password")

        # Both prior sessions are dead.
        assert await service.user_for_token(db, raw_a) is None
        assert await service.user_for_token(db, raw_b) is None
        # The new password works, the old one does not.
        assert await service.authenticate(db, ALICE["email"], "a brand new password")
        with pytest.raises(service.InvalidCredentials):
            await service.authenticate(db, ALICE["email"], ALICE["password"])
        # Single use.
        with pytest.raises(reset_service.InvalidResetToken):
            await reset_service.complete_reset(db, token, "yet another password")


async def test_an_expired_or_forged_token_is_refused(app: FastAPI) -> None:
    async with app.state.session_factory() as db:
        user = await service.register(db, ALICE["email"], ALICE["password"], allow=True)
        expired: list[str] = []

        class Capturing:
            name = "test"

            async def send(self, email: str, token: str) -> None:
                expired.append(token)

        await reset_service.request_reset(
            db, ALICE["email"], Capturing(), ttl=timedelta(seconds=-1)
        )
        with pytest.raises(reset_service.InvalidResetToken):
            await reset_service.complete_reset(db, expired[0], "a brand new password")
        with pytest.raises(reset_service.InvalidResetToken):
            await reset_service.complete_reset(db, "forged", "a brand new password")
        assert user is not None


async def test_delivery_is_unconfigured_and_says_so_rather_than_pretending() -> None:
    delivery = reset_service.UnconfiguredDelivery()
    with pytest.raises(reset_service.DeliveryUnavailable) as exc:
        await delivery.send("a@b.io", "token")
    assert "level 34" in str(exc.value)


# ------------------------------------------------------------ user record


async def test_last_login_is_written_on_success_only(app: FastAPI) -> None:
    async with app.state.session_factory() as db:
        user = await service.register(db, ALICE["email"], ALICE["password"], allow=True)
        assert user.last_login_at is None

        with pytest.raises(service.InvalidCredentials):
            await service.authenticate(db, ALICE["email"], "wrong wrong wrong")
        await db.refresh(user)
        assert user.last_login_at is None, "a failed attempt moved last_login_at"

        await service.authenticate(db, ALICE["email"], ALICE["password"])
        await db.refresh(user)
        assert user.last_login_at is not None


async def test_the_user_response_never_carries_a_hash(client: AsyncClient) -> None:
    r = await client.post("/auth/register", json=ALICE)
    assert "password" not in r.text and "hash" not in r.text
    me = await client.get("/auth/me")
    assert "password" not in me.text and "hash" not in me.text
