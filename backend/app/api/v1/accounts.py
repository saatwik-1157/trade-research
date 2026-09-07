"""Trading accounts: broker accounts and paper accounts.

Scoped to the signed-in user, and scoped in the query rather than after it. A
route that fetched every account and filtered the list in Python would leak
through any bug in the filter; a `WHERE user_id = :me` cannot.

`BrokerAccountOut` deliberately omits `login`. An account number is a
credential-shaped identifier, nothing in the UI needs it, and the cheapest way
to guarantee it is never rendered is to leave it out of the shape. Credentials
themselves are not in this table at all -- encrypted broker credentials are
L39, and until then the platform holds none.

Creating and connecting accounts is L10: a broker account that cannot be
connected is a row that promises something the platform cannot do.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import Page, PageParams, page_params, paginate
from app.api.v1.schemas import BrokerAccountOut, PaperAccountOut
from app.auth.deps import current_user, get_db
from app.auth.models import User
from app.core.errors import NotImplementedYet
from app.models.accounts import BrokerAccount, PaperAccount

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get(
    "/broker",
    response_model=Page[BrokerAccountOut],
    summary="Broker accounts belonging to the signed-in user",
    description=(
        "`account_mode` is demo or live and is a property of the account, not "
        "a setting. The account number is not returned. Connection state comes "
        "from a connected adapter (L10), not from this row."
    ),
)
async def list_broker_accounts(
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> Page[BrokerAccountOut]:
    stmt = (
        select(BrokerAccount)
        .where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at.desc())
    )
    rows, page = await paginate(db, stmt, params)
    items = [BrokerAccountOut.model_validate(r, from_attributes=True) for r in rows]
    return Page[BrokerAccountOut](items=items, page=page)


@router.get(
    "/paper",
    response_model=Page[PaperAccountOut],
    summary="Paper accounts belonging to the signed-in user",
    description=(
        "PAPER is the internal simulator, distinct from DEMO, which is a real "
        "broker's demo account. Balances here are simulated and are never "
        "pooled with broker figures."
    ),
)
async def list_paper_accounts(
    params: PageParams = Depends(page_params),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> Page[PaperAccountOut]:
    stmt = (
        select(PaperAccount)
        .where(PaperAccount.user_id == user.id)
        .order_by(PaperAccount.created_at.desc())
    )
    rows, page = await paginate(db, stmt, params)
    items = [PaperAccountOut.model_validate(r, from_attributes=True) for r in rows]
    return Page[PaperAccountOut](items=items, page=page)


@router.post(
    "/broker",
    status_code=501,
    summary="Connect a broker account (built at L10)",
    description=(
        "Not built. A broker account row without a working adapter promises a "
        "connection the platform cannot make. L10 brings the adapter, the "
        "connection state machine and the demo fence that runs on connect."
    ),
)
async def create_broker_account(_: User = Depends(current_user)) -> None:
    raise NotImplementedYet(10, "connecting an account requires the broker adapter")
