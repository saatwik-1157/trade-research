"""Authentication and authorization, end to end through the ASGI app.

Runs on an in-memory SQLite database (StaticPool: one connection shared by
the test) so no service is needed. Every request goes through the real
routers, dependencies and cookie handling.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from app.auth import service
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import Role
from app.auth.permissions import RESOURCE_MIN_ROLE
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

ALICE = {"email": "alice@tr-platform.io", "password": "correct horse battery"}
BOB = {"email": "bob@tr-platform.io", "password": "another strong one"}


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    import app.auth.models  # noqa: F401 - register tables

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    from app.auth.models import User
    from sqlalchemy import select

    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


# ---------------------------------------------------------------- accounts


async def test_register_sets_httponly_cookie_and_me_works(client: AsyncClient) -> None:
    r = await client.post("/auth/register", json=ALICE)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == ALICE["email"]
    assert body["role"] == "user"
    assert "password" not in r.text and "hash" not in r.text
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert "Secure" not in cookie  # development

    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == ALICE["email"]


async def test_duplicate_email_is_409(client: AsyncClient) -> None:
    assert (await client.post("/auth/register", json=ALICE)).status_code == 201
    r = await client.post("/auth/register", json={**ALICE, "email": "ALICE@tr-platform.io"})
    assert r.status_code == 409


async def test_weak_password_is_rejected(client: AsyncClient) -> None:
    r = await client.post("/auth/register", json={**ALICE, "password": "short"})
    assert r.status_code == 422


async def test_registration_can_be_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    s = Settings(_env_file=None)
    assert s.allow_registration is False


async def test_login_wrong_password_and_unknown_email_look_the_same(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    await client.post("/auth/logout")
    wrong = await client.post("/auth/login", json={**ALICE, "password": "not the password"})
    unknown = await client.post("/auth/login", json={**BOB})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["detail"] == unknown.json()["error"]["detail"]


def csrf(client: AsyncClient) -> dict[str, str]:
    """A session-bearing state-changing request must echo the CSRF token."""
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def test_logout_revokes_the_session(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    r = await client.post("/auth/logout", headers=csrf(client))
    assert r.status_code == 204
    assert (await client.get("/auth/me")).status_code == 401


async def test_expired_session_is_rejected(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    async with app.state.session_factory() as db:
        user = await service.authenticate(db, ALICE["email"], ALICE["password"])
        token, _ = await service.create_session(db, user, timedelta(seconds=-1))
    client.cookies.set("tr_session", token)
    assert (await client.get("/auth/me")).status_code == 401


async def test_forged_token_is_rejected(client: AsyncClient) -> None:
    client.cookies.set("tr_session", "not-a-real-token")
    assert (await client.get("/auth/me")).status_code == 401


# ----------------------------------------------------------- authorization


async def test_permission_table_covers_the_protected_resources() -> None:
    for resource in ("brokers", "strategies", "bots", "orders", "ai_models"):
        assert RESOURCE_MIN_ROLE[resource] is Role.trader
    assert RESOURCE_MIN_ROLE["admin"] is Role.admin


async def test_there_are_no_pre_v1_aliases_left() -> None:
    """Five have left this list as their groups were built: `/orders` at L06,
    `/brokers` at L10, `/strategies` at L12, `/bots` at L22 and `/ai/models` at
    L24. In each case a root path claiming the whole group was missing would
    have become a false statement about a surface that exists.

    Asserted rather than left implicit, so adding a new unversioned alias has
    to be a deliberate act that changes this test."""
    from app.api.protected import LEGACY_PATHS

    assert LEGACY_PATHS == {}


# `/v1/risk/rules` was here until L17 built it, `/v1/bots` until L22 and
# `/v1/ai/models` until L24; none is a pending stub any more. The one below
# replaces the coverage they used to give: a built group must gate at least as
# hard as the 501 it replaced.
@pytest.mark.parametrize("path", ["/v1/models/registry"])
async def test_v1_pending_routes_gate_before_answering(
    app: FastAPI, client: AsyncClient, path: str
) -> None:
    """The gate runs before the 501, so authorization is enforced and tested
    before any of these can do anything."""
    assert (await client.get(path)).status_code == 401

    await client.post("/auth/register", json=ALICE)
    assert (await client.get(path)).status_code == 403

    await _promote(app, ALICE["email"], Role.trader)
    r = await client.get(path)
    assert r.status_code == 501
    assert "not built until level" in r.json()["error"]["detail"]


async def test_building_a_group_does_not_loosen_the_gate_it_replaced(
    app: FastAPI, client: AsyncClient
) -> None:
    """`/v1/bots` answered 403 to a plain USER while it was a 501 stub. Building
    it at L22 must not have made it easier to reach than the stub was.

    This is the failure the level actually shipped for one run: the new router
    asked only for a logged-in user, so a fresh USER account could list every
    bot, its mode and its limits. A built group inheriting the gate of the stub
    it replaces is the rule; this test is what enforces it."""
    assert (await client.get("/v1/bots")).status_code == 401  # anonymous

    await client.post("/auth/register", json=ALICE)
    assert (await client.get("/v1/bots")).status_code == 403  # USER

    await _promote(app, ALICE["email"], Role.trader)
    assert (await client.get("/v1/bots")).status_code == 200


async def test_a_built_group_drops_its_unversioned_alias(client: AsyncClient) -> None:
    """The alias existed to carry a 501 for a whole group. Once the group is
    real the alias would be a second path to one implementation, so it goes --
    as `/orders` did at L06 and `/bots` at L22. 404 is the honest answer."""
    assert (await client.get("/bots")).status_code == 404
    assert (await client.get("/ai/models")).status_code == 404


async def test_a_still_pending_group_answers_501_from_behind_its_gate(
    app: FastAPI, client: AsyncClient
) -> None:
    """What the legacy-alias test used to cover, now that no alias is left.

    The property that mattered was never "two paths agree" -- it was that a
    pending group is gated BEFORE it answers, so authorization is enforced and
    tested before the group can do anything."""
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.trader)
    r = await client.get("/v1/models/registry")
    assert r.status_code == 501
    assert "not built until level" in r.json()["error"]["detail"]


async def test_auth_works_at_both_the_legacy_and_versioned_paths(client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    legacy = await client.get("/auth/me")
    versioned = await client.get("/v1/auth/me")
    assert legacy.status_code == versioned.status_code == 200
    assert legacy.json()["email"] == versioned.json()["email"] == ALICE["email"]


async def test_admin_routes_need_admin(app: FastAPI, client: AsyncClient) -> None:
    assert (await client.get("/admin/users")).status_code == 401
    await client.post("/auth/register", json=ALICE)
    assert (await client.get("/admin/users")).status_code == 403
    await _promote(app, ALICE["email"], Role.trader)
    assert (await client.get("/admin/users")).status_code == 403
    await _promote(app, ALICE["email"], Role.admin)
    r = await client.get("/admin/users")
    assert r.status_code == 200
    assert [u["email"] for u in r.json()] == [ALICE["email"]]
    assert "password_hash" not in r.text


async def test_admin_can_change_roles_but_not_remove_last_admin(
    app: FastAPI, client: AsyncClient
) -> None:
    await client.post("/auth/register", json=ALICE)
    await _promote(app, ALICE["email"], Role.admin)
    bob = (await client.post("/auth/register", json=BOB)).json()  # switches session to bob
    # bob is USER; admin actions must fail for him
    assert (await client.get("/admin/users")).status_code == 403
    # back to alice
    await client.post("/auth/login", json=ALICE)
    r = await client.patch(
        f"/admin/users/{bob['id']}/role", json={"role": "trader"}, headers=csrf(client)
    )
    assert r.status_code == 200 and r.json()["role"] == "trader"

    me = (await client.get("/auth/me")).json()
    r = await client.patch(
        f"/admin/users/{me['id']}/role", json={"role": "user"}, headers=csrf(client)
    )
    assert r.status_code == 400
    assert "last admin" in r.json()["error"]["detail"]

    missing = await client.patch(
        "/admin/users/nope/role", json={"role": "user"}, headers=csrf(client)
    )
    assert missing.status_code == 404


async def test_bootstrap_admin_creates_then_promotes(app: FastAPI) -> None:
    async with app.state.session_factory() as db:
        user, action = await service.ensure_admin(db, BOB["email"], BOB["password"])
        assert action == "created" and user.role == "admin"
        _, action = await service.ensure_admin(db, BOB["email"], BOB["password"])
        assert action == "unchanged"


def test_cookie_secure_follows_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).cookie_secure_effective is False
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert Settings(_env_file=None).cookie_secure_effective is True
    monkeypatch.setenv("COOKIE_SECURE", "false")
    assert Settings(_env_file=None).cookie_secure_effective is False
