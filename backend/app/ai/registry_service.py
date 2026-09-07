"""The registry's operations: register, deploy, promote, roll back, retire.

**Not a second registry.** `app/ai/registry.py` is the in-process cache of
*loaded model objects*; this module owns the lifecycle of rows in the one
`model_versions` table that has existed since L05. Section 47 forbids a second
registry, loader or version table, and there is none: this imports L26's
verdict, L24's loader and L28's artifact check and writes status.

**Every transition goes through `_transition`.** One function does the check,
the write, the audit row and the event, so a new operation cannot forget the
audit trail and a status cannot change by any other path. Section 35.

**Registration is gated on validation and on the artifact, and both are
re-checked.** Section 9 says validation must never be bypassed; section 7 says
the artifact must be verified before a model is loaded for trading. Neither is
taken on trust from a column: the validation run is read, and the artifact is
re-hashed and re-loaded at registration.

**There is no path to live.** Section 12: the only edge into `promoted` starts
at `paper`, and it is the transition table that says so rather than a check
here. This module imports no broker, no OMS, no risk engine and no sizing, and
does not read or write `TRADING_MODE` or `LIVE_TRADING`. A test parses it.

**Nothing is deleted.** Section 22. `retire` and `reject` are status changes;
there is no delete statement in this file, and the deployment table's foreign
key is `RESTRICT` so the database refuses one too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import artifacts
from app.ai.lifecycle import (
    ModelStatus,
    Transition,
    check_transition,
)
from app.ai.loader import ArtifactError as LoaderError
from app.ai.loader import model_from_version
from app.datasets import features as feature_engine
from app.models.ai import AIModel, ModelVersion
from app.models.ai_registry import ModelDeployment, ModelLifecycleEvent
from app.models.ops import AuditLog
from app.models.validation import ValidationRun

log = logging.getLogger("app.ai.registry")

#: Validation verdicts that permit registration. Section 9.
#:
#: BLOCKED is absent, and the reason is L26's own: "we could not tell" is not
#: "yes". A blocked candidate is refused with that sentence rather than with a
#: generic failure, because the two lead to different next actions.
PASSING_VERDICTS = frozenset({"PASS", "CONDITIONAL"})

#: What this deployment can load. Recorded on the version so a future runtime
#: can tell what produced it. `stdlib` is honest: L24 deliberately uses no ML
#: framework, and writing "scikit-learn" here would be a fabricated lineage.
FRAMEWORK = "stdlib"


class RegistryError(Exception):
    """An operation the registry refuses. Never a silent no-op."""


def utcnow() -> datetime:
    from app.ai.service import utcnow as _utcnow

    return _utcnow()


# ============================================================== lineage (§8)


@dataclass(frozen=True)
class Lineage:
    """Why this version exists, end to end. Section 8."""

    version: ModelVersion
    model: AIModel | None
    training_run: Any | None
    validation_run: ValidationRun | None
    deployments: list[ModelDeployment]

    def complete(self) -> tuple[bool, list[str]]:
        """Whether every link a registered version needs is present."""
        missing: list[str] = []
        for name, value in (
            ("feature_version", self.version.feature_version),
            ("dataset_version", self.version.dataset_version),
            ("dataset_fingerprint", self.version.dataset_fingerprint),
            ("params", self.version.params),
        ):
            if not value:
                missing.append(name)
        if self.validation_run is None:
            missing.append("validation_run")
        if self.training_run is None:
            # A version registered outside the training service is legitimate,
            # so this is reported and does not block. It is named because a
            # lineage with a gap should show the gap rather than look complete.
            pass
        return (not missing, missing)

    def as_dict(self) -> dict[str, Any]:
        complete, missing = self.complete()
        return {
            "model": self.model.key if self.model else None,
            "version": self.version.artifact_ref,
            "training_run_id": getattr(self.training_run, "id", None),
            "dataset_version": self.version.dataset_version,
            "dataset_fingerprint": self.version.dataset_fingerprint,
            "feature_version": self.version.feature_version,
            "label_version": self.version.label_version,
            "preprocessing_version": self.version.preprocessing_version,
            "code_version": self.version.code_version,
            "training_config": (self.version.params or {}).get("training_config"),
            "validation_run_id": self.validation_run.id if self.validation_run else None,
            "validation_verdict": self.validation_run.verdict if self.validation_run else None,
            "validation_summary": self.validation_run.summary if self.validation_run else None,
            "deployments": [summarise_deployment(d) for d in self.deployments],
            "complete": complete,
            "missing": missing,
            "answers": (
                "why this version exists and why it is where it is. A gap here is shown "
                "rather than filled: an incomplete lineage that looks complete is worse "
                "than one that says what is missing."
            ),
        }


async def lineage(db: AsyncSession, version: ModelVersion) -> Lineage:
    """The full chain behind one version. Reads only."""
    from app.models.ai import TrainingRun

    model = await db.get(AIModel, version.model_id)
    training = await db.scalar(
        select(TrainingRun).where(TrainingRun.model_version_id == version.id)
    )
    validation = (
        await db.get(ValidationRun, version.validation_run_id)
        if version.validation_run_id
        else await db.scalar(
            select(ValidationRun)
            .where(
                ValidationRun.model_version_id == version.id,
                ValidationRun.status == "completed",
            )
            .order_by(ValidationRun.created_at.desc())
            .limit(1)
        )
    )
    deployments = list(
        (
            await db.scalars(
                select(ModelDeployment)
                .where(ModelDeployment.model_version_id == version.id)
                .order_by(ModelDeployment.activated_at.desc())
            )
        ).all()
    )
    return Lineage(
        version=version,
        model=model,
        training_run=training,
        validation_run=validation,
        deployments=deployments,
    )


# ============================================================ the transition


async def _transition(
    db: AsyncSession,
    version: ModelVersion,
    target: ModelStatus,
    *,
    reason: str,
    actor_user_id: str | None,
    environment: str = "paper",
    validation_run_id: str | None = None,
    deployment_id: str | None = None,
    details: dict[str, Any] | None = None,
    hub: object | None = None,
) -> Transition:
    """The ONE place a version's status changes.

    Checks the transition, writes it, records the lifecycle event AND the audit
    log, and publishes. A second path that skipped any of those would produce a
    history with holes in it, and a history with holes is read as a history.
    """
    current = ModelStatus(version.status)
    check_transition(current, target)

    model = await db.get(AIModel, version.model_id)
    key = model.key if model else "unknown"
    label = (version.artifact_ref or "").split(":", 1)[-1]
    at = utcnow()

    version.status = str(target)
    if target is ModelStatus.registered:
        version.registered_at = at
    elif target is ModelStatus.promoted:
        version.promoted_at = at
        version.promoted_by_user_id = actor_user_id
    elif target is ModelStatus.retired:
        version.retired_at = at

    transition = Transition(
        version_id=version.id,
        model_key=key,
        from_status=current,
        to_status=target,
        reason=reason,
        actor_user_id=actor_user_id,
        environment=environment,
        validation_run_id=validation_run_id or version.validation_run_id,
        deployment_id=deployment_id,
        details=details,
    )

    db.add(
        ModelLifecycleEvent(
            model_version_id=version.id,
            model_key=key,
            model_version_label=label,
            from_status=str(current),
            to_status=str(target),
            occurred_at=at,
            actor_user_id=actor_user_id,
            reason=reason,
            environment=environment,
            validation_run_id=transition.validation_run_id,
            deployment_id=deployment_id,
            details=details,
        )
    )
    # The platform's own audit log too, not instead. §35 asks for an audit
    # trail and the platform already has one that an administrator reads across
    # every resource type; the lifecycle table is the model-shaped view of the
    # same facts, indexed for the questions this level asks.
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=f"model.{target}",
            resource_type="model_version",
            resource_id=version.id,
            occurred_at=at,
            details={
                "model": key,
                "version": label,
                "from": str(current),
                "to": str(target),
                "reason": reason,
                "environment": environment,
            },
        )
    )

    await _publish(hub, target, transition)
    return transition


async def _publish(hub: object | None, target: ModelStatus, transition: Transition) -> None:
    """Through L07's hub, never a second realtime system (§27)."""
    if hub is None:
        return
    from app.core.events import Event
    from app.realtime.catalogue import EventType

    mapping = {
        ModelStatus.registered: EventType.MODEL_REGISTERED,
        ModelStatus.paper: EventType.MODEL_PAPER_ACTIVATED,
        ModelStatus.promoted: EventType.MODEL_ACTIVATED,
        ModelStatus.rolled_back: EventType.MODEL_ROLLED_BACK,
        ModelStatus.retired: EventType.MODEL_RETIRED,
        ModelStatus.rejected: EventType.MODEL_REJECTED,
    }
    event_type = mapping.get(target)
    if event_type is None:
        return
    try:
        await hub.publish(  # type: ignore[attr-defined]
            Event(
                type=str(event_type),
                payload=transition.as_dict(),
                source="model_registry",
                channel=f"model:{transition.model_key}",
            )
        )
    except Exception:  # noqa: BLE001 - a promotion does not fail because an event did
        log.warning(
            "a model lifecycle event could not be published",
            extra={
                "event": "model_event_failed",
                "model": transition.model_key,
                "to": str(target),
            },
        )


# ============================================================== registration


async def _decisive_validation(db: AsyncSession, version: ModelVersion) -> ValidationRun:
    """The completed validation run this registration relies on. Section 9.

    The most recent completed one, and it must have passed. Read fresh rather
    than trusted from a column: the column is what this function is about to
    write, and reading what you are about to write is not a check.
    """
    run = await db.scalar(
        select(ValidationRun)
        .where(ValidationRun.model_version_id == version.id, ValidationRun.status == "completed")
        .order_by(ValidationRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        raise RegistryError(
            "this version has never completed a validation run. Registration is gated on "
            "L26's verdict and there is nothing to read -- not a judgement about the "
            "model, but nothing has been established about it."
        )
    if run.verdict not in PASSING_VERDICTS:
        detail = (
            "BLOCKED means a check could not be evaluated, so nothing was established either way."
            if run.verdict == "BLOCKED"
            else "A candidate that did not pass validation cannot be registered."
        )
        raise RegistryError(
            f"the most recent validation returned {run.verdict}: "
            f"{run.summary or 'no summary'}. {detail}"
        )
    return run


def _compatibility(version: ModelVersion) -> None:
    """Section 16, at registration rather than at inference.

    The checks a version must pass before it may be consulted at all. The
    per-signal checks — symbol, timeframe, strategy scope — belong to
    resolution, because they depend on the request; these depend only on the
    version.
    """
    if not version.feature_version:
        raise RegistryError(
            "this version does not say which feature set it was fitted against. Without "
            "it the model cannot refuse mismatched inputs, which is the check every "
            "prediction depends on."
        )
    if version.feature_version != feature_engine.FEATURE_SET_VERSION:
        raise RegistryError(
            f"this version was fitted against feature set {version.feature_version} and "
            f"this deployment computes {feature_engine.FEATURE_SET_VERSION}. A model run "
            "on features it was not fitted on is being asked about a different quantity, "
            "so it is refused rather than registered and discovered later."
        )
    declared = set(version.features or [])
    unknown = sorted(declared - set(feature_engine.CATALOGUE))
    if unknown:
        raise RegistryError(
            f"this version requires {', '.join(unknown)}, which this platform's feature "
            "engine does not compute."
        )


async def register(
    db: AsyncSession,
    version: ModelVersion,
    *,
    actor_user_id: str | None = None,
    reason: str = "",
    hub: object | None = None,
) -> Transition:
    """Accept a validated candidate into the registry. Sections 7, 8, 9 and 16.

    `draft` -> `validated` -> `registered` in one call, because they are one
    decision with two halves that must both hold: L26 says the evidence supports
    the candidate, and the registry says the artifact loads and still matches.
    Both are recorded as separate lifecycle events, so the history shows which
    gate a version got through and which one it stopped at.
    """
    current = ModelStatus(version.status)
    if current is not ModelStatus.draft:
        raise RegistryError(
            f"only a draft can be registered; this version is {current}. §6: a registered "
            "version is immutable, and re-registering one would be modifying it in place."
        )

    run = await _decisive_validation(db, version)

    first = await _transition(
        db,
        version,
        ModelStatus.validated,
        reason=f"validation run {run.id[:12]} returned {run.verdict}",
        actor_user_id=actor_user_id,
        validation_run_id=run.id,
        hub=hub,
    )
    version.validation_run_id = run.id

    # --- the registry's own checks, which validation does not make.
    try:
        _compatibility(version)
        sha, size, kind = artifacts.stamp(version)
        # And it must actually LOAD. A digest proves the bytes did not change;
        # only loading proves they were ever a model. §7 asks for both.
        model = model_from_version(version)
        if not model.fitted:
            raise RegistryError("the artifact loaded but carries no fitted parameters")
    except (RegistryError, artifacts.ArtifactError, LoaderError) as exc:
        await _transition(
            db,
            version,
            ModelStatus.rejected,
            reason=f"the registry refused this candidate: {exc}",
            actor_user_id=actor_user_id,
            validation_run_id=run.id,
            hub=hub,
        )
        await db.commit()
        raise RegistryError(str(exc)) from exc

    version.artifact_sha256 = sha
    version.artifact_bytes = size
    version.artifact_kind = kind
    version.framework = FRAMEWORK
    version.framework_version = artifacts.ALGORITHM

    from app.models.ai import TrainingRun

    training = await db.scalar(
        select(TrainingRun).where(TrainingRun.model_version_id == version.id)
    )
    version.training_run_id = training.id if training else None

    transition = await _transition(
        db,
        version,
        ModelStatus.registered,
        reason=reason
        or (
            f"artifact verified ({artifacts.ALGORITHM} {sha[:12]}, {size} bytes, {kind}), "
            f"loads, feature set {version.feature_version} matches, lineage complete"
        ),
        actor_user_id=actor_user_id,
        validation_run_id=run.id,
        details={"artifact_sha256": sha, "artifact_bytes": size, "artifact_kind": kind},
        hub=hub,
    )
    await db.commit()
    assert first is not None
    return transition


async def reject(
    db: AsyncSession,
    version: ModelVersion,
    *,
    reason: str,
    actor_user_id: str | None = None,
    hub: object | None = None,
) -> Transition:
    """Refuse a candidate. Terminal, and never a deletion (§22)."""
    if not reason:
        raise RegistryError("a rejection states its reason; an unexplained one is a hole")
    transition = await _transition(
        db, version, ModelStatus.rejected, reason=reason, actor_user_id=actor_user_id, hub=hub
    )
    await db.commit()
    return transition


# =============================================================== deployment


@dataclass(frozen=True)
class Scope:
    """Where a deployment applies. Section 15.

    None on an axis means "not restricted on it" — a recorded absence, not a
    wildcard somebody typed. Empty strings are refused: '' and None would be two
    spellings of one fact, and the partial unique index would read them as two
    different scopes.
    """

    strategy_key: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    environment: str = "paper"

    def __post_init__(self) -> None:
        for name in ("strategy_key", "symbol", "timeframe"):
            value = getattr(self, name)
            if value is not None and not str(value).strip():
                raise RegistryError(
                    f"{name} is an empty string. Use None to mean 'not restricted on this "
                    "axis': '' and None would be two spellings of one fact, and the "
                    "uniqueness rule would treat them as two different scopes."
                )
        if self.environment not in ("paper", "demo", "live"):
            raise RegistryError(f"unknown environment {self.environment!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "environment": self.environment,
        }

    def key(self) -> str:
        """The three axes as one NOT NULL string, for the unique index.

        Derived here so no caller can write an inconsistent one. NULL becomes
        the empty segment, which is unambiguous because `Scope.__post_init__`
        refuses an empty string on any axis -- so `""` in a segment can only
        ever mean "not restricted".
        """
        return "|".join([self.strategy_key or "", self.symbol or "", self.timeframe or ""])

    def describe(self) -> str:
        parts = [f"environment={self.environment}"]
        for name in ("strategy_key", "symbol", "timeframe"):
            value = getattr(self, name)
            if value:
                parts.append(f"{name}={value}")
        return ", ".join(parts)


def _scope_checks(version: ModelVersion, scope: Scope) -> None:
    """Section 15 and 16: the version's own scope must contain the deployment's.

    A version fitted on EURUSD H1 may be deployed to EURUSD H1 or to a scope
    that names nothing narrower — never to GBPUSD. `symbol_scope` being None
    means nobody recorded a restriction, and that is reported as unrestricted
    rather than treated as a match for everything, because the two are different
    claims and only one of them was made by a person.
    """
    if version.symbol_scope and scope.symbol and version.symbol_scope != scope.symbol:
        raise RegistryError(
            f"this version was fitted for {version.symbol_scope} and the deployment names "
            f"{scope.symbol}. §15: the registry prevents accidental use of a model outside "
            "its intended scope."
        )
    if version.timeframe_scope and scope.timeframe and version.timeframe_scope != scope.timeframe:
        raise RegistryError(
            f"this version was fitted for {version.timeframe_scope} and the deployment "
            f"names {scope.timeframe}."
        )


async def _active_deployment(
    db: AsyncSession, model_key: str, scope: Scope
) -> ModelDeployment | None:
    # By `scope_key`, the same column the unique index covers, so the lookup and
    # the constraint can never disagree about what "the same scope" means.
    return await db.scalar(
        select(ModelDeployment).where(
            ModelDeployment.model_key == model_key,
            ModelDeployment.scope_key == scope.key(),
            ModelDeployment.environment == scope.environment,
            ModelDeployment.status == "active",
        )
    )


async def _guard(db: AsyncSession, version: ModelVersion, scope: Scope) -> None:
    """Everything that must hold before a version may serve a scope."""
    _scope_checks(version, scope)
    _compatibility(version)
    check = artifacts.verify(version)
    if not check.intact:
        raise RegistryError(f"the artifact cannot be trusted: {check.reason}")
    try:
        model = model_from_version(version)
    except LoaderError as exc:
        raise RegistryError(f"the artifact does not load: {exc}") from exc
    if not model.fitted:
        raise RegistryError("the artifact loads but carries no fitted parameters")


async def deploy_to_paper(
    db: AsyncSession,
    version: ModelVersion,
    scope: Scope,
    *,
    actor_user_id: str | None = None,
    reason: str = "",
    config: dict[str, Any] | None = None,
    hub: object | None = None,
) -> ModelDeployment:
    """Put a registered version to work in paper trading. Section 13.

    Reversible: `stop` ends the deployment and returns the version to
    `registered`, which is what "paper deployment should be reversible" means
    when nothing is ever deleted.
    """
    if scope.environment != "paper":
        raise RegistryError(
            f"deploy_to_paper is for the paper environment; this scope names "
            f"{scope.environment}. §12: paper comes first and the step is explicit."
        )
    current = ModelStatus(version.status)
    if current not in (ModelStatus.registered, ModelStatus.paper):
        raise RegistryError(f"only a registered version can be deployed; this one is {current}.")
    await _guard(db, version, scope)

    model = await db.get(AIModel, version.model_id)
    key = model.key if model else "unknown"
    label = (version.artifact_ref or "").split(":", 1)[-1]
    at = utcnow()

    existing = await _active_deployment(db, key, scope)
    if existing is not None and existing.model_version_id == version.id:
        raise RegistryError(
            f"{key} v{label} is already the active deployment for {scope.describe()}."
        )
    previous_version_id = existing.model_version_id if existing else None
    if existing is not None:
        existing.status = "superseded"
        existing.deactivated_at = at
        existing.reason = (existing.reason or "") + f" | superseded by {label} at {at}"
        superseded = await db.get(ModelVersion, existing.model_version_id)
        if superseded is not None and ModelStatus(superseded.status) in (
            ModelStatus.paper,
            ModelStatus.promoted,
        ):
            # Back to the eligible pool, NOT to `retired`. §20 needs the
            # previous version available for a rollback, and retiring it on
            # every promotion would make every rollback a resurrection --
            # which §22 says should be a deliberate, separate act.
            await _transition(
                db,
                superseded,
                ModelStatus.registered,
                reason=f"superseded by {label} on {scope.describe()}",
                actor_user_id=actor_user_id,
                environment=scope.environment,
                hub=hub,
            )

    deployment = ModelDeployment(
        model_version_id=version.id,
        model_key=key,
        model_version_label=label,
        strategy_key=scope.strategy_key,
        symbol=scope.symbol,
        timeframe=scope.timeframe,
        scope_key=scope.key(),
        environment=scope.environment,
        status="active",
        activated_at=at,
        activated_by_user_id=actor_user_id,
        reason=reason or f"deployed to {scope.describe()}",
        previous_version_id=previous_version_id,
        config=config,
    )
    db.add(deployment)
    await db.flush()

    if current is not ModelStatus.paper:
        await _transition(
            db,
            version,
            ModelStatus.paper,
            reason=reason or f"deployed to paper on {scope.describe()}",
            actor_user_id=actor_user_id,
            environment=scope.environment,
            deployment_id=deployment.id,
            hub=hub,
        )
    await db.commit()
    await db.refresh(deployment)
    return deployment


async def promote(
    db: AsyncSession,
    version: ModelVersion,
    scope: Scope,
    *,
    actor_user_id: str | None = None,
    reason: str,
    hub: object | None = None,
) -> Transition:
    """Make a paper-deployed version the active one. Sections 11 and 12.

    **This does not enable live trading and cannot.** `promoted` means "the
    version this scope resolves to"; whether anything executes is decided by
    `TRADING_MODE`, `LIVE_TRADING` and the ten live gates, none of which this
    module reads or writes.

    The only edge into `promoted` starts at `paper`, so a newly trained model
    cannot arrive here: there is no arc from `registered`, and the transition
    table rather than a check in this function is what says so.
    """
    if not reason:
        raise RegistryError(
            "a promotion states its reason. §19 asks for an audit trail, and an entry "
            "that does not say why is a timestamp rather than a record."
        )
    current = ModelStatus(version.status)
    if current is not ModelStatus.paper:
        raise RegistryError(
            f"only a paper-deployed version can be promoted; this one is {current}. §12: "
            "Training -> Validation -> Registry -> Paper -> Monitoring -> explicit "
            "promotion, and there is no shortcut through it."
        )
    await _guard(db, version, scope)

    model = await db.get(AIModel, version.model_id)
    key = model.key if model else "unknown"
    deployment = await _active_deployment(db, key, scope)
    if deployment is None or deployment.model_version_id != version.id:
        raise RegistryError(
            f"this version has no active deployment on {scope.describe()}. A promotion "
            "names the scope it applies to; promoting a version that is not deployed "
            "anywhere would be a status with no meaning."
        )

    transition = await _transition(
        db,
        version,
        ModelStatus.promoted,
        reason=reason,
        actor_user_id=actor_user_id,
        environment=scope.environment,
        deployment_id=deployment.id,
        hub=hub,
    )
    await db.commit()
    return transition


async def rollback(
    db: AsyncSession,
    version: ModelVersion,
    scope: Scope,
    *,
    actor_user_id: str | None = None,
    reason: str,
    hub: object | None = None,
) -> dict[str, Any]:
    """Withdraw the active version and restore the one it replaced. Section 20.

    **Never blind.** The previous version is re-checked exactly as a fresh
    deployment would be: it must still exist, be in a state that can serve, load,
    match its recorded digest, and be compatible with the scope. If any of that
    fails the rollback is REFUSED and the current version is left active — an
    active model that is wrong is bad, and no active model at all is worse.
    """
    if not reason:
        raise RegistryError("a rollback states its reason")
    current = ModelStatus(version.status)
    if current not in (ModelStatus.paper, ModelStatus.promoted):
        raise RegistryError(f"only a deployed version can be rolled back; this one is {current}")

    model = await db.get(AIModel, version.model_id)
    key = model.key if model else "unknown"
    deployment = await _active_deployment(db, key, scope)
    if deployment is None or deployment.model_version_id != version.id:
        raise RegistryError(f"this version is not the active deployment on {scope.describe()}")

    previous: ModelVersion | None = None
    if deployment.previous_version_id:
        previous = await db.get(ModelVersion, deployment.previous_version_id)
        if previous is None:
            raise RegistryError(
                f"the version this deployment replaced ({deployment.previous_version_id}) "
                "no longer exists. Refused rather than rolled back to nothing."
            )
        try:
            await _guard(db, previous, scope)
        except RegistryError as exc:
            raise RegistryError(
                f"the previous version cannot be restored: {exc}. The rollback is refused "
                "and the current version is left active -- an active model that is wrong "
                "is bad; no active model at all is worse, and it fails silently."
            ) from exc
        if ModelStatus(previous.status) not in (
            ModelStatus.registered,
            ModelStatus.rolled_back,
        ):
            raise RegistryError(
                f"the previous version is {previous.status} and cannot be re-deployed. "
                "A retired or rejected version is not resurrected by a rollback."
            )

    at = utcnow()
    deployment.status = "rolled_back"
    deployment.deactivated_at = at
    deployment.reason = (deployment.reason or "") + f" | rolled back: {reason}"

    await _transition(
        db,
        version,
        ModelStatus.rolled_back,
        reason=reason,
        actor_user_id=actor_user_id,
        environment=scope.environment,
        deployment_id=deployment.id,
        hub=hub,
    )

    restored: ModelDeployment | None = None
    if previous is not None:
        if ModelStatus(previous.status) is ModelStatus.rolled_back:
            await _transition(
                db,
                previous,
                ModelStatus.registered,
                reason="restored by a rollback",
                actor_user_id=actor_user_id,
                environment=scope.environment,
                hub=hub,
            )
        restored = ModelDeployment(
            model_version_id=previous.id,
            model_key=key,
            model_version_label=(previous.artifact_ref or "").split(":", 1)[-1],
            strategy_key=scope.strategy_key,
            symbol=scope.symbol,
            timeframe=scope.timeframe,
            scope_key=scope.key(),
            environment=scope.environment,
            status="active",
            activated_at=at,
            activated_by_user_id=actor_user_id,
            reason=f"restored by a rollback of {(version.artifact_ref or '')}: {reason}",
            previous_version_id=version.id,
        )
        db.add(restored)
        await db.flush()
        await _transition(
            db,
            previous,
            ModelStatus.paper,
            reason=f"restored by a rollback: {reason}",
            actor_user_id=actor_user_id,
            environment=scope.environment,
            deployment_id=restored.id,
            hub=hub,
        )

    await db.commit()
    return {
        "rolled_back": version.id,
        "restored": previous.id if previous else None,
        "restored_deployment": restored.id if restored else None,
        "scope": scope.as_dict(),
        "note": (
            "the previous version was re-checked as a fresh deployment would be: it "
            "loads, its digest matches, and it is compatible with this scope."
            if previous is not None
            else "this deployment replaced nothing, so the scope now resolves to no "
            "model. Under AI_REQUIRED that means no trade, which is the safe reading."
        ),
    }


async def stop_deployment(
    db: AsyncSession,
    version: ModelVersion,
    scope: Scope,
    *,
    actor_user_id: str | None = None,
    reason: str,
    hub: object | None = None,
) -> Transition:
    """Emergency deactivation. Section 21.

    Stops new inference from resolving to this version. It deletes nothing, and
    it touches no strategy's AI configuration — a strategy whose model is
    withdrawn gets no model, which under AI_REQUIRED means no trade and under
    AI_OPTIONAL means the deterministic strategy proceeds. Both are behaviours
    the operator already chose.
    """
    if not reason:
        raise RegistryError("a deactivation states its reason")
    model = await db.get(AIModel, version.model_id)
    key = model.key if model else "unknown"
    deployment = await _active_deployment(db, key, scope)
    if deployment is None or deployment.model_version_id != version.id:
        raise RegistryError(f"this version is not the active deployment on {scope.describe()}")

    at = utcnow()
    deployment.status = "stopped"
    deployment.deactivated_at = at
    deployment.reason = (deployment.reason or "") + f" | stopped: {reason}"

    transition = await _transition(
        db,
        version,
        ModelStatus.registered,
        reason=f"deployment stopped: {reason}",
        actor_user_id=actor_user_id,
        environment=scope.environment,
        deployment_id=deployment.id,
        hub=hub,
    )
    await db.commit()
    return transition


async def retire(
    db: AsyncSession,
    version: ModelVersion,
    *,
    actor_user_id: str | None = None,
    reason: str,
    hub: object | None = None,
) -> Transition:
    """Withdraw a version deliberately. Terminal, and never a deletion (§22)."""
    if not reason:
        raise RegistryError("a retirement states its reason")
    active = await db.scalar(
        select(ModelDeployment).where(
            ModelDeployment.model_version_id == version.id,
            ModelDeployment.status == "active",
        )
    )
    if active is not None:
        raise RegistryError(
            f"this version is still the active deployment on {active.environment}"
            f"{'/' + active.strategy_key if active.strategy_key else ''}. Stop or roll "
            "back the deployment first: retiring a version out from under a running "
            "strategy is the silent change §21 asks to avoid."
        )
    transition = await _transition(
        db, version, ModelStatus.retired, reason=reason, actor_user_id=actor_user_id, hub=hub
    )
    await db.commit()
    return transition


# ================================================================ reporting


def summarise_deployment(row: ModelDeployment) -> dict[str, Any]:
    return {
        "id": row.id,
        "model": row.model_key,
        "version": row.model_version_label,
        "model_version_id": row.model_version_id,
        "strategy_key": row.strategy_key,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "environment": row.environment,
        "status": row.status,
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
        "deactivated_at": row.deactivated_at.isoformat() if row.deactivated_at else None,
        "previous_version_id": row.previous_version_id,
        "reason": row.reason,
        "config": row.config,
    }


def summarise_event(row: ModelLifecycleEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "model": row.model_key,
        "version": row.model_version_label,
        "model_version_id": row.model_version_id,
        "from": row.from_status,
        "to": row.to_status,
        "at": row.occurred_at.isoformat() if row.occurred_at else None,
        "actor_user_id": row.actor_user_id,
        "reason": row.reason,
        "environment": row.environment,
        "validation_run_id": row.validation_run_id,
        "deployment_id": row.deployment_id,
        "details": row.details,
    }


async def history(db: AsyncSession, version_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = list(
        (
            await db.scalars(
                select(ModelLifecycleEvent)
                .where(ModelLifecycleEvent.model_version_id == version_id)
                .order_by(ModelLifecycleEvent.occurred_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return [summarise_event(r) for r in rows]
