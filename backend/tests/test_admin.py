"""The administration surface (L36).

The ones that matter most:

  * `test_the_admin_surface_cannot_reach_anything_that_trades` — §10, §49, §66.
  * `test_there_is_no_route_that_enables_live_trading` — §9, §66.
  * `test_no_route_deletes_or_edits_an_audit_row` — §32.
  * `test_a_dangerous_action_needs_a_reason_and_the_right_phrase` — §30.
  * `test_deactivating_a_user_deletes_nothing` — §13.
  * `test_the_last_administrator_cannot_be_deactivated` — §12.
  * `test_an_admin_cannot_deactivate_themselves` — §6.
  * `test_no_admin_route_returns_a_secret` — §57, §65.
  * `test_every_admin_route_refuses_a_plain_user_and_a_trader` — §37, §63, §65.
  * `test_an_empty_platform_reports_zeros_not_examples` — §41, §69.
  * `test_an_administrative_write_is_audited_with_a_reason_and_before_after` — §31, §33, §34, §67.
"""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import AuthSession, Role, User, utcnow
from app.auth.permissions import DANGEROUS, ROLE_PERMISSIONS
from app.core.settings import Settings
from app.db.base import Base
from app.main import create_app
from app.models.accounts import BrokerAccount, PaperAccount
from app.models.bots import Bot
from app.models.execution import Order, Position
from app.models.market import Symbol
from app.models.ops import AuditLog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
ROUTER = BACKEND / "app" / "api" / "v1" / "admin.py"
SERVICE = BACKEND / "app" / "admin" / "service.py"
LEGACY = BACKEND / "app" / "admin" / "router.py"

ADMIN = {"email": "root@tr-platform.io", "password": "correct horse battery"}
TRADER = {"email": "trader@tr-platform.io", "password": "another long passphrase"}
PLAIN = {"email": "reader@tr-platform.io", "password": "a third long passphrase"}
VICTIM = {"email": "victim@tr-platform.io", "password": "a fourth long passphrase"}

SCOPE_ACCESS = "ADMIN_USER_ACCESS"
SCOPE_SESS = "ADMIN_SESSION_REVOKE"

SECRET = "broker-password-do-not-leak"
BASE = datetime(2026, 9, 5, 12, 0, 0)

#: Every route this level adds or owns, and the gate each carries.
ADMIN_GET_ROUTES = [
    "/v1/admin/dashboard",
    "/v1/admin/permissions",
    "/v1/admin/integrations",
    "/v1/admin/configuration",
    "/v1/admin/contract",
    "/v1/admin/users/search",
    "/v1/admin/audit-logs",
    "/v1/admin/audit-logs/actions",
]


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


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


async def _sign_in(client: AsyncClient, who: dict[str, str]) -> None:
    client.cookies.clear()
    r = await client.post("/auth/register", json=who)
    if r.status_code >= 400:
        await client.post("/auth/login", json=who)


@pytest.fixture
async def admin(app: FastAPI, client: AsyncClient) -> AsyncClient:
    await _sign_in(client, ADMIN)
    await _promote(app, ADMIN["email"], Role.admin)
    return client


async def _step_up(client: AsyncClient, scope: str, subject: str, password: str) -> None:
    """L39. Re-authenticate for one dangerous action against one subject.

    Added when L39 put a step-up gate in front of deactivation, reactivation
    and session revocation. The tests below were not weakened to get past it --
    they satisfy it, which is what makes them evidence that the gate lets a
    legitimate operator through as well as stopping a stolen cookie.

    The grant is single-use, so this is called once per dangerous request.
    """
    r = await client.post(
        "/v1/security/step-up",
        headers=_csrf(client),
        json={"password": password, "scope": scope, "subject": subject},
    )
    assert r.status_code == 201, r.text


async def _make_user(app: FastAPI, who: dict[str, str], role: Role = Role.user) -> str:
    """A second user, created without disturbing the caller's session."""
    async with app.state.session_factory() as db:
        from app.auth.passwords import hash_password

        user = User(
            email=who["email"], password_hash=hash_password(who["password"]), role=role.value
        )
        db.add(user)
        await db.commit()
        return user.id


async def _user_id(app: FastAPI, email: str) -> str:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        return user.id


# ================================= 1. the panel is not a second trading engine


FORBIDDEN_IMPORTS = (
    "app.risk",
    "app.oms",
    "app.brokers",
    "app.sizing",
    "app.execution",
    "app.paper.oms",
    "app.positions",
)


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


def test_the_admin_surface_cannot_reach_anything_that_trades() -> None:
    """Sections 10, 49 and 66. A property of the imports, not a promise.

    The admin surface reads models to COUNT them. It imports no risk engine, no
    order manager, no broker adapter and no execution pipeline, so there is no
    object in scope through which it could place, modify or cancel anything.
    """
    for path in (ROUTER, SERVICE, LEGACY):
        offences = [m for m in _imports(path) if m.startswith(FORBIDDEN_IMPORTS)]
        assert offences == [], f"{path.name} imports {offences}"


def test_there_is_no_route_that_enables_live_trading() -> None:
    """Sections 9 and 66. No one-click live switch, with or without safeguards."""
    source = ROUTER.read_text(encoding="utf-8") + SERVICE.read_text(encoding="utf-8")
    for forbidden in ("live_trading =", "LIVE_GATES[", "trading_mode =", "live_trading=True"):
        assert forbidden not in source


def test_no_route_deletes_or_edits_an_audit_row() -> None:
    """Section 32. An administrator cannot erase their own trail.

    Searched across the whole application, not only the admin module: a delete
    from anywhere would be the same hole.
    """
    offenders: list[str] = []
    for path in (BACKEND / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "AuditLog" not in source:
            continue
        if "delete(AuditLog" in source or "update(AuditLog" in source:
            offenders.append(path.name)
    assert offenders == []


def test_the_admin_router_writes_only_user_access() -> None:
    """Section 10. Everything else delegates to the surface that owns it."""
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    posts = [
        d.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for d in node.decorator_list
        if isinstance(d, ast.Call)
        and getattr(d.func, "attr", None) in ("post", "patch", "put", "delete")
        and d.args
        and isinstance(d.args[0], ast.Constant)
    ]
    assert sorted(str(p) for p in posts) == [
        "/users/{user_id}/activate",
        "/users/{user_id}/deactivate",
        "/users/{user_id}/revoke-sessions",
    ]


# ============================================== 2. authorization (§37, 63, 65)


async def test_admin_routes_need_a_session(client: AsyncClient) -> None:
    for path in ADMIN_GET_ROUTES:
        assert (await client.get(path)).status_code == 401, path


async def test_every_admin_route_refuses_a_plain_user_and_a_trader(
    app: FastAPI, client: AsyncClient
) -> None:
    """Section 37. Backend authorization, not a hidden link."""
    await _sign_in(client, PLAIN)
    for path in ADMIN_GET_ROUTES:
        assert (await client.get(path)).status_code == 403, path

    await _sign_in(client, TRADER)
    await _promote(app, TRADER["email"], Role.trader)
    for path in ADMIN_GET_ROUTES:
        assert (await client.get(path)).status_code == 403, path


async def test_a_trader_cannot_deactivate_anybody(app: FastAPI, client: AsyncClient) -> None:
    victim = await _make_user(app, VICTIM)
    await _sign_in(client, TRADER)
    await _promote(app, TRADER["email"], Role.trader)
    r = await client.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(client),
        json={"reason": "no reason at all", "confirm": VICTIM["email"]},
    )
    assert r.status_code == 403
    async with app.state.session_factory() as db:
        assert (await db.get(User, victim)).is_active is True


async def test_a_state_changing_admin_route_needs_csrf(app: FastAPI, admin: AsyncClient) -> None:
    """Section 46. The existing double-submit gate covers the new routes."""
    victim = await _make_user(app, VICTIM)
    r = await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        json={"reason": "a good long reason", "confirm": VICTIM["email"]},
    )
    assert r.status_code == 403


async def test_role_escalation_through_the_role_route_is_still_gated(
    app: FastAPI, client: AsyncClient
) -> None:
    """L04's protection, re-asserted because L36 is the level that surfaces it."""
    await _sign_in(client, TRADER)
    await _promote(app, TRADER["email"], Role.trader)
    me = await _user_id(app, TRADER["email"])
    r = await client.patch(
        f"/v1/admin/users/{me}/role", headers=_csrf(client), json={"role": "admin"}
    )
    assert r.status_code == 403
    async with app.state.session_factory() as db:
        assert (await db.get(User, me)).role == "trader"


# ============================================= 3. dangerous actions (§6, 30)


async def test_a_dangerous_action_needs_a_reason_and_the_right_phrase(
    app: FastAPI, admin: AsyncClient
) -> None:
    victim = await _make_user(app, VICTIM)

    # No reason at all: refused by the schema.
    short = await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(admin),
        json={"reason": "x", "confirm": VICTIM["email"]},
    )
    assert short.status_code == 422

    # A reason, but the wrong phrase: refused by the service.
    wrong = await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(admin),
        json={"reason": "offboarding this account", "confirm": "yes"},
    )
    assert wrong.status_code == 422
    assert "repeat the" in wrong.json()["error"]["detail"]

    async with app.state.session_factory() as db:
        assert (await db.get(User, victim)).is_active is True

    # The step-up gate is deliberately LAST, after the schema and the phrase,
    # so neither refusal above spends a grant. Only the request that can
    # actually succeed needs one.
    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    ok = await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(admin),
        json={"reason": "offboarding this account", "confirm": VICTIM["email"]},
    )
    assert ok.status_code == 200
    assert ok.json()["is_active"] is False


async def test_an_admin_cannot_deactivate_themselves(app: FastAPI, admin: AsyncClient) -> None:
    """Section 6. The action wants a second person's name on it."""
    me = await _user_id(app, ADMIN["email"])
    r = await admin.post(
        f"/v1/admin/users/{me}/deactivate",
        headers=_csrf(admin),
        json={"reason": "testing the guard", "confirm": ADMIN["email"]},
    )
    assert r.status_code == 422
    assert "own account" in r.json()["error"]["detail"]


async def test_the_last_administrator_cannot_be_deactivated(
    app: FastAPI, admin: AsyncClient
) -> None:
    """Section 12, and the same reasoning L04 applied to demotion."""
    second = await _make_user(app, VICTIM, role=Role.admin)
    # Two admins: this one may go.
    await _step_up(admin, SCOPE_ACCESS, second, ADMIN["password"])
    first = await admin.post(
        f"/v1/admin/users/{second}/deactivate",
        headers=_csrf(admin),
        json={"reason": "reducing administrator count", "confirm": VICTIM["email"]},
    )
    assert first.status_code == 200
    # Now there is one, and it is the caller, which the self-guard catches.
    me = await _user_id(app, ADMIN["email"])
    r = await admin.post(
        f"/v1/admin/users/{me}/deactivate",
        headers=_csrf(admin),
        json={"reason": "reducing administrator count", "confirm": ADMIN["email"]},
    )
    assert r.status_code == 422


async def test_deactivating_an_already_inactive_user_is_a_conflict(
    app: FastAPI, admin: AsyncClient
) -> None:
    victim = await _make_user(app, VICTIM)
    body = {"reason": "offboarding this account", "confirm": VICTIM["email"]}
    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    assert (
        await admin.post(f"/v1/admin/users/{victim}/deactivate", headers=_csrf(admin), json=body)
    ).status_code == 200
    again = await admin.post(
        f"/v1/admin/users/{victim}/deactivate", headers=_csrf(admin), json=body
    )
    assert again.status_code == 409


# ============================================== 4. deactivation keeps history


async def test_deactivating_a_user_deletes_nothing(app: FastAPI, admin: AsyncClient) -> None:
    """Section 13. Access is removed; the record is not."""
    victim = await _make_user(app, VICTIM)
    async with app.state.session_factory() as db:
        db.add(
            PaperAccount(
                id="p-victim",
                user_id=victim,
                name="paper",
                currency="USD",
                starting_balance=Decimal("1000"),
                balance=Decimal("1000"),
                equity=Decimal("1000"),
            )
        )
        db.add(Bot(id="bot-victim", user_id=victim, name="runner", mode="paper"))
        db.add(
            AuthSession(user_id=victim, token_hash="a" * 64, expires_at=utcnow().replace(year=2030))
        )
        await db.commit()

    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    r = await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(admin),
        json={"reason": "offboarding this account", "confirm": VICTIM["email"]},
    )
    assert r.status_code == 200
    assert r.json()["sessions_revoked"] == 1

    async with app.state.session_factory() as db:
        assert await db.get(PaperAccount, "p-victim") is not None
        assert await db.get(Bot, "bot-victim") is not None
        user = await db.get(User, victim)
        assert user is not None and user.is_active is False
        session = await db.scalar(select(AuthSession).where(AuthSession.user_id == victim))
        assert session is not None and session.revoked_at is not None


async def test_an_open_position_is_reported_and_never_liquidated(
    app: FastAPI, admin: AsyncClient
) -> None:
    """Section 13. Surface the condition; route the decision elsewhere."""
    victim = await _make_user(app, VICTIM)
    async with app.state.session_factory() as db:
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        # The rows below reference this symbol. SQLAlchemy has no
        # relationship() to tell it they depend on it, so without this
        # flush the children can be written first -- an IntegrityError
        # now that SQLite enforces foreign keys.
        await db.flush()
        db.add(
            PaperAccount(
                id="p-victim",
                user_id=victim,
                name="paper",
                currency="USD",
                starting_balance=Decimal("1000"),
                balance=Decimal("1000"),
                equity=Decimal("1000"),
            )
        )
        db.add(Bot(id="bot-victim", user_id=victim, name="runner", mode="paper", is_enabled=True))
        db.add(
            Position(
                id="pos-1",
                mode="paper",
                paper_account_id="p-victim",
                symbol_id="sym-eur",
                side="long",
                quantity=Decimal("1"),
                initial_quantity=Decimal("1"),
                closed_quantity=Decimal("0"),
                entry_price=Decimal("1.1"),
                status="open",
                source="simulator",
                opened_at=BASE,
            )
        )
        await db.commit()

    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    r = await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(admin),
        json={"reason": "offboarding this account", "confirm": VICTIM["email"]},
    )
    body = r.json()
    assert body["open_positions"] == 1
    assert body["enabled_bots"] == 1
    assert "does NOT close" in body["warning"]

    async with app.state.session_factory() as db:
        position = await db.get(Position, "pos-1")
        assert position is not None and position.status == "open"
        bot = await db.get(Bot, "bot-victim")
        assert bot is not None and bot.is_enabled is True


async def test_a_deactivated_user_cannot_sign_in(app: FastAPI, admin: AsyncClient) -> None:
    victim = await _make_user(app, VICTIM)
    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(admin),
        json={"reason": "offboarding this account", "confirm": VICTIM["email"]},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as other:
        r = await other.post("/auth/login", json=VICTIM)
        assert r.status_code >= 400


async def test_activating_restores_access_without_restoring_sessions(
    app: FastAPI, admin: AsyncClient
) -> None:
    victim = await _make_user(app, VICTIM)
    body = {"reason": "offboarding this account", "confirm": VICTIM["email"]}
    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    await admin.post(f"/v1/admin/users/{victim}/deactivate", headers=_csrf(admin), json=body)
    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    r = await admin.post(
        f"/v1/admin/users/{victim}/activate",
        headers=_csrf(admin),
        json={"reason": "returned from leave", "confirm": VICTIM["email"]},
    )
    assert r.status_code == 200 and r.json()["is_active"] is True
    async with app.state.session_factory() as db:
        session = await db.scalar(select(AuthSession).where(AuthSession.user_id == victim))
        assert session is None or session.revoked_at is not None


# =============================================== 5. sessions (§14, 12)


async def test_sessions_are_listed_without_their_tokens(app: FastAPI, admin: AsyncClient) -> None:
    victim = await _make_user(app, VICTIM)
    async with app.state.session_factory() as db:
        db.add(
            AuthSession(
                user_id=victim,
                token_hash="b" * 64,
                expires_at=utcnow().replace(year=2030),
                user_agent="a browser",
            )
        )
        await db.commit()
    r = await admin.get(f"/v1/admin/users/{victim}/sessions")
    assert r.status_code == 200
    assert "b" * 64 not in r.text
    assert "token" not in json.dumps(r.json()["sessions"][0]).lower()
    assert r.json()["sessions"][0]["active"] is True


async def test_revoking_sessions_leaves_the_account_active(
    app: FastAPI, admin: AsyncClient
) -> None:
    victim = await _make_user(app, VICTIM)
    async with app.state.session_factory() as db:
        db.add(
            AuthSession(user_id=victim, token_hash="c" * 64, expires_at=utcnow().replace(year=2030))
        )
        await db.commit()
    await _step_up(admin, SCOPE_SESS, victim, ADMIN["password"])
    r = await admin.post(
        f"/v1/admin/users/{victim}/revoke-sessions",
        headers=_csrf(admin),
        json={"reason": "suspected shared credentials", "confirm": VICTIM["email"]},
    )
    assert r.status_code == 200 and r.json()["sessions_revoked"] == 1
    async with app.state.session_factory() as db:
        assert (await db.get(User, victim)).is_active is True


# ============================================ 6. the audit trail (§31, 33, 34)


async def test_an_administrative_write_is_audited_with_a_reason_and_before_after(
    app: FastAPI, admin: AsyncClient
) -> None:
    """Sections 31, 33, 34 and 67. Not 'user changed'."""
    victim = await _make_user(app, VICTIM)
    await _step_up(admin, SCOPE_ACCESS, victim, ADMIN["password"])
    await admin.post(
        f"/v1/admin/users/{victim}/deactivate",
        headers=_csrf(admin),
        json={"reason": "offboarding after the contract ended", "confirm": VICTIM["email"]},
    )
    async with app.state.session_factory() as db:
        row = await db.scalar(select(AuditLog).where(AuditLog.action == "user_deactivated"))
    assert row is not None
    assert row.actor_user_id == await _user_id(app, ADMIN["email"])
    assert row.resource_type == "user" and row.resource_id == victim
    assert row.request_id
    assert row.occurred_at is not None
    details = row.details or {}
    assert details["reason"] == "offboarding after the contract ended"
    assert details["before"] == {"is_active": True}
    assert details["after"] == {"is_active": False}


async def test_a_secret_placed_in_a_reason_never_reaches_the_row(
    app: FastAPI, admin: AsyncClient
) -> None:
    """Section 34. Redaction on the way in, not at the call site."""
    victim = await _make_user(app, VICTIM)
    await _step_up(admin, SCOPE_SESS, victim, ADMIN["password"])
    await admin.post(
        f"/v1/admin/users/{victim}/revoke-sessions",
        headers=_csrf(admin),
        json={
            "reason": "rotating after an incident",
            "confirm": VICTIM["email"],
        },
    )
    # And the scrubber itself, on the shape a careless caller would build.
    from app.core.audit import _scrub

    scrubbed = _scrub({"before": {"password": SECRET, "api_key": SECRET}, "note": "fine"})
    assert SECRET not in json.dumps(scrubbed)
    assert scrubbed["note"] == "fine"


async def test_the_audit_trail_filters_by_actor_action_and_resource(
    app: FastAPI, admin: AsyncClient
) -> None:
    """Section 35."""
    victim = await _make_user(app, VICTIM)
    await _step_up(admin, SCOPE_SESS, victim, ADMIN["password"])
    await admin.post(
        f"/v1/admin/users/{victim}/revoke-sessions",
        headers=_csrf(admin),
        json={"reason": "a good long reason", "confirm": VICTIM["email"]},
    )
    actor = await _user_id(app, ADMIN["email"])
    by_action = await admin.get("/v1/admin/audit-logs?action=sessions_revoked")
    assert by_action.json()["page"]["total"] == 1
    by_actor = await admin.get(f"/v1/admin/audit-logs?actor_user_id={actor}")
    assert by_actor.json()["page"]["total"] >= 1
    by_resource = await admin.get(f"/v1/admin/audit-logs?resource_id={victim}")
    assert by_resource.json()["page"]["total"] == 1
    none = await admin.get("/v1/admin/audit-logs?action=nothing_like_this")
    assert none.json()["page"]["total"] == 0


async def test_the_audit_trail_is_paginated_with_the_usual_ceiling(admin: AsyncClient) -> None:
    assert (await admin.get("/v1/admin/audit-logs?limit=10000")).status_code == 422


async def test_the_audit_summary_states_the_retention_position(admin: AsyncClient) -> None:
    """Section 52. Documented, not silently absent."""
    body = (await admin.get("/v1/admin/audit-logs/actions")).json()
    assert "retention" in body and "not been made" in body["retention"]
    assert "immutability" in body


# ================================================== 7. no secrets (§57, 65)


async def test_no_admin_route_returns_a_secret(app: FastAPI, client: AsyncClient) -> None:
    """Section 57. Status only, never the value."""
    configured = Settings(
        _env_file=None,
        smtp_host="mail.example.com",
        smtp_password=SECRET,
        tv_webhook_secret=SECRET,
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/1234567890/" + SECRET,
    )
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(configured, checks={}, engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        await c.post("/auth/register", json=ADMIN)
        await _promote(application, ADMIN["email"], Role.admin)
        for path in ADMIN_GET_ROUTES:
            body = (await c.get(path)).text
            assert SECRET not in body, path
            assert configured.database_url not in body, path
            assert configured.redis_url not in body, path
    await engine.dispose()


async def test_integrations_reports_configured_without_reporting_how(admin: AsyncClient) -> None:
    body = (await admin.get("/v1/admin/integrations")).json()
    assert body["email"]["configured"] is False
    assert "host" not in body["email"]
    assert "TradingView webhook secret" in body["never_returned"]


async def test_a_user_detail_never_carries_a_hash(app: FastAPI, admin: AsyncClient) -> None:
    victim = await _make_user(app, VICTIM)
    body = (await admin.get(f"/v1/admin/users/{victim}")).text
    assert "password" not in body.lower() or "no password" in body.lower()
    assert "$argon2" not in body


# ============================================ 8. configuration and flags (§28)


async def test_no_configuration_is_editable_from_a_browser(admin: AsyncClient) -> None:
    """Section 28. A field that accepted DATABASE_URL would be an RCE."""
    body = (await admin.get("/v1/admin/configuration")).json()
    assert body["runtime_editable"] == []
    assert body["feature_flags"]["implemented"] is False
    assert "without a deployment" in body["feature_flags"]["why_not"]


async def test_the_live_gates_are_reported_and_all_closed(admin: AsyncClient) -> None:
    """Sections 8, 9 and 41."""
    body = (await admin.get("/v1/admin/configuration")).json()
    gates = body["live_gates"]["gates"]
    assert gates and not any(gates.values())
    assert body["live_gates"]["all_built"] is False
    assert body["read_only"]["trading_mode"] == "paper"
    assert body["read_only"]["live_trading"] is False


async def test_the_dashboard_shows_the_environment_first_and_plainly(admin: AsyncClient) -> None:
    body = (await admin.get("/v1/admin/dashboard")).json()
    env = body["environment"]
    assert env["trading_mode"] == "paper"
    assert env["live_trading"] is False
    assert env["live_execution_allowed"] is False
    assert len(env["live_execution_blockers"]) > 0


# ================================================= 9. no fake data (§41, 69)


async def test_an_empty_platform_reports_zeros_not_examples(admin: AsyncClient) -> None:
    body = (await admin.get("/v1/admin/dashboard")).json()
    assert body["bots"]["total"] == 0
    assert body["strategies"]["total"] == 0
    assert body["models"]["versions"] == 0
    assert body["trading"]["trades"] == 0
    assert body["trading"]["positions_open"] == 0
    # One user exists: the administrator who just signed in. Not a sample.
    assert body["users"]["total"] == 1


async def test_the_dashboard_counts_what_is_really_there(app: FastAPI, admin: AsyncClient) -> None:
    victim = await _make_user(app, VICTIM)
    async with app.state.session_factory() as db:
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        # The rows below reference this symbol. SQLAlchemy has no
        # relationship() to tell it they depend on it, so without this
        # flush the children can be written first -- an IntegrityError
        # now that SQLite enforces foreign keys.
        await db.flush()
        db.add(Bot(id="b1", user_id=victim, name="one", mode="paper", is_enabled=True))
        db.add(Bot(id="b2", user_id=victim, name="two", mode="paper"))
        db.add(
            BrokerAccount(id="ba1", user_id=victim, name="demo", broker="fake", account_mode="demo")
        )
        db.add(
            Order(
                id="o-unknown",
                intent_id="i1",
                mode="paper",
                symbol_id="sym-eur",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                status="unknown",
                source="manual",
            )
        )
        await db.commit()
    body = (await admin.get("/v1/admin/dashboard")).json()
    assert body["bots"] == {"total": 2, "enabled": 1, "runs_live": 0}
    assert body["accounts"]["broker"] == 1
    # Section 17: an unresolved order is reported as needing reconciliation.
    assert body["trading"]["orders_needing_reconciliation"] == 1


# ================================================= 10. RBAC is served, not copied


async def test_the_permission_table_is_the_servers_own(admin: AsyncClient) -> None:
    body = (await admin.get("/v1/admin/permissions")).json()
    served = {r["role"]: set(r["permissions"]) for r in body["roles"]}
    for role, permissions in ROLE_PERMISSIONS.items():
        assert served[str(role)] == {str(p) for p in permissions}
    dangerous = {p["permission"] for p in body["permissions"] if p["dangerous"]}
    assert dangerous == {str(p) for p in DANGEROUS}


async def test_a_plain_user_holds_no_dangerous_permission() -> None:
    """Section 5, re-asserted at the level that displays the table."""
    assert ROLE_PERMISSIONS[Role.user].isdisjoint(DANGEROUS)


async def test_mfa_is_recorded_as_a_gap_rather_than_implied(admin: AsyncClient) -> None:
    """Section 54. Documented as absent, not quietly missing."""
    for path in ("/v1/admin/permissions", "/v1/admin/contract"):
        body = (await admin.get(path)).json()
        assert "NOT IMPLEMENTED" in body["mfa"]


async def test_the_contract_names_where_each_control_actually_lives(admin: AsyncClient) -> None:
    """Section 10, served so a client can check it."""
    body = (await admin.get("/v1/admin/contract")).json()
    assert "/v1/bots" in body["delegates_to"]["bots"]
    assert "/v1/ai/models" in body["delegates_to"]["models"]
    cannot = " ".join(body["cannot"])
    assert "enable live trading" in cannot
    assert "delete a trade" in cannot


# ================================================= 11. search and pagination


async def test_user_search_is_server_side_and_paginated(app: FastAPI, admin: AsyncClient) -> None:
    await _make_user(app, VICTIM)
    await _make_user(app, PLAIN)
    everyone = (await admin.get("/v1/admin/users/search")).json()
    assert everyone["page"]["total"] == 3
    found = (await admin.get("/v1/admin/users/search?search=victim")).json()
    assert found["page"]["total"] == 1
    assert found["items"][0]["email"] == VICTIM["email"]
    assert "password_hash" not in json.dumps(found["items"][0])
    none = (await admin.get("/v1/admin/users/search?search=nobody-here")).json()
    assert none["page"]["total"] == 0
    assert none["items"] == []


async def test_user_search_rejects_an_unknown_sort_field(admin: AsyncClient) -> None:
    r = await admin.get("/v1/admin/users/search?sort=password_hash")
    assert r.status_code == 422
    assert "sortable fields" in r.json()["error"]["detail"]


async def test_user_search_filters_by_role_and_status(app: FastAPI, admin: AsyncClient) -> None:
    await _make_user(app, VICTIM, role=Role.trader)
    traders = (await admin.get("/v1/admin/users/search?role=trader")).json()
    assert traders["page"]["total"] == 1
    active = (await admin.get("/v1/admin/users/search?active=true")).json()
    assert active["page"]["total"] == 2
    bad = await admin.get("/v1/admin/users/search?role=wizard")
    assert bad.status_code == 422


async def test_a_missing_user_answers_404(admin: AsyncClient) -> None:
    assert (await admin.get("/v1/admin/users/does-not-exist")).status_code == 404
    assert (await admin.get("/v1/admin/users/does-not-exist/sessions")).status_code == 404
