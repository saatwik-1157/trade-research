"""Strategy routes. Read and evaluate; never execute.

The evaluate route is the one worth explaining. It runs a strategy over real
bars and returns what it said -- which is a **signal**, not an order. There is
no path from this route to a venue: the engine imports no broker adapter, the
response carries no order id or fill, and `POST /v1/orders` is still 501.

Running a strategy is gated on `create_strategies` (TRADER and above) rather
than on a read permission, because an evaluation fetches market data and
publishes a `SIGNAL_CREATED` event. It is cheap, but it is not nothing.

Every strategy here is `research_only`, and each one's `evidence` field says
what was measured about it. That is not decoration: a list of strategies with
no evidence attached invites someone to pick the one with the best-sounding
name.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import DependencyUnavailable, NotFound, ValidationFailed
from app.marketdata.base import ProviderUnavailable
from app.marketdata.types import Provider, TimeframeError, parse_timeframe
from app.strategies.base import StrategyTier
from app.strategies.engine import StrategyEngine
from app.strategies.registry import UnknownStrategy

router = APIRouter(prefix="/strategies", tags=["strategies"])

_READ = Depends(require_permission(Permission.view_markets))
_RUN = Depends(require_permission(Permission.create_strategies))


def _engine(request: Request) -> StrategyEngine:
    engine: StrategyEngine = request.app.state.strategy_engine
    return engine


@router.get(
    "",
    summary="Every registered strategy, with what has been measured about it",
    description=(
        "`tier` is `research_only` for all of them, and that is a measurement "
        "rather than caution: none separates from a coin flip at this broker's "
        "spreads. `evidence` says so per strategy, because a list with no "
        "evidence attached invites picking the best-sounding name."
    ),
)
async def list_strategies(request: Request, _: User = _READ) -> dict[str, object]:
    engine = _engine(request)
    return {
        "strategies": engine.registry.describe(),
        "tiers": [str(t) for t in StrategyTier],
        "note": (
            "A strategy produces a signal. It is sized by nothing and approved "
            "by nothing until Risk and the OMS exist."
        ),
    }


@router.get(
    "/{key}",
    summary="One strategy's contract and data requirement",
)
async def get_strategy(request: Request, key: str, _: User = _READ) -> dict[str, object]:
    engine = _engine(request)
    try:
        instance = engine.registry.implementation(key)()
    except UnknownStrategy as exc:
        raise NotFound(str(exc)) from exc
    return {
        **instance.metadata().as_dict(),
        "required_data": instance.required_data().as_dict(),
        "default_config": instance.config,
    }


@router.get(
    "/{key}/evaluate",
    summary="Run a strategy over real bars and report what it said",
    description=(
        "Returns a **signal**, never an order. The engine cannot reach a "
        "venue: it imports no broker adapter and the response carries no order "
        "id, no fill and no ticket. An actionable signal also publishes "
        "SIGNAL_CREATED, which nothing consumes yet."
        "\n\n"
        "The forming bar is dropped before the strategy sees the data, so a "
        "signal at bar T used only information available at or before T."
    ),
)
async def evaluate(
    request: Request,
    key: str,
    symbol: str = Query(..., max_length=32, description="Internal symbol code."),
    timeframe: str = Query("H1"),
    provider: str = Query("mt5", description=" | ".join(str(p) for p in Provider)),
    limit: int = Query(300, ge=1, le=5000, description="Bars to fetch for the warm-up."),
    db: AsyncSession = Depends(get_db),
    _: User = _RUN,
) -> dict[str, object]:
    engine = _engine(request)
    try:
        frame = parse_timeframe(timeframe)
    except TimeframeError as exc:
        raise ValidationFailed(str(exc)) from exc
    try:
        chosen = Provider(provider)
    except ValueError as exc:
        raise ValidationFailed(f"unknown provider {provider!r}") from exc

    # Resolved BEFORE any market data is fetched. An unknown key is a caller
    # error -- the resource does not exist -- and answering 404 keeps it
    # distinct from a strategy that exists and failed, which is an `error`
    # outcome with the reason attached. The engine catches its own errors by
    # design, so without this check a typo'd key would come back 200.
    try:
        engine.registry.implementation(key)
    except UnknownStrategy as exc:
        raise NotFound(str(exc)) from exc

    market = request.app.state.market_data
    try:
        series = await market.get_bars(db, symbol, frame, chosen, limit=limit)
    except ProviderUnavailable as exc:
        raise DependencyUnavailable(str(exc)) from exc

    evaluation = await engine.evaluate(
        key,
        symbol,
        frame,
        series.series.bars,
        correlation_id=request.headers.get("X-Request-ID"),
    )
    return {
        **evaluation.as_dict(),
        "provider_used": str(series.provider_used),
        "series_stale": series.stale,
        "data_quality": series.series.quality.as_dict(),
    }


@router.get(
    "/engine/counters",
    summary="Strategy activity, counted",
    description=(
        "Signals generated, held, refused for want of data, refused for "
        "staleness, and errors. Enough to answer 'how active was this' later; "
        "the analytics system is L32."
    ),
)
async def counters(request: Request, _: User = _READ) -> dict[str, int]:
    return _engine(request).counters.as_dict()
