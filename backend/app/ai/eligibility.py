"""May this model version be used to influence a trading decision?

Section 12 asks that AI integration use **validated** models and that arbitrary
model selection be impossible. Section 12 also says that when L28's registry is
not yet built, the integration boundary should be built so it can consume the
registry when it arrives — and that no competing registry be created here.

So this module is a **boundary and not a registry**. It holds no state, stores
nothing, and answers one question by reading rows two other levels already own:

  * `model_versions.status` — L24 writes `draft`; L28 will write the rest.
  * `validation_runs` — L26's verdict for that version.

**Today the answer comes from L26.** A version is eligible when its most recent
completed validation run returned PASS or CONDITIONAL. When L28 lands and starts
writing `model_versions.status`, `_status_allows()` becomes the primary check
and this module is where that switch happens — one function, not a search.

**A version with no validation is NOT eligible.** Not because it is bad, but
because nothing has been established about it. That is the same distinction L26
draws between FAIL and BLOCKED, applied one level down: an unvalidated model is
refused, and the reason says so.

**Nothing here promotes.** No column is written by this module at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIModel, ModelVersion
from app.models.validation import ValidationRun

# Verdicts that make a candidate eligible for CONSIDERATION. Both mean "the
# validation requirements that were checked are satisfied"; CONDITIONAL adds
# caveats about how the output may be READ, which is a constraint on
# interpretation rather than on use.
#
# BLOCKED is absent deliberately. "We could not tell" is not "yes".
USABLE_VERDICTS = frozenset({"PASS", "CONDITIONAL"})

# Statuses that permit inference once L28 is writing them. `promoted` is the
# only one today that would mean "in production"; `validated` is included
# because a validated-but-not-promoted version is exactly what a paper run
# should be allowed to consult.
#
# This tuple is the L28 boundary. When the registry starts writing these,
# `_status_allows` stops being permissive and this list becomes the gate.
USABLE_STATUSES = frozenset({"validated", "promoted"})


@dataclass(frozen=True)
class Eligibility:
    """Whether a version may be consulted, and the evidence either way."""

    eligible: bool
    reason: str
    model_version_id: str | None = None
    model_key: str | None = None
    model_version: str | None = None
    status: str | None = None
    verdict: str | None = None
    validation_run_id: str | None = None
    validated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "model_version_id": self.model_version_id,
            "model_key": self.model_key,
            "model_version": self.model_version,
            "status": self.status,
            "verdict": self.verdict,
            "validation_run_id": self.validation_run_id,
            "validated_at": self.validated_at,
            "source": (
                "L26's validation verdict. When L28's registry begins writing "
                "model_versions.status, that column becomes the gate and this function "
                "is the one place that changes."
            ),
        }


def _refused(reason: str, **fields: Any) -> Eligibility:
    return Eligibility(eligible=False, reason=reason, **fields)


async def check(db: AsyncSession, *, key: str, version: str) -> Eligibility:
    """Whether `key` v`version` may influence a trading decision.

    Reads only. The version is named exactly — there is no "latest" here, for
    the reason §12 gives: a strategy whose behaviour changes when somebody
    registers a new version is a strategy nobody can reproduce.
    """
    model = await db.scalar(select(AIModel).where(AIModel.key == key))
    if model is None:
        return _refused(
            f"no model {key!r} is recorded on this platform. A strategy cannot depend on "
            "a model nobody registered.",
            model_key=key,
            model_version=version,
        )

    # `artifact_ref` is `"{key}:{version}"`, written by `register_version`. The
    # integer `version` column orders rows; the string identifies the model.
    row = await db.scalar(
        select(ModelVersion).where(
            ModelVersion.model_id == model.id,
            ModelVersion.artifact_ref == f"{key}:{version}",
        )
    )
    if row is None:
        return _refused(
            f"model {key} has no version {version!r}. Refused rather than resolved to "
            "the nearest one: a version that is guessed is a model nobody chose.",
            model_key=key,
            model_version=version,
        )

    common: dict[str, Any] = {
        "model_version_id": row.id,
        "model_key": key,
        "model_version": version,
        "status": row.status,
    }

    if row.status == "retired":
        return _refused(
            f"model {key} v{version} is retired. A retired version is refused rather "
            "than quietly consulted; retiring it was a decision somebody made.",
            **common,
        )
    if row.status == "rejected":
        return _refused(f"model {key} v{version} was rejected", **common)

    if row.status in USABLE_STATUSES:
        # L28 is writing statuses. Its answer is authoritative and this function
        # stops consulting L26 directly -- the registry's job is to have done so.
        return Eligibility(
            eligible=True,
            reason=(
                f"the model registry records {key} v{version} as {row.status}, which "
                "permits inference. The risk engine remains authoritative."
            ),
            **common,
        )

    latest = await db.scalar(
        select(ValidationRun)
        .where(ValidationRun.model_version_id == row.id, ValidationRun.status == "completed")
        .order_by(ValidationRun.created_at.desc())
        .limit(1)
    )
    if latest is None:
        return _refused(
            f"model {key} v{version} has never been validated. Not a judgement about the "
            "model: nothing has been established about it, and an unvalidated model "
            "influencing a trade is exactly what §12 exists to prevent.",
            **common,
        )

    verdict_fields = {
        **common,
        "verdict": latest.verdict,
        "validation_run_id": latest.id,
        "validated_at": latest.finished_at.isoformat() if latest.finished_at else None,
    }
    if latest.verdict not in USABLE_VERDICTS:
        return _refused(
            f"the most recent validation of {key} v{version} returned {latest.verdict}: "
            f"{latest.summary or 'no summary'}. "
            + (
                "BLOCKED means a check could not be evaluated, so nothing was established "
                "about this model either way."
                if latest.verdict == "BLOCKED"
                else "A failing candidate is not eligible for consideration."
            ),
            **verdict_fields,
        )

    return Eligibility(
        eligible=True,
        reason=(
            f"validation run {latest.id[:12]} returned {latest.verdict}. That makes the "
            "candidate eligible for CONSIDERATION -- it is not a promotion, and the risk "
            "engine remains authoritative over every signal it touches."
        ),
        **verdict_fields,
    )


async def eligible_versions(db: AsyncSession, *, limit: int = 100) -> list[dict[str, Any]]:
    """Every version a strategy could legitimately name. Reads only.

    Used by the API so an operator configuring a strategy is offered the models
    that have actually been validated, rather than typing a version string and
    finding out at signal time.
    """
    models = {row.id: row for row in (await db.scalars(select(AIModel))).all()}
    versions = list(
        (
            await db.scalars(
                select(ModelVersion).order_by(ModelVersion.created_at.desc()).limit(limit)
            )
        ).all()
    )

    out: list[dict[str, Any]] = []
    for version in versions:
        model = models.get(version.model_id)
        if model is None:  # pragma: no cover - a FK guarantees this
            continue
        label = (version.artifact_ref or "").split(":", 1)
        if len(label) != 2:
            continue
        verdict = await check(db, key=model.key, version=label[1])
        out.append(verdict.as_dict())
    return out
