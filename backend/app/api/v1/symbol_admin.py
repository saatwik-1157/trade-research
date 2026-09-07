"""Administrative symbol and mapping management.

Separate from the read surface in `app.api.v1.symbols` on purpose. Reading a
mapping needs `view_markets`, which every signed-in user has; **changing** one
needs `manage_system_settings`, which only an admin has. A mapping edit decides
which instrument an order reaches, so it sits behind the strictest gate the
permission table offers.

Every write goes through `app.symbols.service`'s existing `upsert_symbol` and
`upsert_mapping`. Nothing here writes a row directly, so the refusals those
functions already enforce apply to the API too:

  * a provider symbol already pointing at a different instrument is a
    **conflict**, not an update -- silently repointing it would send orders for
    one instrument to another;
  * an unknown mapping field is refused rather than ignored;
  * a code is normalized or rejected, never best-guessed.

Every change is written to the audit log with what it was before, because a
mapping edit is a change to where orders go and "who changed EURUSD to point
at GBPUSD" has to be answerable.

**Deactivation, not deletion.** There is no DELETE route. A mapping that was
used to place an order is part of the record of why that order went where it
did, and removing it would make a historical trade unexplainable. `is_active`
false takes it out of every trading path while leaving the history readable --
`resolve_source` already refuses an inactive mapping.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import MappingOut, SymbolOut
from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core import audit
from app.core.errors import request_id_of
from app.models.market import Symbol
from app.symbols import service as symbols
from app.symbols.status import SymbolReport, status_of

router = APIRouter(prefix="/admin/symbols", tags=["admin"])

# The strictest gate the table offers. A mapping edit decides which instrument
# an order reaches.
_ADMIN = Depends(require_permission(Permission.manage_system_settings))
# Reading a symbol's status is a read: any signed-in user may ask whether an
# instrument is usable. Changing a mapping is not.
_READ = Depends(require_permission(Permission.view_markets))


class SymbolIn(BaseModel):
    code: Annotated[str, Field(max_length=32)]
    asset_class: str
    base_currency: str | None = None
    quote_currency: str | None = None
    digits: int | None = Field(default=None, ge=0, le=12)
    point_size: Decimal | None = None
    unit_class: str = "points"

    @field_validator("asset_class")
    @classmethod
    def _known_asset_class(cls, v: str) -> str:
        if v not in symbols.ASSET_CLASSES:
            raise ValueError(f"asset_class must be one of {', '.join(symbols.ASSET_CLASSES)}")
        return v

    @field_validator("unit_class")
    @classmethod
    def _known_unit_class(cls, v: str) -> str:
        # Pooling points across symbols whose point sizes differ 59x is the
        # metals error this field exists to prevent, so it is checked here and
        # again in the service.
        if v not in ("points", "percent"):
            raise ValueError("unit_class must be 'points' or 'percent'")
        return v


class MappingIn(BaseModel):
    provider: str
    provider_symbol: Annotated[str, Field(max_length=64)]

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in symbols.PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(symbols.PROVIDERS)}")
        return v


class ActiveIn(BaseModel):
    is_active: bool


async def _record(
    db: AsyncSession,
    request: Request,
    actor: User,
    action: str,
    resource_id: str,
    details: dict,
) -> None:
    await audit.record(
        db,
        action,
        "symbol_mapping",
        actor_user_id=actor.id,
        resource_id=resource_id,
        ip=request.client.host if request.client else None,
        request_id=request_id_of(request),
        details=details,
    )


@router.post(
    "",
    response_model=SymbolOut,
    status_code=201,
    summary="Create or update an internal symbol",
    description=(
        "Idempotent. The code is normalized (trimmed, upper-cased) or refused; "
        "it is never best-guessed. Creating a symbol grants nothing on its own "
        "-- until a provider mapping and a synced contract spec exist, "
        "`/v1/admin/symbols/{code}/status` reports it as not_tradable."
    ),
)
async def upsert_symbol(
    body: SymbolIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(current_user),
    _: User = _ADMIN,
) -> SymbolOut:
    wanted = body.code.strip().upper()
    before = await db.scalar(select(Symbol).where(Symbol.code == wanted))
    row = await symbols.upsert_symbol(
        db,
        body.code,
        body.asset_class,
        base_currency=body.base_currency,
        quote_currency=body.quote_currency,
        unit_class=body.unit_class,
        digits=body.digits,
        point_size=body.point_size,
    )
    await _record(
        db,
        request,
        actor,
        "symbol_upserted",
        row.id,
        {"code": row.code, "existed": before is not None, "asset_class": row.asset_class},
    )
    await db.commit()
    return SymbolOut.model_validate(row, from_attributes=True)


@router.put(
    "/{code}/mappings",
    response_model=MappingOut,
    summary="Create or update one provider mapping",
    description=(
        "A provider symbol already mapped to a **different** instrument is a "
        "409 conflict, not an update. Silently repointing it would send orders "
        "for one instrument to another, which is the failure the whole mapping "
        "table exists to prevent. Contract specs are not settable here: they "
        "come from the terminal via `app.symbols.sync_mt5`, because a spec "
        "typed by hand and a spec read from the venue are not the same evidence."
    ),
)
async def upsert_mapping(
    code: str,
    body: MappingIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(current_user),
    _: User = _ADMIN,
) -> MappingOut:
    row = await symbols.upsert_mapping(db, code, body.provider, body.provider_symbol)
    await _record(
        db,
        request,
        actor,
        "symbol_mapping_upserted",
        row.id,
        {
            "internal_symbol": code.strip().upper(),
            "provider": row.provider,
            "provider_symbol": row.provider_symbol,
        },
    )
    await db.commit()
    return MappingOut.model_validate(row, from_attributes=True).model_copy(
        update={
            "has_complete_spec": all(
                getattr(row, f) is not None for f in symbols.REQUIRED_SPEC_FIELDS
            )
        }
    )


@router.patch(
    "/{code}/mappings/{provider}/active",
    response_model=MappingOut,
    summary="Enable or disable one provider mapping",
    description=(
        "There is no delete. A mapping that was used to place an order is part "
        "of the record of why that order went where it did; removing it would "
        "make a historical trade unexplainable. Disabling takes it out of every "
        "trading path -- `resolve_source` refuses an inactive mapping -- while "
        "leaving the history readable."
    ),
)
async def set_mapping_active(
    code: str,
    provider: str,
    body: ActiveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(current_user),
    _: User = _ADMIN,
) -> MappingOut:
    # allow_inactive: an admin re-enabling a disabled mapping must be able to
    # find it, and the ordinary lookup refuses one.
    row = await symbols.mapping_for(db, code, provider, allow_inactive=True)
    was = row.is_active
    row.is_active = body.is_active
    await db.flush()
    await _record(
        db,
        request,
        actor,
        "symbol_mapping_active_changed",
        row.id,
        {
            "internal_symbol": code.strip().upper(),
            "provider": provider,
            "provider_symbol": row.provider_symbol,
            "from": was,
            "to": body.is_active,
        },
    )
    await db.commit()
    return MappingOut.model_validate(row, from_attributes=True).model_copy(
        update={
            "has_complete_spec": all(
                getattr(row, f) is not None for f in symbols.REQUIRED_SPEC_FIELDS
            )
        }
    )


@router.get(
    "/{code}/status",
    summary="Whether this symbol may enter a trading flow, and why not if it may not",
    description=(
        "Never raises, and never reports a symbol it could not resolve as "
        "active. `not_tradable` means resolvable but the contract spec is "
        "incomplete, and the missing fields are named. Passing is not "
        "authorization: it says the symbol is usable, not that a trade may "
        "happen -- Risk decides that."
    ),
)
async def symbol_status(
    code: str,
    provider: str = symbols.BROKER_PROVIDER,
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> dict[str, object]:
    report: SymbolReport = await status_of(db, code, provider)
    return report.as_dict()
