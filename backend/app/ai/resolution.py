"""Which model version does this request resolve to, and may it be used?

Section 29. A resolution takes a scope — model family, strategy, symbol,
timeframe, environment — and returns one version, or a refusal that says why.

**Deterministic, and specific beats general.** A deployment naming a strategy
wins over one that names none; a deployment naming a symbol wins over one that
does not. Ties are impossible: the partial unique index makes at most one
`active` deployment per exact scope, so the ordering below picks a unique row
rather than the first of several equally good ones.

**Five things must hold before a version is returned.** The version exists, its
status serves inference, validation passed, the artifact verifies, and it is
compatible with the request. All five are re-checked on the way out of the
cache, because a cached "yes" from before a rollback is exactly the stale
authorisation §30 warns about.

**Never `latest`.** A resolution answers "what is deployed here", not "what is
newest". A model that changed because someone registered a new version, without
anyone deploying it, is a strategy nobody can reproduce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import artifacts
from app.ai.base import BaseModel
from app.ai.lifecycle import serves_inference
from app.ai.loader import ArtifactError, model_from_version
from app.datasets import features as feature_engine
from app.models.ai import AIModel, ModelVersion
from app.models.ai_registry import ModelDeployment

log = logging.getLogger("app.ai.registry")


class ResolutionError(Exception):
    """No version may serve this request. Always says why."""


@dataclass(frozen=True)
class Request:
    """What is being asked for. Section 29."""

    model_key: str
    strategy_key: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    environment: str = "paper"

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "environment": self.environment,
        }

    def describe(self) -> str:
        parts = [self.model_key, f"environment={self.environment}"]
        for name in ("strategy_key", "symbol", "timeframe"):
            value = getattr(self, name)
            if value:
                parts.append(f"{name}={value}")
        return ", ".join(parts)


@dataclass(frozen=True)
class Resolution:
    """One answer: a version, the deployment that put it there, and the checks."""

    version_id: str
    model_key: str
    model_version: str
    deployment_id: str
    environment: str
    status: str
    feature_version: str | None
    artifact_sha256: str | None
    checks: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version_id": self.version_id,
            "model": self.model_key,
            "version": self.model_version,
            "deployment_id": self.deployment_id,
            "environment": self.environment,
            "status": self.status,
            "feature_version": self.feature_version,
            "artifact_sha256": self.artifact_sha256,
            "checks": self.checks,
            "note": (
                "resolved from an active deployment, never from 'latest'. A model that "
                "changed because someone registered a new version, without anyone "
                "deploying it, is a strategy nobody can reproduce."
            ),
        }


def _specificity(row: ModelDeployment) -> tuple[int, int, int]:
    """How narrowly a deployment is scoped. Higher wins.

    Strategy first, then symbol, then timeframe — the order a person would read
    them in, and the order that makes "this strategy's model" beat "this
    symbol's model" when both exist.
    """
    return (
        1 if row.strategy_key else 0,
        1 if row.symbol else 0,
        1 if row.timeframe else 0,
    )


def _covers(row: ModelDeployment, request: Request) -> bool:
    """Whether a deployment's scope contains the request.

    A NULL axis on the deployment matches anything on that axis; a set axis must
    match exactly. A deployment narrower than the request does NOT cover it: a
    model deployed for EURUSD is not an answer to a question that named GBPUSD,
    and it is not an answer to a question that named no symbol either — the
    caller would get a EURUSD model for an unspecified instrument.
    """
    for attribute, wanted in (
        ("strategy_key", request.strategy_key),
        ("symbol", request.symbol),
        ("timeframe", request.timeframe),
    ):
        scoped = getattr(row, attribute)
        if scoped is not None and scoped != wanted:
            return False
    return True


async def resolve(db: AsyncSession, request: Request) -> Resolution:
    """The version this request resolves to, or a refusal naming the reason."""
    model = await db.scalar(select(AIModel).where(AIModel.key == request.model_key))
    if model is None:
        raise ResolutionError(f"no model {request.model_key!r} is registered on this platform.")

    candidates = list(
        (
            await db.scalars(
                select(ModelDeployment).where(
                    ModelDeployment.model_key == request.model_key,
                    ModelDeployment.environment == request.environment,
                    ModelDeployment.status == "active",
                )
            )
        ).all()
    )
    covering = [row for row in candidates if _covers(row, request)]
    if not covering:
        raise ResolutionError(
            f"no active deployment covers {request.describe()}. "
            + (
                f"{len(candidates)} deployment(s) exist for this model in "
                f"{request.environment} and none of their scopes contains this request."
                if candidates
                else "This model has no active deployment in this environment."
            )
        )
    covering.sort(key=_specificity, reverse=True)
    deployment = covering[0]

    version = await db.get(ModelVersion, deployment.model_version_id)
    if version is None:  # pragma: no cover - the FK is RESTRICT
        raise ResolutionError(f"deployment {deployment.id} names a version that no longer exists")

    checks: dict[str, str] = {}

    if not serves_inference(version.status):
        raise ResolutionError(
            f"{request.model_key} v{deployment.model_version_label} is {version.status}, "
            "which does not serve inference. A deployment row and a servable status are "
            "two different facts and both must hold."
        )
    checks["status"] = f"{version.status} serves inference"

    if not version.validation_run_id:
        raise ResolutionError(
            f"{request.model_key} v{deployment.model_version_label} does not name the "
            "validation run that gated it. §9: validation is never bypassed, and a "
            "version that cannot name its run has not demonstrably passed one."
        )
    checks["validation"] = f"gated by validation run {version.validation_run_id[:12]}"

    integrity = artifacts.verify(version)
    if not integrity.intact:
        raise ResolutionError(
            f"the artifact for {request.model_key} v{deployment.model_version_label} "
            f"cannot be trusted: {integrity.reason} §7: a model whose integrity fails is "
            "not loaded for trading."
        )
    checks["artifact"] = integrity.reason

    if version.feature_version != feature_engine.FEATURE_SET_VERSION:
        raise ResolutionError(
            f"{request.model_key} v{deployment.model_version_label} was fitted against "
            f"feature set {version.feature_version} and this deployment computes "
            f"{feature_engine.FEATURE_SET_VERSION}."
        )
    checks["features"] = f"feature set {version.feature_version} matches"

    if version.symbol_scope and request.symbol and version.symbol_scope != request.symbol:
        raise ResolutionError(
            f"{request.model_key} v{deployment.model_version_label} was fitted for "
            f"{version.symbol_scope} and the request names {request.symbol}."
        )
    if (
        version.timeframe_scope
        and request.timeframe
        and version.timeframe_scope != request.timeframe
    ):
        raise ResolutionError(
            f"{request.model_key} v{deployment.model_version_label} was fitted for "
            f"{version.timeframe_scope} and the request names {request.timeframe}."
        )
    checks["scope"] = f"deployment scope {_describe_scope(deployment)} covers the request"

    return Resolution(
        version_id=version.id,
        model_key=request.model_key,
        model_version=deployment.model_version_label or "",
        deployment_id=deployment.id,
        environment=deployment.environment,
        status=version.status,
        feature_version=version.feature_version,
        artifact_sha256=version.artifact_sha256,
        checks=checks,
    )


def _describe_scope(row: ModelDeployment) -> str:
    parts = [f"environment={row.environment}"]
    for name in ("strategy_key", "symbol", "timeframe"):
        value = getattr(row, name)
        if value:
            parts.append(f"{name}={value}")
    return "(" + ", ".join(parts) + ")"


# =================================================================== caching


@dataclass
class _Entry:
    model: BaseModel
    version_id: str
    artifact_sha256: str
    loaded_at: datetime


class ModelCache:
    """Loaded models, keyed by version id. Section 30.

    **Keyed by version, never by scope.** A scope's answer changes on every
    promotion and rollback; a version's artifact does not, because §6 makes a
    changed model a new version. So the cache never has to be invalidated on a
    lifecycle change — the resolution runs first and simply asks for a different
    key.

    That is the whole design, and it is why `invalidate()` exists for the one
    case it cannot cover: an artifact edited in place, which §6 says must never
    happen and which the digest check catches anyway.

    **The digest is re-checked on every hit.** Correctness over speed (§45): a
    hash of a few hundred bytes is cheaper than serving a trading decision from
    an artifact that changed under the process.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, version: ModelVersion) -> BaseModel:
        """The loaded model for this version, from cache or freshly loaded."""
        entry = self._entries.get(version.id)
        if entry is not None:
            if version.artifact_sha256 and entry.artifact_sha256 == version.artifact_sha256:
                self.hits += 1
                return entry.model
            # The row's digest no longer matches what was cached. §30: never
            # continue from a stale cache. Dropped and reloaded rather than
            # trusted, and counted so the condition is visible.
            self.evictions += 1
            self._entries.pop(version.id, None)

        check = artifacts.verify(version)
        if not check.intact:
            raise ResolutionError(f"the artifact cannot be trusted: {check.reason}")
        try:
            model = model_from_version(version)
        except ArtifactError as exc:
            raise ResolutionError(f"the artifact does not load: {exc}") from exc

        self.misses += 1
        self._entries[version.id] = _Entry(
            model=model,
            version_id=version.id,
            artifact_sha256=version.artifact_sha256 or "",
            loaded_at=datetime.now(UTC),
        )
        return model

    def invalidate(self, version_id: str) -> bool:
        removed = self._entries.pop(version_id, None) is not None
        if removed:
            self.evictions += 1
        return removed

    def clear(self) -> None:
        self.evictions += len(self._entries)
        self._entries.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "keyed_by": (
                "model version id, never scope. A scope's answer changes on every "
                "promotion; a version's artifact does not, because a changed model is a "
                "new version. So a lifecycle change needs no invalidation -- the "
                "resolution asks for a different key."
            ),
            "revalidation": (
                "the artifact digest is re-checked on every hit. Correctness over "
                "caching speed: hashing a few hundred bytes is cheaper than serving a "
                "trading decision from an artifact that changed under the process."
            ),
        }


async def resolve_and_load(
    db: AsyncSession, request: Request, cache: ModelCache
) -> tuple[BaseModel, Resolution]:
    """The whole path §28 asks for: resolve, check, then load.

    The single entry point the AI integration layer uses, so there is no way to
    obtain a model that skipped a check.
    """
    resolution = await resolve(db, request)
    version = await db.get(ModelVersion, resolution.version_id)
    assert version is not None  # `resolve` just read it
    return cache.get(version), resolution
