"""Recovery, resilience and reconciliation (L38).

The ones that matter most:

  * `test_recovery_cannot_submit_modify_or_cancel_anything` — rules 3 and 12.
  * `test_recovery_never_enables_live_trading` — rule 15.
  * `test_an_unknown_order_latches_safe_mode_and_is_never_retried` — steps 6, 18.
  * `test_safe_mode_blocks_a_new_order_and_names_the_condition` — steps 18, 19.
  * `test_safe_mode_is_an_extra_refusal_not_a_replacement_for_risk` — rule 2.
  * `test_releasing_safe_mode_re_runs_the_sequence_and_refuses_while_it_still_holds` — step 18.
  * `test_a_bot_is_not_recovered_over_an_unresolved_order` — steps 10, 6.
  * `test_reconciliation_repairs_nothing` — step 14.
  * `test_the_startup_sequence_never_raises` — steps 17, 25.
  * `test_a_missing_table_latches_safe_mode_rather_than_migrating` — step 15.
  * `test_a_notification_failure_never_blocks_recovery` — step 21, rule 14.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import Role, User, utcnow
from app.core.settings import Settings
from app.db.base import Base
from app.execution.outcome import Outcome
from app.main import create_app
from app.models.accounts import PaperAccount
from app.models.bots import Bot, BotRun
from app.models.execution import Order, Position
from app.models.market import Symbol
from app.models.ops import SystemEvent
from app.recovery import bots as recovery_bots
from app.recovery import reconciliation
from app.recovery.contract import RecoveryState, SafeModeReason, StepStatus
from app.recovery.manager import RecoveryManager
from app.recovery.safe_mode import SafeMode, SafeModeBlocked
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "recovery"
ROUTER = BACKEND / "app" / "api" / "v1" / "recovery.py"

ADMIN = {"email": "root@tr-platform.io", "password": "correct horse battery"}
TRADER = {"email": "trader@tr-platform.io", "password": "another long passphrase"}
NOW = datetime(2026, 9, 5, 12, 0, 0)


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
    async with application.state.session_factory() as db:
        db.add(User(id="owner", email="owner@tr-platform.io", password_hash="x", role="trader"))
        db.add(Symbol(id="sym-eur", code="EURUSD", asset_class="fx", unit_class="points"))
        db.add(
            PaperAccount(
                id="p1",
                user_id="owner",
                name="paper",
                currency="USD",
                starting_balance=Decimal("1000"),
                balance=Decimal("1000"),
                equity=Decimal("1000"),
            )
        )
        await db.commit()
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _step_up_safe_mode(client: AsyncClient) -> None:
    """L39. Releasing safe mode needs the password again.

    L38 made releasing expensive on purpose -- it re-runs the whole startup
    sequence -- and said in its own document that the administrative session
    doing the releasing was still just a cookie. L39 closed that. These tests
    satisfy the gate rather than being relaxed around it: a release that a
    legitimate operator cannot perform is as broken as one anybody can.
    """
    r = await client.post(
        "/v1/security/step-up",
        headers=_csrf(client),
        json={
            "password": ADMIN["password"],
            "scope": "SAFE_MODE_EXIT",
            "subject": "safe_mode",
        },
    )
    assert r.status_code == 201, r.text


async def _promote(app: FastAPI, email: str, role: Role) -> None:
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role.value
        await db.commit()


@pytest.fixture
async def admin(app: FastAPI, client: AsyncClient) -> AsyncClient:
    await client.post("/auth/register", json=ADMIN)
    await _promote(app, ADMIN["email"], Role.admin)
    return client


async def _unknown_order(app: FastAPI, order_id: str = "o-unknown") -> None:
    async with app.state.session_factory() as db:
        db.add(
            Order(
                id=order_id,
                intent_id=f"intent-{order_id}",
                mode="paper",
                paper_account_id="p1",
                symbol_id="sym-eur",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                status="unknown",
                source="manual",
            )
        )
        await db.commit()


# ================================= 1. recovery cannot trade (rules 3, 12, 15)


FORBIDDEN_IMPORTS = (
    "app.oms.service",
    "app.oms.order",
    "app.brokers.base",
    "app.brokers.mt5",
    "app.risk.engine",
    "app.sizing",
    "app.execution.pipeline",
)

FORBIDDEN_CALLS = (
    "submit",
    "submit_order",
    "place_order",
    "cancel",
    "modify",
    "close",
    "close_now",
    "engage_kill_switch",
    "release_kill_switch",
    "drop_all",
    "create_all",
)


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_recovery_cannot_submit_modify_or_cancel_anything() -> None:
    """Rules 3 and 12. A property of the package, not a promise about it."""
    offences: list[str] = []
    called: set[str] = set()
    for path in [*_modules(), ROUTER]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(FORBIDDEN_IMPORTS):
                    offences.append(f"{path.name} imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(FORBIDDEN_IMPORTS):
                        offences.append(f"{path.name} imports {alias.name}")
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if isinstance(name, str):
                    called.add(name)
    assert offences == []
    assert called.isdisjoint(FORBIDDEN_CALLS)


def test_recovery_never_enables_live_trading() -> None:
    """Rule 15. No assignment to either, anywhere in the package."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in [*_modules(), ROUTER])
    for forbidden in (
        "live_trading =",
        "live_trading=True",
        "trading_mode =",
        "LIVE_GATES[",
    ):
        assert forbidden not in source


def test_recovery_destroys_no_historical_record() -> None:
    """Rule 13. No delete, no drop, no truncate."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in [*_modules(), ROUTER])
    for forbidden in ("DROP TABLE", "DELETE FROM", "TRUNCATE", "db.delete(", "drop_all"):
        assert forbidden not in source


def test_recovery_sends_no_notification_directly() -> None:
    """Step 28. It publishes one catalogued event; L34 does the rest."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in _modules())
    for forbidden in ("DiscordWebhookChannel", "EmailChannel", "smtplib", "NotificationService("):
        assert forbidden not in source


# =============================================== 2. the safe-mode latch (§18)


def test_a_latch_never_closes_without_a_reason() -> None:
    latch = SafeMode()
    assert latch.engaged is False
    latch.engage(SafeModeReason.unknown_order_state, "two orders unresolved")
    assert latch.engaged is True
    assert latch.reasons[0].detail == "two orders unresolved"
    assert str(latch.reasons[0].reason) == "UNKNOWN_ORDER_STATE"


def test_engaging_the_same_reason_twice_keeps_the_original_time() -> None:
    """ "When did this start" must not answer "just now" forever."""
    latch = SafeMode()
    first = latch.engage(SafeModeReason.broker_unreachable, "gone", at=NOW)
    again = latch.engage(SafeModeReason.broker_unreachable, "still gone")
    assert again.at == first.at == NOW
    assert len(latch.reasons) == 1


def test_releasing_one_reason_leaves_the_others_holding() -> None:
    latch = SafeMode()
    latch.engage(SafeModeReason.unknown_order_state, "a")
    latch.engage(SafeModeReason.broker_unreachable, "b")
    assert latch.release(SafeModeReason.broker_unreachable, actor_user_id="u1", why="reconnected")
    assert latch.engaged is True
    assert [str(r.reason) for r in latch.reasons] == ["UNKNOWN_ORDER_STATE"]


def test_releasing_something_that_was_never_latched_is_a_no_op() -> None:
    latch = SafeMode()
    assert latch.release(SafeModeReason.operator, actor_user_id="u1", why="x") is False


def test_the_guard_raises_with_the_condition_not_the_mode() -> None:
    latch = SafeMode()
    latch.engage(SafeModeReason.unknown_order_state, "two orders unresolved")
    with pytest.raises(SafeModeBlocked) as caught:
        latch.check()
    assert "two orders unresolved" in caught.value.detail


def test_safe_mode_still_allows_everything_observational() -> None:
    """Step 18. Reconciling is how you get out, so it must keep working."""
    latch = SafeMode()
    latch.engage(SafeModeReason.operator, "somebody decided")
    described = latch.describe()
    allowed = " ".join(described["still_allowed"])
    assert "reconciliation" in allowed
    assert "monitoring" in allowed
    assert "recovery actions" in allowed


def test_safe_mode_is_an_extra_refusal_not_a_replacement_for_risk() -> None:
    """Rule 2. It never approves, releases or widens anything."""
    described = SafeMode().describe()
    assert "never approves anything" in described["authority"]
    assert "RiskEngine's veto is unaffected" in described["authority"]


# ================================= 3. the startup sequence (§17, §25, §15)


async def test_the_startup_sequence_runs_every_step_in_order(app: FastAPI) -> None:
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    names = [s.name for s in report.steps]
    assert names[0] == "configuration"
    assert names[-1] == "operational_mode"
    # Reconciliation sits between the broker link and the resume decision.
    assert names.index("broker") < names.index("oms_reconciliation")
    assert names.index("oms_reconciliation") < names.index("operational_mode")


async def test_the_startup_sequence_never_raises(app: FastAPI) -> None:
    """Step 25. A recovery routine that can crash leaves the platform as found."""

    class _Broken:
        @property
        def adapters(self) -> dict[str, Any]:
            raise RuntimeError("registry exploded")

    app.state.brokers = _Broken()
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    broker = next(s for s in report.steps if s.name == "broker")
    assert broker.status is StepStatus.failed
    assert "the check itself failed" in broker.detail
    # The rest of the sequence still ran.
    assert len(report.steps) > 8


async def test_a_clean_platform_starts_in_normal_mode(app: FastAPI) -> None:
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    # Nothing latched, and the operational mode says so. The event-bus step is
    # ATTENTION in this fixture because the lifespan -- which is what starts
    # the hub's reader -- has not run; that is a true observation and
    # deliberately does NOT latch safe mode, because a reader that is not
    # running is not a reason to stop trading.
    assert manager.safe_mode.engaged is False
    blocking = {s.name for s in report.attention}
    assert blocking <= {"event_bus"}
    mode = next(s for s in report.steps if s.name == "operational_mode")
    assert "never will" in mode.detail
    assert manager.state() in (RecoveryState.normal, RecoveryState.degraded)


async def test_an_unknown_order_latches_safe_mode_and_is_never_retried(
    app: FastAPI,
) -> None:
    """Steps 6 and 18, and the sentence that matters: settle by asking."""
    await _unknown_order(app)
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    assert report.clean is False
    assert manager.safe_mode.engaged is True
    assert SafeModeReason.unknown_order_state in {r.reason for r in manager.safe_mode.reasons}
    step = next(s for s in report.steps if s.name == "oms_reconciliation")
    assert "never by re-sending" in step.detail
    # And the order is untouched: recovery settles nothing by itself.
    async with app.state.session_factory() as db:
        row = await db.get(Order, "o-unknown")
        assert row is not None and row.status == "unknown"


async def test_an_unsettled_position_latches_safe_mode(app: FastAPI) -> None:
    async with app.state.session_factory() as db:
        db.add(
            Position(
                id="pos-1",
                mode="paper",
                paper_account_id="p1",
                symbol_id="sym-eur",
                side="long",
                quantity=Decimal("1"),
                initial_quantity=Decimal("1"),
                closed_quantity=Decimal("0"),
                entry_price=Decimal("1.1"),
                status="reconciling",
                source="simulator",
                opened_at=NOW,
            )
        )
        await db.commit()
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        await manager.run_startup(db, app)
        await db.commit()
    assert SafeModeReason.position_mismatch in {r.reason for r in manager.safe_mode.reasons}


async def test_a_missing_table_latches_safe_mode_rather_than_migrating(
    app: FastAPI,
) -> None:
    """Step 15. Never migrate on boot: N processes racing to migrate."""
    async with app.state.session_factory() as db:
        from sqlalchemy import text

        await db.execute(text("DROP TABLE market_bars"))
        await db.commit()
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    step = next(s for s in report.steps if s.name == "migrations")
    assert step.status is StepStatus.failed
    assert "market_bars" in step.facts["missing"]
    assert "racing to migrate" in step.detail
    assert SafeModeReason.database_inconsistent in {r.reason for r in manager.safe_mode.reasons}


async def test_no_market_data_is_skipped_not_stale(app: FastAPI) -> None:
    """A fresh install must not boot into safe mode for having no history."""
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
    step = next(s for s in report.steps if s.name == "market_data")
    assert step.status is StepStatus.skipped
    assert "not staleness" in step.detail
    assert manager.safe_mode.engaged is False


async def test_no_broker_adapter_is_skipped_not_a_clean_reconciliation(
    app: FastAPI,
) -> None:
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
    step = next(s for s in report.steps if s.name == "broker")
    assert step.status is StepStatus.skipped
    assert "the absence of one" in step.detail


async def test_every_startup_step_is_recorded(app: FastAPI) -> None:
    """Step 27. One row per step, in L05's operational-events table."""
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    async with app.state.session_factory() as db:
        rows = list((await db.scalars(select(SystemEvent))).all())
    assert len(rows) == len(report.steps)
    assert all(r.component.startswith("recovery.") for r in rows)
    assert all(r.event_type == "RECOVERY_STARTUP" for r in rows)


# ==================================================== 4. reconciliation (§14)


async def test_reconciliation_repairs_nothing(app: FastAPI) -> None:
    """Step 14. Nothing is closed, opened, cancelled or re-sent."""
    await _unknown_order(app)
    async with app.state.session_factory() as db:
        db.add(
            Position(
                id="pos-1",
                mode="paper",
                paper_account_id="p1",
                symbol_id="sym-eur",
                side="long",
                quantity=Decimal("1"),
                initial_quantity=Decimal("1"),
                closed_quantity=Decimal("0"),
                entry_price=Decimal("1.1"),
                status="unknown",
                source="simulator",
                opened_at=NOW,
            )
        )
        await db.commit()

    async with app.state.session_factory() as db:
        report = await reconciliation.sweep(
            db, brokers=app.state.brokers, session_factory=app.state.session_factory
        )
    assert report.clean is False
    authority = report.as_dict()["authority"]
    assert "REPORTS" in authority and "never re-sent" in authority

    async with app.state.session_factory() as db:
        assert (await db.get(Order, "o-unknown")).status == "unknown"
        assert (await db.get(Position, "pos-1")).status == "unknown"


async def test_reconciliation_names_the_rows_rather_than_counting_them_away(
    app: FastAPI,
) -> None:
    await _unknown_order(app, "o-a")
    await _unknown_order(app, "o-b")
    async with app.state.session_factory() as db:
        step = await reconciliation.check_orders(db)
    assert step.facts["unresolved"] == 2
    assert set(step.facts["order_ids"]) == {"o-a", "o-b"}


# ============================================ 5. safe mode blocks new work


async def test_safe_mode_blocks_a_new_order_and_names_the_condition(
    app: FastAPI, client: AsyncClient
) -> None:
    """Steps 18 and 19, through the real submission route."""
    await client.post("/auth/register", json=TRADER)
    await _promote(app, TRADER["email"], Role.trader)
    app.state.safe_mode.engage(
        SafeModeReason.unknown_order_state, "one order's venue state was never established"
    )
    r = await client.post(
        "/v1/orders",
        headers={"Idempotency-Key": "01JB2K7Q9WZ8YV5N3M4XR6TSD0", **_csrf(client)},
        json={
            "account_id": "p1",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
        },
    )
    assert r.status_code == 409
    detail = r.json()["error"]["detail"]
    assert "safe mode" in detail
    assert "venue state was never established" in detail


async def test_the_execution_pipeline_refuses_while_safe_mode_is_engaged(
    app: FastAPI,
) -> None:
    """Automated execution stops too, with its own outcome."""
    from app.execution.pipeline import IncomingSignal

    app.state.safe_mode.engage(SafeModeReason.operator, "an operator decided")
    signal = IncomingSignal(
        signal_id="s1",
        signal_key="k1",
        source="tradingview",
        symbol="EURUSD",
        side="buy",
        signal_time=utcnow(),
        account_id="p1",
        mode="paper",
        strategy_id=None,
        entry_price=Decimal("1.1"),
    )
    result = await app.state.execution_pipeline.process(signal)
    assert result.outcome is Outcome.safe_mode
    assert "safe mode" in result.detail
    # The pass stopped before anything was sent, and the record says so.
    assert result.created_order is False
    assert result.order is None
    assert result.risk is None


async def test_a_bot_is_not_recovered_while_safe_mode_is_engaged(app: FastAPI) -> None:
    """Step 10, first condition."""
    async with app.state.session_factory() as db:
        db.add(Bot(id="bot1", user_id="owner", name="runner", mode="paper", is_enabled=True))
        await db.commit()
    app.state.safe_mode.engage(SafeModeReason.recovery_failed, "reconciliation failed")
    gate = recovery_bots.recovery_gate(app.state.session_factory, safe_mode=app.state.safe_mode)
    async with app.state.session_factory() as db:
        bot = await db.get(Bot, "bot1")
    refusal = await gate(bot)
    assert refusal is not None and "safe mode" in refusal


async def test_a_bot_is_not_recovered_over_an_unresolved_order(app: FastAPI) -> None:
    """Steps 10 and 6. Restarting over one is a second order waiting to happen."""
    await _unknown_order(app)
    async with app.state.session_factory() as db:
        db.add(
            Bot(
                id="bot1",
                user_id="owner",
                name="runner",
                mode="paper",
                paper_account_id="p1",
                is_enabled=True,
            )
        )
        await db.commit()
    gate = recovery_bots.recovery_gate(app.state.session_factory)
    async with app.state.session_factory() as db:
        bot = await db.get(Bot, "bot1")
    refusal = await gate(bot)
    assert refusal is not None
    assert "never established" in refusal
    assert "reconcile" in refusal


async def test_a_clean_account_passes_the_bot_gate(app: FastAPI) -> None:
    """The gate refuses; it does not refuse everything."""
    async with app.state.session_factory() as db:
        db.add(
            Bot(
                id="bot1",
                user_id="owner",
                name="runner",
                mode="paper",
                paper_account_id="p1",
                is_enabled=True,
            )
        )
        await db.commit()
    gate = recovery_bots.recovery_gate(app.state.session_factory)
    async with app.state.session_factory() as db:
        bot = await db.get(Bot, "bot1")
    assert await gate(bot) is None


async def test_a_bot_is_not_recovered_out_of_a_kill_switch(app: FastAPI) -> None:
    """Rule: a kill switch is a decision somebody made."""

    class _Risk:
        def status(self) -> dict[str, Any]:
            return {"kill_switches": {"global": True, "accounts": [], "strategies": []}}

    async with app.state.session_factory() as db:
        db.add(Bot(id="bot1", user_id="owner", name="runner", mode="paper", is_enabled=True))
        await db.commit()
    gate = recovery_bots.recovery_gate(app.state.session_factory, risk=_Risk())
    async with app.state.session_factory() as db:
        bot = await db.get(Bot, "bot1")
    refusal = await gate(bot)
    assert refusal is not None and "does not undo it" in refusal


# ==================================================== 6. releasing the latch


async def test_releasing_safe_mode_re_runs_the_sequence_and_refuses_while_it_still_holds(
    app: FastAPI, admin: AsyncClient
) -> None:
    """Step 18. Not a flag you can unset."""
    await _unknown_order(app)
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        await manager.run_startup(db, app)
        await db.commit()
    assert manager.safe_mode.engaged

    await _step_up_safe_mode(admin)
    r = await admin.post(
        "/v1/recovery/safe-mode/exit",
        headers=_csrf(admin),
        json={"reason": "we think it is fine now"},
    )
    assert r.status_code == 200
    # The condition is still true, so the latch is still shut.
    assert r.json()["safe_mode"]["engaged"] is True
    assert "UNKNOWN_ORDER_STATE" in str(r.json()["report"]["safe_mode_engaged"])


async def test_releasing_succeeds_once_the_condition_has_actually_cleared(
    app: FastAPI, admin: AsyncClient
) -> None:
    await _unknown_order(app)
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        await manager.run_startup(db, app)
        await db.commit()
    assert manager.safe_mode.engaged

    # Settle it the way the OMS would: the order stops being unresolved.
    async with app.state.session_factory() as db:
        row = await db.get(Order, "o-unknown")
        row.status = "failed"
        await db.commit()

    await _step_up_safe_mode(admin)
    r = await admin.post(
        "/v1/recovery/safe-mode/exit",
        headers=_csrf(admin),
        json={"reason": "reconciled with the venue; the order was never accepted"},
    )
    assert r.status_code == 200
    assert r.json()["safe_mode"]["engaged"] is False
    assert manager.safe_mode.engaged is False


async def test_a_release_is_audited(app: FastAPI, admin: AsyncClient) -> None:
    app.state.safe_mode.engage(SafeModeReason.operator, "a deliberate pause")
    await _step_up_safe_mode(admin)
    await admin.post(
        "/v1/recovery/safe-mode/exit",
        headers=_csrf(admin),
        json={"reason": "the maintenance window is over"},
    )
    async with app.state.session_factory() as db:
        from app.models.ops import AuditLog

        rows = list((await db.scalars(select(AuditLog))).all())
    entries = [r for r in rows if r.resource_type == "recovery"]
    assert entries
    assert entries[0].details["reason"] == "the maintenance window is over"


# =========================================================== 7. the API (§32)


async def test_recovery_status_needs_a_session(client: AsyncClient) -> None:
    assert (await client.get("/v1/recovery/status")).status_code == 401


async def test_acting_on_recovery_needs_authorization(app: FastAPI, client: AsyncClient) -> None:
    """Step 32. Dangerous recovery actions require permission."""
    await client.post("/auth/register", json=TRADER)
    await _promote(app, TRADER["email"], Role.trader)
    for path in (
        "/v1/recovery/reconcile",
        "/v1/recovery/safe-mode/enter",
        "/v1/recovery/safe-mode/exit",
    ):
        r = await client.post(path, headers=_csrf(client), json={"reason": "a good reason"})
        assert r.status_code == 403, path
    assert app.state.safe_mode.engaged is False


async def test_a_recovery_action_needs_a_reason(admin: AsyncClient) -> None:
    r = await admin.post("/v1/recovery/safe-mode/enter", headers=_csrf(admin), json={"reason": "x"})
    assert r.status_code == 422


async def test_entering_safe_mode_deliberately_records_the_operator(
    app: FastAPI, admin: AsyncClient
) -> None:
    r = await admin.post(
        "/v1/recovery/safe-mode/enter",
        headers=_csrf(admin),
        json={"reason": "maintenance on the broker terminal"},
    )
    assert r.status_code == 200
    assert r.json()["safe_mode"]["engaged"] is True
    reasons = r.json()["safe_mode"]["reasons"]
    assert reasons[0]["reason"] == "OPERATOR"
    assert reasons[0]["actor_user_id"]


async def test_the_reconcile_route_repairs_nothing(app: FastAPI, admin: AsyncClient) -> None:
    await _unknown_order(app)
    r = await admin.post(
        "/v1/recovery/reconcile", headers=_csrf(admin), json={"reason": "checking after a restart"}
    )
    assert r.status_code == 200
    assert r.json()["clean"] is False
    async with app.state.session_factory() as db:
        assert (await db.get(Order, "o-unknown")).status == "unknown"


async def test_the_contract_states_what_recovery_cannot_do(admin: AsyncClient) -> None:
    body = (await admin.get("/v1/recovery/contract")).json()
    cannot = " ".join(body["cannot"])
    assert "submit, cancel or modify an order" in cannot
    assert "enable live trading" in cannot
    assert "reset, drop or migrate a database" in cannot
    assert "asking the venue, never by retrying" in " ".join(body["rules"])


async def test_the_sequence_is_served_so_the_order_is_checkable(admin: AsyncClient) -> None:
    body = (await admin.get("/v1/recovery/sequence")).json()
    names = [s["step"] for s in body["sequence"]]
    assert names.index("broker") < names.index("broker_reconciliation")
    assert names.index("oms_reconciliation") < names.index("operational_mode")
    assert "DISCONNECTED -> EXECUTING" in body["rule"]


async def test_no_recovery_route_returns_a_secret(app: FastAPI, admin: AsyncClient) -> None:
    settings = app.state.settings
    for path in ("/v1/recovery/status", "/v1/recovery/contract", "/v1/recovery/sequence"):
        body = (await admin.get(path)).text
        assert settings.database_url not in body, path
        assert settings.redis_url not in body, path


# ================================================ 8. failure isolation (§21)


async def test_a_notification_failure_never_blocks_recovery(app: FastAPI) -> None:
    """Step 21 and rule 14."""

    class _BrokenHub:
        async def publish(self, event: Any) -> None:
            raise RuntimeError("bus down")

    await _unknown_order(app)
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    sent = await manager.announce(_BrokenHub(), report)
    assert sent == 0
    # The report and the latch survived the channel that did not.
    assert report.clean is False
    assert manager.safe_mode.engaged is True


async def test_recovery_announces_through_the_event_bus_only(app: FastAPI) -> None:
    """Step 28. One catalogued event; L34 decides what to do with it."""
    published: list[Any] = []

    class _Hub:
        async def publish(self, event: Any) -> None:
            published.append(event)

    await _unknown_order(app)
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        report = await manager.run_startup(db, app)
        await db.commit()
    await manager.announce(_Hub(), report)
    assert published
    assert all(e.type == "SYSTEM_ALERT" for e in published)
    assert all(e.channel == "system" for e in published)


# =========================================== 9. the startup wiring is real


async def test_the_application_runs_the_sequence_on_startup(settings: Settings) -> None:
    """Step 17. Not a route somebody has to remember to call.

    On the in-process bus: the lifespan starts the hub, which on the Redis bus
    would need a Redis. That is L07's behaviour and not what this test is
    about.
    """
    settings = settings.model_copy(update={"events_enabled": False})
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(settings, checks={}, engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        # The lifespan is what runs it.
        async with application.router.lifespan_context(application):
            await c.post("/auth/register", json=ADMIN)
            body = (await c.get("/v1/recovery/status")).json()
        assert body["startup"] is not None
        assert body["startup"]["kind"] == "startup"
        assert body["state"] in ("NORMAL", "DEGRADED", "SAFE_MODE")
    await engine.dispose()


async def test_starting_successfully_leaves_live_trading_off(app: FastAPI) -> None:
    """Rule 11 and rule 15."""
    manager: RecoveryManager = app.state.recovery
    async with app.state.session_factory() as db:
        await manager.run_startup(db, app)
    status = manager.status(app.state.settings)
    assert status["environment"]["trading_mode"] == "paper"
    assert status["environment"]["live_trading"] is False
    assert status["environment"]["live_execution_allowed"] is False


async def test_a_stale_bot_run_is_reported_and_never_silently_started(
    app: FastAPI,
) -> None:
    """Step 10. L22's plan_restart is asked, not told."""
    async with app.state.session_factory() as db:
        db.add(Bot(id="bot1", user_id="owner", name="runner", mode="paper", is_enabled=True))
        db.add(
            BotRun(
                id="run1",
                bot_id="bot1",
                started_at=utcnow() - timedelta(hours=1),
                status="running",
                last_heartbeat_at=utcnow() - timedelta(hours=1),
            )
        )
        await db.commit()
    step = await reconciliation.check_bots(app.state.session_factory)
    assert "Nothing was started" in step.detail
    async with app.state.session_factory() as db:
        run = await db.get(BotRun, "run1")
        assert run is not None and run.status != "running"


async def test_releasing_safe_mode_without_re_authentication_is_refused(
    app: FastAPI, admin: AsyncClient
) -> None:
    """L39, and the reason the three tests above call `_step_up_safe_mode`.

    A cookie is enough to READ the recovery status and enough to ENTER safe
    mode -- entering is the cautious direction. Leaving it is what lets orders
    flow again, so it costs the password.
    """
    app.state.safe_mode.engage(SafeModeReason.operator, "a deliberate pause")
    r = await admin.post(
        "/v1/recovery/safe-mode/exit",
        headers=_csrf(admin),
        json={"reason": "trying without confirming first"},
    )
    assert r.status_code == 403
    assert app.state.safe_mode.engaged is True, "refused and released anyway"


async def test_entering_safe_mode_needs_no_re_authentication(admin: AsyncClient) -> None:
    """The asymmetry, asserted rather than described.

    A control that is equally expensive to set and to lift is one that gets
    lifted by whoever is in a hurry; a control that is expensive to SET is one
    nobody engages in an emergency.
    """
    r = await admin.post(
        "/v1/recovery/safe-mode/enter",
        headers=_csrf(admin),
        json={"reason": "pausing while we investigate"},
    )
    assert r.status_code == 200


# ================ Tier-1 item 4: the sweep exists, and recovery still acts not


async def test_the_reconcile_sweep_is_registered_and_not_started(app: FastAPI) -> None:
    """Constructed and registered by `create_app`, started by nobody. The
    registry refuses a duplicate name, so this also proves the five workers
    the app builds have five distinct names."""
    workers = app.state.workers.workers
    assert "oms_reconcile" in workers
    sweep = workers["oms_reconcile"]
    assert app.state.oms_reconciler is sweep
    assert sweep.status.running is False
    assert sweep.store is app.state.order_store
    assert sweep.managers is app.state.order_managers
    report = sweep.report()
    assert "resume" in str(report["scope"]) and "load_unresolved" in str(report["scope"])
    assert "releases no safe-mode latch" in str(report["authority"])


async def test_the_startup_sequence_still_settles_no_order(app: FastAPI) -> None:
    """A regression fence on a deliberate decision. `recovery/reconciliation`
    counts unresolved orders and settles none -- "settling an order is an act
    against a venue and this module does not act". The sweep is a separate
    actor and boot does not start it, so an `unknown` order is still
    `unknown` after the whole startup sequence."""
    await _unknown_order(app)
    async with app.state.session_factory() as db:
        report = await app.state.recovery.run_startup(db, app)
        await db.commit()
    assert report.clean is False
    async with app.state.session_factory() as db:
        row = await db.get(Order, "o-unknown")
        assert row is not None and row.status == "unknown"
    assert app.state.oms_reconciler.status.running is False
    assert app.state.oms_reconciler.status.passes == 0
