"""The strategy builder API: create, validate, version, duplicate, lifecycle.

Every strategy here is owned. A user sees and edits their own and nobody
else's, and the scoping is done **in the query** (`WHERE owner_user_id = :me`)
rather than by filtering a full list afterwards, because a filter has bugs and
a WHERE clause does not.

**A definition is never executed.** It is stored as JSON in
`strategy_versions.config`, and `code_ref` records the fixed interpreter that
reads it — `app.strategies.built:BuiltStrategy` — the same string for every
built strategy, because there is one evaluator and it is not chosen by the
user.

**Versions are immutable once they leave draft.** Editing a validated version
creates a new one rather than overwriting it, so a signal recorded against
version 2 can always be explained by reading version 2. That is the whole point
of versioning and it is easy to lose by making PUT convenient.

**A validated strategy is not an approved one.** `validated` means the
definition parses, its indicators exist and its operands are comparable. It
does not mean the strategy works — nothing has measured it — and the lifecycle
refuses to move past `validated` because the levels that would justify it
(backtesting at L14, paper at L16, risk at L17) are not built.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_db
from app.auth.models import User
from app.core import audit
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.strategies import Strategy as StrategyRow
from app.models.strategies import StrategyVersion
from app.strategies.built import BuiltStrategy
from app.strategies.definition import (
    Comparison,
    DefinitionError,
    Logical,
    OperandKind,
    parse_definition,
    validate_against_platform,
)
from app.strategies.indicators import PRICE_FIELDS, catalogue
from app.strategies.lifecycle import IllegalVersionTransition, check_transition

router = APIRouter(prefix="/strategy-builder", tags=["strategy-builder"])

# The one interpreter, recorded on every built version. Not chosen by the user,
# and not a path anything imports from a payload.
INTERPRETER = "app.strategies.built:BuiltStrategy"

# The states the database allows. `draft` and `validated` are reachable now;
# `retired` is the archive. Anything beyond that needs machinery that does not
# exist, and the transition route says which level builds it.
REACHABLE = ("draft", "validated", "retired")
BLOCKED_UNTIL = {
    "backtested": 14,
    "paper": 16,
    "active": 17,
    "paused": 22,
}


class DefinitionIn(BaseModel):
    """The raw definition. Validated by `parse_definition`, not by pydantic.

    Deliberately untyped here: the definition is a nested tree and the real
    validation names the exact path of a problem (`entry_rules[0].when.left`),
    which a pydantic error cannot.
    """

    definition: dict[str, Any]


class CreateIn(DefinitionIn):
    key: Annotated[str, Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")]


class StatusIn(BaseModel):
    status: str


async def _owned(db: AsyncSession, user: User, key: str) -> StrategyRow:
    """The user's strategy, or 404. Scoped in the query, never after it."""
    row = await db.scalar(
        select(StrategyRow).where(StrategyRow.key == key, StrategyRow.owner_user_id == user.id)
    )
    if row is None:
        # The same answer whether it does not exist or belongs to someone else:
        # telling a caller that a key is taken by another user is a membership
        # oracle.
        raise NotFound(f"no strategy {key!r} owned by this user")
    return row


async def _latest(db: AsyncSession, strategy_id: str) -> StrategyVersion | None:
    return await db.scalar(
        select(StrategyVersion)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version.desc())
    )


def _stored(config: dict[str, Any] | None) -> dict[str, Any]:
    """Unwrap a stored config. A cloned version wraps the definition beside its
    provenance; an ordinary one is the definition itself."""
    data = config or {}
    inner = data.get("definition")
    return inner if isinstance(inner, dict) else data


def _parse(payload: dict[str, Any]) -> Any:
    try:
        return parse_definition(payload)
    except DefinitionError as exc:
        # The message names the exact field. It never carries a stack trace.
        raise ValidationFailed(str(exc)) from exc


@router.get(
    "/catalogue",
    summary="What the builder may use: indicators, operators, actions",
    description=(
        "Four indicators, because four is what the numpy rule pipeline "
        "actually has. `unit` is what makes an incompatible comparison "
        "refusable: a price level and a 0-100 oscillator are different "
        "quantities and `PRICE > RSI` is rejected."
    ),
)
async def builder_catalogue(_: User = Depends(current_user)) -> dict[str, object]:
    return {
        "indicators": catalogue(),
        "price_fields": [{"key": k, "unit": str(v)} for k, v in PRICE_FIELDS.items()],
        "comparisons": [str(c) for c in Comparison],
        "logical": [str(g) for g in Logical],
        "operand_kinds": [str(k) for k in OperandKind],
        "entry_actions": ["ENTRY_LONG", "ENTRY_SHORT"],
        "exit_actions": ["EXIT_LONG", "EXIT_SHORT", "CLOSE"],
        "note": (
            "A definition is data interpreted by a fixed evaluator. Nothing here "
            "becomes code, and a built strategy produces a signal, never an order."
        ),
    }


@router.post(
    "/validate",
    summary="Check a definition without saving it",
    description=(
        "Structural and semantic validation, plus a readable summary. Use it "
        "for live feedback in the editor. Passing means the definition is "
        "well formed -- not that the strategy works, which nothing has measured."
    ),
)
async def validate(
    body: DefinitionIn,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict[str, object]:
    definition = _parse(body.definition)
    problems = await validate_against_platform(db, definition)
    return {
        "valid": not problems,
        "problems": problems,
        "summary": definition.summary(),
        "warmup_bars": definition.warmup(),
        "indicators": [o.label() for o in definition.indicators()],
        "note": (
            "Valid means well formed. It does not mean profitable: no backtest has "
            "been run and no validation report is attached."
        ),
    }


@router.post(
    "",
    status_code=201,
    summary="Create a strategy and its first draft version",
)
async def create(
    body: CreateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    definition = _parse(body.definition)
    problems = await validate_against_platform(db, definition)

    existing = await db.scalar(select(StrategyRow).where(StrategyRow.key == body.key))
    if existing is not None:
        raise Conflict(f"strategy key {body.key!r} is already taken")

    strategy = StrategyRow(
        key=body.key,
        name=definition.name,
        description=definition.description or None,
        owner_user_id=user.id,
        # research_only always. A strategy that has never been backtested has
        # no evidence behind it, and the tier is where that is recorded.
        tier="research_only",
        is_active=True,
    )
    db.add(strategy)
    await db.flush()

    version = StrategyVersion(
        strategy_id=strategy.id,
        version=1,
        code_ref=INTERPRETER,
        config=definition.to_payload(),
        status="draft" if problems else "validated",
    )
    db.add(version)
    await audit.record(
        db,
        "strategy_created",
        "strategy",
        actor_user_id=user.id,
        resource_id=strategy.id,
        request_id=request.headers.get("X-Request-ID"),
        details={"key": body.key, "version": 1, "problems": problems},
    )
    await db.commit()
    return {
        "key": strategy.key,
        "version": 1,
        "status": version.status,
        "problems": problems,
        "summary": definition.summary(),
    }


@router.get("", summary="Strategies owned by the signed-in user")
async def list_mine(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    rows = (
        await db.scalars(
            select(StrategyRow)
            .where(StrategyRow.owner_user_id == user.id)
            .order_by(StrategyRow.created_at.desc())
            .limit(limit)
        )
    ).all()
    out: list[dict[str, object]] = []
    for row in rows:
        latest = await _latest(db, row.id)
        count = await db.scalar(
            select(func.count())
            .select_from(StrategyVersion)
            .where(StrategyVersion.strategy_id == row.id)
        )
        out.append(
            {
                "key": row.key,
                "name": row.name,
                "tier": row.tier,
                "is_active": row.is_active,
                "latest_version": latest.version if latest else None,
                "latest_status": latest.status if latest else None,
                "versions": int(count or 0),
                "created_at": row.created_at.isoformat(),
            }
        )
    return {"strategies": out, "count": len(out)}


@router.get("/{key}", summary="One owned strategy, with all its versions")
async def get_one(
    key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    strategy = await _owned(db, user, key)
    versions = (
        await db.scalars(
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == strategy.id)
            .order_by(StrategyVersion.version.asc())
        )
    ).all()
    return {
        "key": strategy.key,
        "name": strategy.name,
        "description": strategy.description,
        "tier": strategy.tier,
        "is_active": strategy.is_active,
        "versions": [
            {
                "version": v.version,
                "status": v.status,
                "code_ref": v.code_ref,
                "definition": v.config,
                "created_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
    }


@router.post(
    "/{key}/versions",
    status_code=201,
    summary="Save a change as a NEW version",
    description=(
        "A validated version is never overwritten. A signal recorded against "
        "version 2 must always be explainable by reading version 2, and an "
        "in-place edit destroys that. A **draft** may be edited in place with "
        "PUT, because nothing has run against it."
    ),
)
async def new_version(
    key: str,
    body: DefinitionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    strategy = await _owned(db, user, key)
    definition = _parse(body.definition)
    problems = await validate_against_platform(db, definition)
    latest = await _latest(db, strategy.id)
    number = (latest.version + 1) if latest else 1

    version = StrategyVersion(
        strategy_id=strategy.id,
        version=number,
        code_ref=INTERPRETER,
        config=definition.to_payload(),
        status="draft" if problems else "validated",
    )
    db.add(version)
    await audit.record(
        db,
        "strategy_version_created",
        "strategy",
        actor_user_id=user.id,
        resource_id=strategy.id,
        request_id=request.headers.get("X-Request-ID"),
        details={"key": key, "version": number},
    )
    await db.commit()
    return {"key": key, "version": number, "status": version.status, "problems": problems}


@router.put(
    "/{key}/versions/{number}",
    summary="Edit a DRAFT version in place",
    description=(
        "Only a draft. A validated version is immutable — editing one would "
        "make a recorded signal unexplainable, so it answers 409 and points at "
        "POST /versions."
    ),
)
async def edit_draft(
    key: str,
    number: int,
    body: DefinitionIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    strategy = await _owned(db, user, key)
    version = await db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == number
        )
    )
    if version is None:
        raise NotFound(f"strategy {key!r} has no version {number}")
    if version.status != "draft":
        raise Conflict(
            f"version {number} is {version.status} and cannot be edited in place; "
            "create a new version instead so the recorded one stays readable"
        )
    definition = _parse(body.definition)
    problems = await validate_against_platform(db, definition)
    version.config = definition.to_payload()
    version.status = "draft" if problems else "validated"
    await db.commit()
    return {"key": key, "version": number, "status": version.status, "problems": problems}


@router.post(
    "/{key}/duplicate",
    status_code=201,
    summary="Clone a strategy under a new key",
    description=(
        "A new strategy id, a fresh version 1, and a reference to the source "
        "recorded in the definition. The original is not touched."
    ),
)
async def duplicate(
    key: str,
    new_key: Annotated[str, Query(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    source = await _owned(db, user, key)
    if await db.scalar(select(StrategyRow).where(StrategyRow.key == new_key)):
        raise Conflict(f"strategy key {new_key!r} is already taken")
    latest = await _latest(db, source.id)
    if latest is None:
        raise NotFound(f"strategy {key!r} has no versions to copy")

    clone = StrategyRow(
        key=new_key,
        name=f"{source.name} (copy)",
        description=source.description,
        owner_user_id=user.id,
        tier="research_only",
        is_active=True,
    )
    db.add(clone)
    await db.flush()
    # Re-parsed rather than copied blind: a stored definition must still be
    # valid to be worth cloning.
    config = _parse(_stored(latest.config)).to_payload()
    # Provenance travels with the copy: "where did this come from" is a
    # question somebody asks about a strategy that suddenly looks good. It
    # is kept beside the definition rather than inside it, so the stored
    # payload stays round-trippable.
    config = {"definition": config, "cloned_from": {"key": key, "version": latest.version}}
    db.add(
        StrategyVersion(
            strategy_id=clone.id,
            version=1,
            code_ref=INTERPRETER,
            config=config,
            status="draft",
        )
    )
    await db.commit()
    return {"key": new_key, "version": 1, "cloned_from": {"key": key, "version": latest.version}}


@router.patch(
    "/{key}/versions/{number}/status",
    summary="Move a version through its lifecycle",
    description=(
        "`draft`, `validated` and `retired` are reachable. Anything beyond "
        "them needs machinery that is not built — backtesting is L14, paper is "
        "L16, risk is L17, bot supervision is L22 — and the refusal names the "
        "level rather than letting a version claim a state nothing enforces."
    ),
)
async def set_status(
    key: str,
    number: int,
    body: StatusIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    strategy = await _owned(db, user, key)
    version = await db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == number
        )
    )
    if version is None:
        raise NotFound(f"strategy {key!r} has no version {number}")

    wanted = body.status
    if wanted in BLOCKED_UNTIL:
        raise Conflict(
            f"a version cannot be {wanted!r} yet: the machinery that would enforce it "
            f"is built at level {BLOCKED_UNTIL[wanted]:02d}. Marking it so would let a "
            "strategy claim a state nothing checks"
        )
    if wanted not in REACHABLE:
        raise ValidationFailed(f"status must be one of {', '.join(REACHABLE)}, not {wanted!r}")
    # **L57.** The TRANSITION, not just the destination.
    #
    # Everything above checks what the version is being set TO. Nothing
    # checked what it is being set FROM, so `retired -> validated` was one
    # call: a retired strategy brought back with no re-validation of its
    # dependencies, its model or its approval. Every other lifecycle in this
    # platform -- OMS, bots, risk, portfolio -- has a transition table; this
    # was the one that did not.
    #
    # Runs BEFORE the re-validation below, so an illegal move is refused for
    # what it is rather than for whatever the definition check happens to say.
    if version.status == wanted:
        # Not an error and not a transition: the caller asked for the state it
        # already has. Answered without writing an audit row for a change that
        # did not happen.
        return {"key": key, "version": number, "status": wanted, "previous": wanted}
    try:
        check_transition(version.status, wanted)
    except IllegalVersionTransition as exc:
        raise Conflict(str(exc)) from exc

    if wanted == "validated":
        # Re-validated on the way in rather than trusted: the definition could
        # have been saved before an indicator's bounds changed.
        definition = _parse(_stored(version.config))
        problems = await validate_against_platform(db, definition)
        if problems:
            raise ValidationFailed(f"cannot mark validated: {'; '.join(problems)}")

    was = version.status
    version.status = wanted
    await audit.record(
        db,
        "strategy_version_status_changed",
        "strategy",
        actor_user_id=user.id,
        resource_id=strategy.id,
        request_id=request.headers.get("X-Request-ID"),
        details={"key": key, "version": number, "from": was, "to": wanted},
    )
    await db.commit()
    return {"key": key, "version": number, "status": wanted, "previous": was}


@router.get(
    "/{key}/versions/{number}/preview",
    summary="A readable account of what this version does",
    description=(
        "The strategy in words, so it can be understood without reading JSON. "
        "Also reports the warm-up and whether a backtest is available."
    ),
)
async def preview(
    key: str,
    number: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    strategy = await _owned(db, user, key)
    version = await db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == number
        )
    )
    if version is None:
        raise NotFound(f"strategy {key!r} has no version {number}")
    definition = _parse(_stored(version.config))
    built = BuiltStrategy(definition, key=key, version=number)
    return {
        "key": key,
        "version": number,
        "status": version.status,
        "summary": definition.summary(),
        "warmup_bars": definition.warmup(),
        "metadata": built.metadata().as_dict(),
        "definition": definition.as_dict(),
        # Never pretends a backtest ran.
        "backtest": {
            "available": False,
            "state": "NOT RUN",
            "reason": "the backtest runner is built at level 14",
        },
    }
