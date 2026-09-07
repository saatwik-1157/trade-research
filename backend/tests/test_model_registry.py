"""The model registry and model lifecycle (L28).

The ones that matter most, and they are the rules the brief calls strict:

  * `test_a_candidate_cannot_reach_active_without_a_paper_deployment` — §12.
  * `test_registration_is_refused_without_a_passing_validation` — §9.
  * `test_a_tampered_artifact_is_never_loaded_for_trading` — §7.
  * `test_a_version_is_never_deleted` — §22.
  * `test_only_one_version_is_active_per_scope` — §14 and §31.
  * `test_a_rollback_to_an_unloadable_version_is_refused` — §20.
  * `test_the_registry_reaches_no_venue_and_enables_no_live_trading` — §49.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.ai import artifacts, comparison, lifecycle, resolution
from app.ai import registry_service as registry
from app.ai.lifecycle import LifecycleError, ModelStatus
from app.datasets import features as feature_engine
from app.db.base import Base
from app.models.ai import VERSION_STATUSES, AIModel, ModelVersion, TrainingRun
from app.models.ai_registry import ModelDeployment, ModelLifecycleEvent
from app.models.ops import AuditLog
from app.models.validation import ValidationRun
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

FEATURES = ["rsi_14", "atr_pct_14", "ema_spread_10_50", "return_5"]


def logistic_artifact(bias: float = 0.4) -> dict[str, Any]:
    return {
        "kind": "logistic",
        "coefficients": {
            "features": FEATURES,
            "weights": [0.1, -0.2, 0.3, 0.05],
            "bias": bias,
            "fitted_rows": 500,
        },
    }


# ================================================================= fixtures


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import app.auth.models  # noqa: F401
    import app.models  # noqa: F401

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        from app.auth.models import User

        session.add(User(id="u1", email="a@b.io", password_hash="x", role="admin"))
        await session.commit()
    yield factory
    await engine.dispose()


async def make_version(
    db: AsyncSession,
    *,
    key: str = "trade_probability",
    label: str = "1.0",
    verdict: str | None = "PASS",
    artifact: dict[str, Any] | None = None,
    feature_version: str | None = None,
    symbol_scope: str | None = None,
    timeframe_scope: str | None = None,
    status: str = "draft",
) -> ModelVersion:
    """A candidate with the lineage a real training run leaves behind."""
    model = await db.scalar(select(AIModel).where(AIModel.key == key))
    if model is None:
        model = AIModel(key=key, name=key, kind="classifier")
        db.add(model)
        await db.flush()
    sequence = len(
        list(
            (await db.scalars(select(ModelVersion).where(ModelVersion.model_id == model.id))).all()
        )
    )
    row = ModelVersion(
        model_id=model.id,
        version=sequence + 1,
        artifact_ref=f"{key}:{label}",
        status=status,
        features=FEATURES,
        feature_version=feature_version or feature_engine.FEATURE_SET_VERSION,
        label_version="1.0",
        dataset_version="1",
        dataset_fingerprint="d" * 32,
        code_version="1.0.0",
        params={"artifact": artifact if artifact is not None else logistic_artifact()},
        metrics={
            "classification": {"samples": 400, "accuracy": 0.55, "log_loss": 0.68},
            "economic": {"trades": 90, "profit_factor": 1.2},
        },
        symbol_scope=symbol_scope,
        timeframe_scope=timeframe_scope,
        training_start=datetime(2026, 1, 1),
        training_end=datetime(2026, 3, 1),
        test_start=datetime(2026, 4, 1),
        test_end=datetime(2026, 5, 1),
    )
    db.add(row)
    await db.flush()
    db.add(TrainingRun(model_version_id=row.id, status="validation_pending", model_key=key))
    if verdict is not None:
        db.add(
            ValidationRun(
                model_version_id=row.id,
                status="completed",
                verdict=verdict,
                summary=f"{verdict} on all checks",
                report={"verdict": verdict},
            )
        )
    await db.commit()
    await db.refresh(row)
    return row


async def registered(db: AsyncSession, **over: Any) -> ModelVersion:
    row = await make_version(db, **over)
    await registry.register(db, row, actor_user_id="u1")
    await db.refresh(row)
    return row


async def deployed(db: AsyncSession, scope: registry.Scope | None = None, **over: Any):  # noqa: ANN201
    row = await registered(db, **over)
    deployment = await registry.deploy_to_paper(
        db, row, scope or registry.Scope(), actor_user_id="u1", reason="paper run"
    )
    await db.refresh(row)
    return row, deployment


# ======================================================== 1. the lifecycle


def test_the_status_vocabulary_matches_the_schema() -> None:
    """One vocabulary. Neither side may gain a value the other lacks."""
    assert set(VERSION_STATUSES) == {str(s) for s in ModelStatus}


def test_every_declared_state_is_reachable() -> None:
    """§10, and the rule this project has applied since L22.

    A state nothing can enter makes the vocabulary a wish list, and a reader
    cannot tell an unused state from an unreachable one.
    """
    reachable = {ModelStatus.draft}
    for allowed in lifecycle.TRANSITIONS.values():
        reachable |= set(allowed)
    assert reachable == set(ModelStatus)


def test_there_is_exactly_one_edge_into_active() -> None:
    """§12. A newly trained model cannot reach the active state."""
    sources = [
        current
        for current, allowed in lifecycle.TRANSITIONS.items()
        if ModelStatus.promoted in allowed
    ]
    assert sources == [ModelStatus.paper]


def test_validated_does_not_serve_inference() -> None:
    """Validation says the evidence holds; registration says the artifact does."""
    assert ModelStatus.validated not in lifecycle.SERVING_STATUSES
    assert lifecycle.SERVING_STATUSES == {
        ModelStatus.registered,
        ModelStatus.paper,
        ModelStatus.promoted,
    }


def test_terminal_states_have_no_way_out() -> None:
    for status in lifecycle.TERMINAL:
        assert lifecycle.TRANSITIONS[status] == frozenset()
        with pytest.raises(LifecycleError) as exc:
            lifecycle.check_transition(status, ModelStatus.registered)
        assert "terminal" in str(exc.value)


def test_a_rolled_back_version_returns_to_the_pool_not_to_active() -> None:
    """§20. Re-activating it is a second deliberate act."""
    allowed = lifecycle.TRANSITIONS[ModelStatus.rolled_back]
    assert ModelStatus.registered in allowed
    assert ModelStatus.promoted not in allowed


def test_a_no_op_transition_is_refused() -> None:
    with pytest.raises(LifecycleError) as exc:
        lifecycle.check_transition(ModelStatus.paper, ModelStatus.paper)
    assert "already" in str(exc.value)


def test_the_declined_states_are_recorded_with_reasons() -> None:
    """ "We thought about it" and "we forgot" look identical in a schema."""
    assert set(lifecycle.DECLINED) == {"CANDIDATE", "VALIDATING", "FAILED", "ACTIVE"}
    for reason in lifecycle.DECLINED.values():
        assert len(reason) > 40


def test_an_unrecognised_status_does_not_serve_inference() -> None:
    """A row written by code this deployment does not have is not a yes."""
    assert lifecycle.serves_inference("promoted") is True
    assert lifecycle.serves_inference("something_else") is False


# ========================================================= 2. registration


async def test_registration_walks_draft_to_validated_to_registered(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row = await make_version(db)
        transition = await registry.register(db, row, actor_user_id="u1")
        await db.refresh(row)
        events = list(
            (
                await db.scalars(
                    select(ModelLifecycleEvent)
                    .where(ModelLifecycleEvent.model_version_id == row.id)
                    .order_by(ModelLifecycleEvent.occurred_at)
                )
            ).all()
        )

    assert row.status == "registered"
    assert transition.to_status is ModelStatus.registered
    # Both halves are recorded, so the history shows which gate the version got
    # through and which one it would have stopped at.
    assert [e.to_status for e in events] == ["validated", "registered"]
    assert row.artifact_sha256 and len(row.artifact_sha256) == 64
    assert row.artifact_kind == "logistic"
    assert row.validation_run_id
    assert row.training_run_id


async def test_registration_is_refused_without_a_passing_validation(sessions) -> None:  # noqa: ANN001
    """§9. Validation is never bypassed."""
    async with sessions() as db:
        never = await make_version(db, label="1.0", verdict=None)
        with pytest.raises(registry.RegistryError) as exc:
            await registry.register(db, never, actor_user_id="u1")
        assert "never completed a validation run" in str(exc.value)

        failed = await make_version(db, label="2.0", verdict="FAIL")
        with pytest.raises(registry.RegistryError) as exc:
            await registry.register(db, failed, actor_user_id="u1")
        assert "did not pass validation" in str(exc.value)


async def test_a_blocked_validation_is_refused_in_its_own_words(sessions) -> None:  # noqa: ANN001
    """BLOCKED is not FAIL, one level down."""
    async with sessions() as db:
        row = await make_version(db, verdict="BLOCKED")
        with pytest.raises(registry.RegistryError) as exc:
            await registry.register(db, row, actor_user_id="u1")
    assert "nothing was established either way" in str(exc.value)


async def test_a_conditional_verdict_registers(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row = await make_version(db, verdict="CONDITIONAL")
        await registry.register(db, row, actor_user_id="u1")
        await db.refresh(row)
    assert row.status == "registered"


async def test_a_version_with_no_artifact_is_rejected_not_registered(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row = await make_version(db, artifact={})
        with pytest.raises(registry.RegistryError):
            await registry.register(db, row, actor_user_id="u1")
        await db.refresh(row)
    # Rejected, and recorded as such. Not left as a draft that somebody retries.
    assert row.status == "rejected"


async def test_an_unknown_artifact_kind_is_rejected(sessions) -> None:  # noqa: ANN001
    """§33. A kind nobody recognises is refused at the earliest point."""
    async with sessions() as db:
        row = await make_version(db, artifact={"kind": "pickle", "path": "/etc/passwd"})
        with pytest.raises(registry.RegistryError) as exc:
            await registry.register(db, row, actor_user_id="u1")
        await db.refresh(row)
    assert "not one this deployment can load" in str(exc.value)
    assert row.status == "rejected"


async def test_a_feature_version_mismatch_is_rejected(sessions) -> None:  # noqa: ANN001
    """§16, at registration rather than at inference."""
    async with sessions() as db:
        row = await make_version(db, feature_version="0.9")
        with pytest.raises(registry.RegistryError) as exc:
            await registry.register(db, row, actor_user_id="u1")
    assert "a different quantity" in str(exc.value)


async def test_a_registered_version_cannot_be_registered_again(sessions) -> None:  # noqa: ANN001
    """§6. Re-registering would be modifying an immutable version in place."""
    async with sessions() as db:
        row = await registered(db)
        with pytest.raises(registry.RegistryError) as exc:
            await registry.register(db, row, actor_user_id="u1")
    assert "immutable" in str(exc.value)


async def test_the_schema_refuses_a_registered_version_with_no_digest(sessions) -> None:  # noqa: ANN001
    """The gate is in the database, not only in the service."""
    async with sessions() as db:
        row = await make_version(db)
        row.status = "registered"
        row.validation_run_id = "v1"
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_the_schema_refuses_a_registered_version_with_no_validation(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row = await make_version(db)
        row.status = "registered"
        row.artifact_sha256 = "a" * 64
        with pytest.raises(IntegrityError):
            await db.commit()


# ====================================================== 3. artifact integrity


def test_the_digest_is_stable_across_key_order() -> None:
    """Without this the check would fire the first time JSONB reordered a row."""
    a = {"kind": "logistic", "coefficients": {"bias": 1.0, "features": ["x"]}}
    b = {"coefficients": {"features": ["x"], "bias": 1.0}, "kind": "logistic"}
    assert artifacts.digest(a) == artifacts.digest(b)


def test_the_digest_changes_when_a_coefficient_changes() -> None:
    assert artifacts.digest(logistic_artifact(0.4)) != artifacts.digest(logistic_artifact(0.5))


async def test_a_tampered_artifact_is_never_loaded_for_trading(sessions) -> None:  # noqa: ANN001
    """§7. The whole reason the digest exists."""
    async with sessions() as db:
        row, _ = await deployed(db)
        assert artifacts.verify(row).intact

        # Somebody edits the coefficients in place. §6 says this must never
        # happen; the digest is what notices when it does.
        row.params = {"artifact": logistic_artifact(9.9)}
        await db.commit()

        check = artifacts.verify(row)
        assert not check.intact
        assert "does not match" in check.reason

        with pytest.raises(resolution.ResolutionError) as exc:
            await resolution.resolve(db, resolution.Request(model_key="trade_probability"))
    assert "cannot be trusted" in str(exc.value)


async def test_an_unregistered_version_is_unverifiable_not_verified(sessions) -> None:  # noqa: ANN001
    """ "Never checked" and "checked and fine" must not look the same."""
    async with sessions() as db:
        row = await make_version(db)
        check = artifacts.verify(row)
    assert not check.intact
    assert "never been registered" in check.reason


def test_an_oversized_artifact_is_refused() -> None:
    big = {"kind": "logistic", "coefficients": {"weights": [0.123456] * 200_000}}
    with pytest.raises(artifacts.ArtifactError) as exc:
        artifacts.inspect(big)
    assert "ceiling" in str(exc.value)


# ============================================================ 4. deployment


async def test_a_draft_cannot_be_deployed(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row = await make_version(db)
        with pytest.raises(registry.RegistryError) as exc:
            await registry.deploy_to_paper(db, row, registry.Scope(), actor_user_id="u1")
    assert "only a registered version" in str(exc.value)


async def test_deploying_puts_a_version_into_paper(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, deployment = await deployed(db)
    assert row.status == "paper"
    assert deployment.status == "active"
    assert deployment.environment == "paper"


async def test_deploy_to_paper_refuses_a_non_paper_environment(sessions) -> None:  # noqa: ANN001
    """§12. Paper comes first and the step is explicit."""
    async with sessions() as db:
        row = await registered(db)
        with pytest.raises(registry.RegistryError) as exc:
            await registry.deploy_to_paper(
                db, row, registry.Scope(environment="live"), actor_user_id="u1"
            )
    assert "paper comes first" in str(exc.value).lower()


async def test_only_one_version_is_active_per_scope(sessions) -> None:  # noqa: ANN001
    """§14, and §31: the DATABASE enforces it, not a check-then-write."""
    async with sessions() as db:
        first, _ = await deployed(db, label="1.0")
        second = await registered(db, label="2.0")
        db.add(
            ModelDeployment(
                model_version_id=second.id,
                model_key="trade_probability",
                model_version_label="2.0",
                scope_key="||",
                environment="paper",
                status="active",
                activated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_deploying_a_second_version_supersedes_the_first(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        first, _ = await deployed(db, label="1.0")
        second = await registered(db, label="2.0")
        await registry.deploy_to_paper(
            db, second, registry.Scope(), actor_user_id="u1", reason="v2 trial"
        )
        await db.refresh(first)
        await db.refresh(second)
        rows = list((await db.scalars(select(ModelDeployment))).all())

    assert second.status == "paper"
    # Back to the eligible pool, NOT retired: a rollback needs it available.
    assert first.status == "registered"
    assert sorted(r.status for r in rows) == ["active", "superseded"]
    active = next(r for r in rows if r.status == "active")
    assert active.previous_version_id == first.id


async def test_a_version_cannot_be_deployed_outside_its_own_scope(sessions) -> None:  # noqa: ANN001
    """§15. The registry prevents accidental use outside the intended scope."""
    async with sessions() as db:
        row = await registered(db, symbol_scope="EURUSD")
        with pytest.raises(registry.RegistryError) as exc:
            await registry.deploy_to_paper(
                db, row, registry.Scope(symbol="GBPUSD"), actor_user_id="u1"
            )
    assert "was fitted for EURUSD" in str(exc.value)


def test_an_empty_scope_axis_is_refused() -> None:
    """'' and None would be two spellings of one fact."""
    with pytest.raises(registry.RegistryError) as exc:
        registry.Scope(symbol="")
    assert "Use None" in str(exc.value)


# ============================================================= 5. promotion


async def test_a_candidate_cannot_reach_active_without_a_paper_deployment(sessions) -> None:  # noqa: ANN001
    """§12, the strict rule of the level."""
    async with sessions() as db:
        row = await registered(db)
        with pytest.raises(registry.RegistryError) as exc:
            await registry.promote(
                db, row, registry.Scope(), actor_user_id="u1", reason="looks good"
            )
    assert "only a paper-deployed version" in str(exc.value)
    assert "no shortcut" in str(exc.value)


async def test_promotion_requires_a_reason(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, _ = await deployed(db)
        with pytest.raises(registry.RegistryError) as exc:
            await registry.promote(db, row, registry.Scope(), actor_user_id="u1", reason="")
    assert "states its reason" in str(exc.value)


async def test_promotion_makes_the_version_the_one_a_scope_resolves_to(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, _ = await deployed(db)
        await registry.promote(
            db, row, registry.Scope(), actor_user_id="u1", reason="two weeks of paper"
        )
        await db.refresh(row)
        answer = await resolution.resolve(db, resolution.Request(model_key="trade_probability"))

    assert row.status == "promoted"
    assert row.promoted_at is not None
    assert row.promoted_by_user_id == "u1"
    assert answer.version_id == row.id


async def test_promotion_does_not_enable_live_trading(sessions) -> None:  # noqa: ANN001
    """§40 and §49."""
    from app.core.settings import LIVE_GATES, get_settings

    async with sessions() as db:
        row, _ = await deployed(db)
        await registry.promote(db, row, registry.Scope(), actor_user_id="u1", reason="ok")

    settings = get_settings()
    assert settings.trading_mode == "paper"
    assert settings.live_trading is False
    assert not any(LIVE_GATES.values())


# ============================================================== 6. rollback


async def test_a_rollback_restores_the_previous_version(sessions) -> None:  # noqa: ANN001
    """§20, end to end."""
    async with sessions() as db:
        first, _ = await deployed(db, label="1.0")
        second = await registered(db, label="2.0")
        await registry.deploy_to_paper(db, second, registry.Scope(), actor_user_id="u1")
        await registry.promote(
            db, second, registry.Scope(), actor_user_id="u1", reason="paper looked fine"
        )

        result = await registry.rollback(
            db, second, registry.Scope(), actor_user_id="u1", reason="live spread blew out"
        )
        await db.refresh(first)
        await db.refresh(second)
        answer = await resolution.resolve(db, resolution.Request(model_key="trade_probability"))

    assert second.status == "rolled_back"
    assert first.status == "paper"
    assert result["restored"] == first.id
    assert answer.version_id == first.id


async def test_a_rollback_to_an_unloadable_version_is_refused(sessions) -> None:  # noqa: ANN001
    """§20 and §44. Never blind, and it fails closed on the CURRENT version."""
    async with sessions() as db:
        first, _ = await deployed(db, label="1.0")
        second = await registered(db, label="2.0")
        await registry.deploy_to_paper(db, second, registry.Scope(), actor_user_id="u1")

        # The version we would roll back to has been corrupted meanwhile.
        first.params = {"artifact": logistic_artifact(7.7)}
        await db.commit()

        with pytest.raises(registry.RegistryError) as exc:
            await registry.rollback(
                db, second, registry.Scope(), actor_user_id="u1", reason="try to go back"
            )
        await db.refresh(second)

    assert "cannot be restored" in str(exc.value)
    assert "no active model at all is worse" in str(exc.value)
    # The current version is left active rather than withdrawn into a gap.
    assert second.status == "paper"


async def test_a_rollback_of_a_version_that_replaced_nothing_says_so(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, _ = await deployed(db)
        result = await registry.rollback(
            db, row, registry.Scope(), actor_user_id="u1", reason="bad idea"
        )
        await db.refresh(row)
    assert row.status == "rolled_back"
    assert result["restored"] is None
    assert "no trade, which is the safe reading" in result["note"]


async def test_rolling_back_a_version_that_is_not_deployed_is_refused(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row = await registered(db)
        with pytest.raises(registry.RegistryError):
            await registry.rollback(db, row, registry.Scope(), actor_user_id="u1", reason="x")


# ==================================================== 7. retirement (§21, §22)


async def test_a_version_is_never_deleted(sessions) -> None:  # noqa: ANN001
    """§22. Retirement is a status change and nothing more."""
    async with sessions() as db:
        row, _ = await deployed(db)
        await registry.stop_deployment(
            db, row, registry.Scope(), actor_user_id="u1", reason="pulled"
        )
        await registry.retire(db, row, actor_user_id="u1", reason="superseded by a rewrite")
        await db.refresh(row)
        still_there = await db.get(ModelVersion, row.id)
        events = await registry.history(db, row.id)

    assert row.status == "retired"
    assert still_there is not None
    assert still_there.params  # the artifact is kept
    assert len(events) >= 4  # validated, registered, paper, registered, retired


async def test_a_deployed_version_cannot_be_retired_under_a_running_strategy(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, _ = await deployed(db)
        with pytest.raises(registry.RegistryError) as exc:
            await registry.retire(db, row, actor_user_id="u1", reason="tidying up")
    assert "Stop or roll back the deployment first" in str(exc.value)


async def test_an_emergency_stop_deletes_nothing_and_frees_the_scope(sessions) -> None:  # noqa: ANN001
    """§21."""
    async with sessions() as db:
        row, deployment = await deployed(db)
        await registry.stop_deployment(
            db, row, registry.Scope(), actor_user_id="u1", reason="anomalous outputs"
        )
        await db.refresh(row)
        await db.refresh(deployment)
        with pytest.raises(resolution.ResolutionError):
            await resolution.resolve(db, resolution.Request(model_key="trade_probability"))

    assert row.status == "registered"
    assert deployment.status == "stopped"
    assert deployment.deactivated_at is not None


# =========================================================== 8. resolution


async def test_resolution_names_every_check_it_made(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, _ = await deployed(db)
        answer = await resolution.resolve(db, resolution.Request(model_key="trade_probability"))
    assert set(answer.checks) == {"status", "validation", "artifact", "features", "scope"}
    assert "never from 'latest'" in answer.as_dict()["note"]


async def test_a_more_specific_deployment_wins(sessions) -> None:  # noqa: ANN001
    """§29. Deterministic, and specific beats general."""
    async with sessions() as db:
        broad, _ = await deployed(db, label="1.0")
        narrow = await registered(db, label="2.0")
        await registry.deploy_to_paper(
            db, narrow, registry.Scope(strategy_key="breakout"), actor_user_id="u1"
        )
        answer = await resolution.resolve(
            db,
            resolution.Request(model_key="trade_probability", strategy_key="breakout"),
        )
        other = await resolution.resolve(
            db, resolution.Request(model_key="trade_probability", strategy_key="reversion")
        )

    assert answer.version_id == narrow.id
    assert other.version_id == broad.id


async def test_a_narrower_deployment_does_not_answer_a_broader_question(sessions) -> None:  # noqa: ANN001
    """A EURUSD model is not an answer to a question that named no symbol."""
    async with sessions() as db:
        row = await registered(db)
        await registry.deploy_to_paper(db, row, registry.Scope(symbol="EURUSD"), actor_user_id="u1")
        with pytest.raises(resolution.ResolutionError) as exc:
            await resolution.resolve(db, resolution.Request(model_key="trade_probability"))
    assert "none of their scopes contains this request" in str(exc.value)


async def test_a_retired_version_resolves_to_nothing(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, _ = await deployed(db)
        await registry.stop_deployment(db, row, registry.Scope(), actor_user_id="u1", reason="x")
        await registry.retire(db, row, actor_user_id="u1", reason="done with it")
        with pytest.raises(resolution.ResolutionError):
            await resolution.resolve(db, resolution.Request(model_key="trade_probability"))


async def test_an_unknown_model_or_environment_is_refused_with_a_reason(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        await deployed(db)
        with pytest.raises(resolution.ResolutionError) as exc:
            await resolution.resolve(db, resolution.Request(model_key="nope"))
        assert "no model 'nope'" in str(exc.value)
        with pytest.raises(resolution.ResolutionError) as exc:
            await resolution.resolve(
                db, resolution.Request(model_key="trade_probability", environment="demo")
            )
        assert "no active deployment" in str(exc.value)


# ================================================================ 9. caching


async def test_the_cache_is_keyed_by_version_not_by_scope(sessions) -> None:  # noqa: ANN001
    """§30. A lifecycle change needs no invalidation; the key changes."""
    async with sessions() as db:
        row, _ = await deployed(db)
        cache = resolution.ModelCache()
        request = resolution.Request(model_key="trade_probability")
        first, _ = await resolution.resolve_and_load(db, request, cache)
        second, _ = await resolution.resolve_and_load(db, request, cache)

    assert first is second
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


async def test_a_changed_artifact_evicts_the_cached_model(sessions) -> None:  # noqa: ANN001
    """§30. Never continue using an unauthorised version from a stale cache."""
    async with sessions() as db:
        row, _ = await deployed(db)
        cache = resolution.ModelCache()
        cache.get(row)
        assert cache.stats()["entries"] == 1

        row.params = {"artifact": logistic_artifact(3.3)}
        row.artifact_sha256 = artifacts.digest(row.params["artifact"])
        await db.commit()

        cache.get(row)
    assert cache.stats()["evictions"] == 1
    assert cache.stats()["misses"] == 2


# ============================================================ 10. comparison


async def test_a_comparison_leads_with_the_reasons_it_may_be_invalid(sessions) -> None:  # noqa: ANN001
    """§18. Do not compare metrics from incompatible datasets unlabelled."""
    async with sessions() as db:
        a = await make_version(db, label="1.0")
        b = await make_version(db, label="2.0")
        b.dataset_fingerprint = "e" * 32
        b.test_start = datetime(2026, 6, 1)
        b.test_end = datetime(2026, 7, 1)
        await db.commit()
        report = comparison.compare(a, b)

    assert report["comparable"] is False
    joined = " ".join(report["differences"])
    assert "different DATASETS" in joined
    assert "different EVALUATION PERIODS" in joined
    assert "does not say which is better" in report["reading"]["no_verdict"]


async def test_a_comparison_names_the_direction_of_every_delta(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        a = await make_version(db, label="1.0")
        b = await make_version(db, label="2.0")
        b.metrics = {
            "classification": {"samples": 400, "accuracy": 0.60, "log_loss": 0.60},
            "economic": {"trades": 90, "profit_factor": 1.5},
        }
        await db.commit()
        report = comparison.compare(a, b)

    accuracy = report["ml_metrics"]["delta"]["accuracy"]
    log_loss = report["ml_metrics"]["delta"]["log_loss"]
    assert accuracy["better"] == "b" and accuracy["lower_is_better"] is False
    # Log loss FELL, which is an improvement. A bare delta would have read the
    # opposite way.
    assert log_loss["delta"] < 0 and log_loss["better"] == "b"
    assert report["sample_sizes"]["b_trades"] == 90


def test_a_metric_measured_on_only_one_side_is_not_a_tie() -> None:
    out = comparison._deltas({"auc": 0.6}, {})
    assert out["auc"]["delta"] is None
    assert out["auc"]["better"] is None


# ============================================================ 11. the audit


async def test_every_transition_leaves_both_an_event_and_an_audit_row(sessions) -> None:  # noqa: ANN001
    """§19 and §35."""
    async with sessions() as db:
        row, _ = await deployed(db)
        events = list((await db.scalars(select(ModelLifecycleEvent))).all())
        audits = list(
            (
                await db.scalars(select(AuditLog).where(AuditLog.resource_type == "model_version"))
            ).all()
        )

    assert len(events) == len(audits) == 3  # validated, registered, paper
    for event in events:
        assert event.reason
        assert event.from_status and event.to_status
        assert event.occurred_at
    assert {a.action for a in audits} == {
        "model.validated",
        "model.registered",
        "model.paper",
    }


async def test_the_history_records_who_and_why(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row, _ = await deployed(db)
        await registry.promote(
            db, row, registry.Scope(), actor_user_id="u1", reason="two weeks of paper, 140 trades"
        )
        events = await registry.history(db, row.id)

    promotion = next(e for e in events if e["to"] == "promoted")
    assert promotion["actor_user_id"] == "u1"
    assert "140 trades" in promotion["reason"]
    assert promotion["from"] == "paper"


# ============================================================== 12. lineage


async def test_the_lineage_answers_why_this_version_is_active(sessions) -> None:  # noqa: ANN001
    """§8."""
    async with sessions() as db:
        row, _ = await deployed(db)
        chain = await registry.lineage(db, row)
        payload = chain.as_dict()

    assert payload["complete"] is True
    assert payload["training_run_id"]
    assert payload["validation_run_id"]
    assert payload["validation_verdict"] == "PASS"
    assert payload["dataset_fingerprint"]
    assert payload["feature_version"]
    assert payload["deployments"]


async def test_an_incomplete_lineage_shows_the_gap(sessions) -> None:  # noqa: ANN001
    async with sessions() as db:
        row = await make_version(db)
        row.dataset_fingerprint = None
        await db.commit()
        chain = await registry.lineage(db, row)
        complete, missing = chain.complete()

    assert complete is False
    assert "dataset_fingerprint" in missing


# ============================================================== 13. safety


REGISTRY_MODULES = [
    Path(__file__).parent.parent / "app" / "ai" / name
    for name in (
        "lifecycle.py",
        "artifacts.py",
        "registry_service.py",
        "resolution.py",
        "comparison.py",
    )
]

FORBIDDEN = (
    "app.execution",
    "app.oms",
    "app.risk",
    "app.sizing",
    "app.brokers",
    "app.orders",
    "app.positions",
    "app.paper",
    "app.bots",
)


def _names(path: Path) -> set[str]:
    """Every identifier and attribute this module actually USES.

    Parsed, so a docstring explaining a rule cannot fail the test that checks
    it. `ast` walks code; prose is not code.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_registry_reaches_no_venue_and_enables_no_live_trading() -> None:
    """§49, parsed."""
    for path in REGISTRY_MODULES:
        assert path.exists(), path
        for imported in _imports(path):
            for banned in FORBIDDEN:
                assert not imported.startswith(banned), f"{path.name} imports {imported}"
        # Names used in CODE, not text found in prose. Every one of these
        # modules explains in its docstring that it does not touch live
        # trading, and a grep would fail on the explanation -- which is the
        # defect L24's version of this test had, and L26 fixed the same way.
        # The SETTINGS, not any identifier that happens to share a word.
        # `strategy_config.py` takes a `trading_mode` parameter -- the journal's
        # paper/demo/live label for one decision -- which is a legitimate use of
        # the word and not a read of the global switch. What must be absent is
        # the settings module and the gate names themselves.
        assert not _names(path) & {
            "LIVE_TRADING",
            "live_trading",
            "LIVE_GATES",
            "live_execution_allowed",
            "get_settings",
        }, path.name
        for imported in _imports(path):
            assert not imported.startswith("app.core.settings"), path.name


def test_the_registry_deletes_nothing() -> None:
    """§22, parsed. There is no delete statement anywhere in the package."""
    for path in REGISTRY_MODULES:
        assert "delete" not in _names(path), path.name


def test_the_registry_does_not_retrain_or_train() -> None:
    """The registry accepts a candidate; it does not create one."""
    for path in REGISTRY_MODULES:
        for imported in _imports(path):
            assert not imported.startswith("app.training"), f"{path.name} imports {imported}"
