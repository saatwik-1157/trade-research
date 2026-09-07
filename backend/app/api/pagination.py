"""Pagination, sorting and filtering primitives shared by every collection route.

Three rules, each of which a test enforces:

1. **A collection endpoint never returns an unbounded set.** `limit` has a
   default and a hard ceiling, and a caller asking for more than the ceiling
   is refused rather than quietly given the ceiling — a silently truncated
   page reads as "that is all there is", which for a trade history is a
   false statement about the record.
2. **Sort and filter fields are looked up in an allow-list**, never
   interpolated. The value a caller sends is a key into a dict of ORM
   columns; an unknown key is a 422 naming what is sortable. There is no code
   path from a query string to SQL text.
3. **The page envelope is the same everywhere**, so a component that can read
   one collection can read all of them.

Offset paging, not keyset: the collections here are bounded by a broker
account's history (hundreds of rows, not millions), and offset paging gives a
`total` the UI needs for "269 orders". When a collection outgrows that, the
envelope has room for a cursor without changing the item shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailed

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

T = TypeVar("T")


class PageInfo(BaseModel):
    """What the caller needs to ask for the next page, and to know if there is one."""

    limit: int
    offset: int
    total: int
    returned: int
    has_more: bool


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: PageInfo


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int

    def info(self, total: int, returned: int) -> PageInfo:
        return PageInfo(
            limit=self.limit,
            offset=self.offset,
            total=total,
            returned=returned,
            has_more=self.offset + returned < total,
        )


def page_params(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Rows per page."),
    offset: int = Query(0, ge=0, description="Rows to skip."),
) -> PageParams:
    """FastAPI dependency. The ceiling is enforced by the validator, so a
    caller asking for 10_000 gets a 422 that says the maximum, not a page of
    200 that looks like the whole set."""
    return PageParams(limit=limit, offset=offset)


@dataclass(frozen=True)
class SortSpec:
    """The columns a collection may be sorted by, and its default."""

    columns: dict[str, Any]
    default: str
    default_descending: bool = True

    def apply(self, stmt: Select, sort: str | None, order: str | None) -> Select:
        field = sort or self.default
        if field not in self.columns:
            raise ValidationFailed(
                f"cannot sort by {field!r}; sortable fields are {', '.join(sorted(self.columns))}"
            )
        if order is None:
            descending = self.default_descending if sort is None else False
        elif order in ("asc", "desc"):
            descending = order == "desc"
        else:
            raise ValidationFailed(f"order must be 'asc' or 'desc', not {order!r}")
        column = self.columns[field]
        return stmt.order_by(column.desc() if descending else column.asc())


async def paginate(
    db: AsyncSession, stmt: Select, params: PageParams
) -> tuple[list[Any], PageInfo]:
    """Run the statement once for the total and once for the page.

    The count runs over the same filtered statement with its ordering
    stripped, so `total` always describes the set the caller actually asked
    for rather than the table.
    """
    counted = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await db.scalar(counted)) or 0)
    rows = list((await db.scalars(stmt.limit(params.limit).offset(params.offset))).all())
    return rows, params.info(total, len(rows))
