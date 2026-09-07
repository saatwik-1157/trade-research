"""Market data routes.

Every response says which provider served it, how old the data is, and what is
wrong with it. That is the whole point of the surface: a chart drawn from these
bars can label itself honestly, and a panel showing a stale feed can say so
rather than showing the last price it happened to receive.

`GET /v1/market/quotes` needs a connected quote provider. On a host without a
running MT5 terminal there is none, and the route answers 503 naming the reason
rather than serving a daily close as a live price. **"LIVE" is never claimed on
the platform's behalf** -- the caller gets `provider`, `at` and `stale` and
decides what to display.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import DependencyUnavailable, ValidationFailed
from app.marketdata.base import ProviderUnavailable
from app.marketdata.service import MarketDataService
from app.marketdata.types import Provider, Timeframe, TimeframeError, parse_timeframe

router = APIRouter(prefix="/market", tags=["market"])

_MARKETS = Depends(require_permission(Permission.view_markets))
_SYSTEM = Depends(require_permission(Permission.manage_system_settings))


def _service(request: Request) -> MarketDataService:
    service: MarketDataService = request.app.state.market_data
    return service


def _provider(value: str) -> Provider:
    try:
        return Provider(value)
    except ValueError as exc:
        raise ValidationFailed(
            f"unknown provider {value!r}; expected one of {', '.join(str(p) for p in Provider)}"
        ) from exc


def _timeframe(value: str) -> Timeframe:
    try:
        return parse_timeframe(value)
    except TimeframeError as exc:
        raise ValidationFailed(str(exc)) from exc


@router.get(
    "/providers",
    summary="Every registered provider and whether it can serve right now",
    description=(
        "`usable` is what the provider reported when asked, not whether it is "
        "configured. MT5 needs a Windows host with a logged-in terminal and "
        "says so on any other host rather than failing inside a request."
    ),
)
async def providers(request: Request, _: User = _MARKETS) -> dict[str, object]:
    service = _service(request)
    return {"providers": [s.as_dict() for s in await service.statuses()]}


@router.get(
    "/quotes",
    summary="A normalized quote for one symbol",
    description=(
        "Answers with the provider, the provider's own timestamp and a `stale` "
        "flag computed from the timeframe rather than a global constant. A "
        "stale quote is reported, never acted on: deciding what to do about "
        "one belongs to the strategy and the risk engine. `spread` is derived "
        "from bid and ask and is null when either is missing -- never zero, "
        "because a zero spread is a claim that trading is free."
    ),
)
async def quote(
    request: Request,
    symbol: str = Query(..., max_length=32, description="Internal symbol code, e.g. EURUSD."),
    provider: str = Query("mt5", description=" | ".join(str(p) for p in Provider)),
    db: AsyncSession = Depends(get_db),
    _: User = _MARKETS,
) -> dict[str, object]:
    service = _service(request)
    try:
        result = await service.get_quote(db, symbol, _provider(provider))
    except ProviderUnavailable as exc:
        raise DependencyUnavailable(str(exc)) from exc
    return result.as_dict()


@router.get(
    "/candles",
    summary="Normalized OHLCV history, with a quality report attached",
    description=(
        "The report travels with the bars rather than being logged where the "
        "caller will not read it. `ohlc_trustworthy=false` means: use the "
        "close, never the body or the shadows -- a measured property of some "
        "feeds, not a hypothetical. Bars are returned oldest first, "
        "deduplicated and sorted, so no consumer has to assume an ordering the "
        "provider never promised. Nothing is repaired silently."
    ),
)
async def candles(
    request: Request,
    symbol: str = Query(..., max_length=32),
    timeframe: str = Query("H1", description=" | ".join(str(t) for t in Timeframe)),
    provider: str = Query("mt5", description=" | ".join(str(p) for p in Provider)),
    limit: int = Query(500, ge=1, le=5000),
    store: bool = Query(
        False, description="Also cache the bars. Idempotent on (provider, symbol, timeframe, time)."
    ),
    db: AsyncSession = Depends(get_db),
    _: User = _MARKETS,
) -> dict[str, object]:
    service = _service(request)
    try:
        result = await service.get_bars(
            db, symbol, _timeframe(timeframe), _provider(provider), limit=limit
        )
    except ProviderUnavailable as exc:
        raise DependencyUnavailable(str(exc)) from exc
    stored = 0
    if store:
        symbol_id = await service.symbol_id_for(db, symbol)
        stored = await service.store_bars(db, result.series.bars, symbol_id=symbol_id)
        await db.commit()
    return {**result.as_dict(), "stored": stored}


@router.get(
    "/candles/stored",
    summary="Bars already cached, without touching a provider",
    description=(
        "Reads the local cache only. Useful when a provider is unreachable and "
        "for deterministic replay, and clearly not a live read: the newest "
        "`bar_time` is in the response so a caller can see how old it is."
    ),
)
async def stored_candles(
    request: Request,
    symbol: str = Query(..., max_length=32),
    timeframe: str = Query("H1"),
    provider: str = Query("mt5"),
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    _: User = _MARKETS,
) -> dict[str, object]:
    service = _service(request)
    chosen = _provider(provider)
    frame = _timeframe(timeframe)
    name = await service.provider_symbol(db, symbol, chosen)
    rows = await service.stored_bars(db, chosen, name, frame, limit=limit)
    return {
        "symbol": symbol,
        "provider": str(chosen),
        "provider_symbol": name,
        "timeframe": str(frame),
        "source": "local cache, not a live provider read",
        "bars": [
            {
                "bar_time": r.bar_time.isoformat(),
                "open": str(r.open),
                "high": str(r.high),
                "low": str(r.low),
                "close": str(r.close),
                "volume": str(r.volume) if r.volume is not None else None,
                "spread": str(r.spread) if r.spread is not None else None,
                "spread_availability": r.spread_availability,
            }
            for r in rows
        ],
        "count": len(rows),
        "newest_bar_time": rows[-1].bar_time.isoformat() if rows else None,
    }


@router.get(
    "/quality",
    summary="Data-quality counters for a cached series",
    description=(
        "Missing bars, duplicates and gaps over what is stored. Counts only; "
        "nothing here fills a gap, because a synthesised bar is "
        "indistinguishable from a real one by eye."
    ),
)
async def quality(
    request: Request,
    symbol: str = Query(..., max_length=32),
    timeframe: str = Query("H1"),
    provider: str = Query("mt5"),
    db: AsyncSession = Depends(get_db),
    _: User = _SYSTEM,
) -> dict[str, object]:
    from decimal import Decimal

    from app.marketdata.types import Availability, Bar
    from app.marketdata.validation import inspect_series

    service = _service(request)
    chosen = _provider(provider)
    frame = _timeframe(timeframe)
    name = await service.provider_symbol(db, symbol, chosen)
    rows = await service.stored_bars(db, chosen, name, frame, limit=5000)
    bars = [
        Bar(
            symbol=name,
            provider=chosen,
            timeframe=frame,
            bar_time=r.bar_time,
            open=Decimal(r.open),
            high=Decimal(r.high),
            low=Decimal(r.low),
            close=Decimal(r.close),
            volume=Decimal(r.volume) if r.volume is not None else None,
            spread=Decimal(r.spread) if r.spread is not None else None,
            spread_availability=Availability(r.spread_availability),
        )
        for r in rows
    ]
    return {
        "symbol": symbol,
        "provider": str(chosen),
        "timeframe": str(frame),
        "quality": inspect_series(bars, frame).as_dict(),
    }
