"""Symbols, provider mappings and contract specs.

This is a read of the L11 layer, which is complete: three-name resolution
(source -> internal -> broker), per-broker contract specs, and refusals in
place of guesses. The routes add no logic of their own; they call
`app.symbols.service` and let its errors become the response.

The refusals are the interesting part of the surface:

  * an unmapped provider symbol is a 404, never a fallback to the input string;
  * a ticker matching two instruments is a 409, never a pick;
  * a spec missing any required field is a 409 naming the missing fields,
    never a spec with defaults filled in.

`app.symbols.errors.SymbolError` already carries a `status_code`, and
`app.core.errors` maps it, so none of that is restated here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, SortSpec, page_params, paginate
from app.api.v1.schemas import ContractSpecOut, MappingOut, SymbolOut
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import NotFound, ValidationFailed
from app.models.market import Symbol, SymbolMapping
from app.symbols import service as symbols

router = APIRouter(prefix="/symbols", tags=["symbols"])

_READ = Depends(require_permission(Permission.view_markets))

SYMBOL_SORTS = SortSpec(
    columns={"code": Symbol.code, "asset_class": Symbol.asset_class},
    default="code",
    default_descending=False,
)


@router.get(
    "",
    response_model=Page[SymbolOut],
    summary="Internal symbols",
    description=(
        "The platform's canonical instrument codes. `unit_class` says whether "
        "this symbol's moves are measured in points or percent; pooling points "
        "across symbols whose point sizes differ by 59x is a real error this "
        "project has made and the field exists to prevent it."
    ),
)
async def list_symbols(
    params: PageParams = Depends(page_params),
    asset_class: str | None = Query(None, description=" | ".join(symbols.ASSET_CLASSES)),
    is_active: bool | None = Query(None),
    sort: str | None = Query(None, description="code | asset_class"),
    order: str | None = Query(None, description="asc | desc"),
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> Page[SymbolOut]:
    if asset_class is not None and asset_class not in symbols.ASSET_CLASSES:
        raise ValidationFailed(f"asset_class must be one of {', '.join(symbols.ASSET_CLASSES)}")
    stmt = select(Symbol)
    if asset_class:
        stmt = stmt.where(Symbol.asset_class == asset_class)
    if is_active is not None:
        stmt = stmt.where(Symbol.is_active == is_active)
    stmt = SYMBOL_SORTS.apply(stmt, sort, order)
    rows, page = await paginate(db, stmt, params)
    items = [SymbolOut.model_validate(r, from_attributes=True) for r in rows]
    return Page[SymbolOut](items=items, page=page)


@router.get("/{code}", response_model=SymbolOut, summary="One internal symbol")
async def get_symbol(
    code: str,
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> SymbolOut:
    row = await symbols.get_symbol(db, code)
    return SymbolOut.model_validate(row, from_attributes=True)


@router.get(
    "/{code}/mappings",
    response_model=list[MappingOut],
    summary="What each provider calls this symbol",
    description=(
        "`has_complete_spec` says whether this mapping carries every field "
        "sizing needs. False does not mean the values are wrong -- it means "
        "they are absent, and a caller that needs them will be refused rather "
        "than given defaults."
    ),
)
async def list_mappings(
    code: str,
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> list[MappingOut]:
    symbol = await symbols.get_symbol(db, code)
    stmt = (
        select(SymbolMapping)
        .where(SymbolMapping.symbol_id == symbol.id)
        .order_by(SymbolMapping.provider.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [
        MappingOut.model_validate(r, from_attributes=True).model_copy(
            update={
                "has_complete_spec": all(
                    getattr(r, f) is not None for f in symbols.REQUIRED_SPEC_FIELDS
                )
            }
        )
        for r in rows
    ]


@router.get(
    "/{code}/spec",
    response_model=ContractSpecOut,
    summary="A broker's contract terms for this symbol",
    description=(
        "Refuses with 409 naming the missing fields when the spec is "
        "incomplete. Contract terms belong to the broker, not the instrument: "
        "DE40 has contract size 1 and minimum volume 0.1 where the FX pairs "
        "have 100,000 and 0.01, both measured from the terminal."
    ),
)
async def get_spec(
    code: str,
    provider: str = Query(symbols.BROKER_PROVIDER, description=" | ".join(symbols.PROVIDERS)),
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> ContractSpecOut:
    spec = await symbols.contract_spec(db, code, provider)
    return ContractSpecOut(**vars(spec))


@router.get(
    "/resolve/{provider}/{provider_symbol:path}",
    response_model=SymbolOut,
    summary="A provider's symbol to the internal symbol",
    description=(
        "Never falls back to the input string. An unmapped symbol is a 404 and "
        "a ticker matching two instruments is a 409 naming both -- guessing "
        "between them would route an order to the wrong instrument."
    ),
)
async def resolve(
    provider: str,
    provider_symbol: str,
    db: AsyncSession = Depends(get_db),
    _: User = _READ,
) -> SymbolOut:
    if not provider_symbol.strip():
        raise NotFound("no provider symbol given")
    row = await symbols.resolve_source(db, provider, provider_symbol)
    return SymbolOut.model_validate(row, from_attributes=True)
