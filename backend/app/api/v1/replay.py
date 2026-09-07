"""Replay control: create, start, pause, resume, step, stop, speed, state.

Sessions are owned. Someone else's answers exactly as one that does not exist,
because telling a caller that an id is taken by another user is a membership
oracle.

**Replay execution is simulated by construction.** These routes reach
`app.replay`, which holds no broker adapter and imports none. There is no
setting that could route a replay order to a venue, because there is no venue
reference in the package to route to. That is a structural guarantee, not a
configuration one, and a test asserts the absence.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.backtests import CostsIn
from app.auth.deps import current_user, get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.backtest.config import BacktestConfig, ConfigError, CostModel, SizingMode
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.marketdata.base import ProviderUnavailable
from app.marketdata.types import Provider, TimeframeError, parse_timeframe
from app.models.research import ReplaySession as ReplaySessionRow
from app.replay.clock import SPEEDS, IllegalTransition, SpeedError
from app.replay.service import ReplayBusy, ReplayService
from app.strategies.registry import UnknownStrategy

router = APIRouter(prefix="/replay", tags=["replay"])

_RUN = Depends(require_permission(Permission.run_backtests))


class CreateIn(BaseModel):
    strategy_key: Annotated[str, Field(min_length=1, max_length=64)]
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    timeframe: str = "H1"
    provider: str = "mt5"
    # `CostsIn` is L14's, imported rather than restated. A replay and a
    # backtest have to be describable by the SAME request body, or "they agree
    # on the same configuration" is a claim no caller can actually make -- and
    # a field present on one and missing from the other is a silent divergence
    # rather than a difference anyone would notice.
    costs: CostsIn
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    start: datetime | None = None
    end: datetime | None = None
    initial_capital: Decimal = Field(default=Decimal("100000"), gt=0)
    quantity: Decimal = Field(default=Decimal("0.01"), gt=0)
    # Carried so the two bodies stay the SAME shape (see `costs` above). L18
    # wired the risk modes into the backtester and not into replay, so asking
    # for one here is REFUSED with that reason -- accepting the field and
    # ignoring it would report a replay at a flat lot that the caller believes
    # was risk-sized, which is the silent divergence this shape exists to stop.
    sizing_mode: str = Field(default="fixed_quantity")
    risk_amount: Decimal | None = Field(default=None, gt=0)
    risk_percent: Decimal | None = Field(default=None, gt=0, le=100)
    stop_atr: Decimal = Field(default=Decimal("1.5"), gt=0)
    target_atr: Decimal = Field(default=Decimal("1.5"), gt=0)
    max_hold_bars: int = Field(default=240, ge=1, le=10_000)
    max_bars: int = Field(default=1000, ge=100, le=20_000)


class SpeedIn(BaseModel):
    speed: float


def _service(request: Request) -> ReplayService:
    service: ReplayService = request.app.state.replay
    return service


def _session(request: Request, session_id: str, user: User):  # noqa: ANN202
    try:
        return _service(request).get(session_id, user.id)
    except KeyError as exc:
        raise NotFound(f"no replay session {session_id} for this user") from exc


def _config(body: CreateIn) -> BacktestConfig:
    try:
        timeframe = parse_timeframe(body.timeframe)
    except TimeframeError as exc:
        raise ValidationFailed(str(exc)) from exc
    try:
        provider = Provider(body.provider)
    except ValueError as exc:
        raise ValidationFailed(f"unknown provider {body.provider!r}") from exc
    if body.sizing_mode != SizingMode.fixed_quantity.value:
        raise ValidationFailed(
            f"replay supports {SizingMode.fixed_quantity.value} sizing only; "
            f"{body.sizing_mode!r} is implemented in the BACKTESTER (L18) and not in "
            "the replay engine, which carries one quantity on the portfolio and is "
            "proved trade-for-trade identical to simulate(). Refused rather than "
            "accepted and ignored"
        )
    config = BacktestConfig(
        strategy_key=body.strategy_key,
        symbol=body.symbol.upper(),
        timeframe=timeframe,
        provider=provider,
        # The same cost model the backtester uses. Replay and backtest share
        # their financial assumptions so their results can be compared.
        costs=CostModel(
            spread_points=body.costs.spread_points,
            commission_per_trade=body.costs.commission_per_trade,
            slippage_points=body.costs.slippage_points,
            swap_long_per_night=body.costs.swap_long_per_night,
            swap_short_per_night=body.costs.swap_short_per_night,
        ),
        strategy_config=body.strategy_config,
        start=body.start,
        end=body.end,
        initial_capital=body.initial_capital,
        sizing_mode=SizingMode.fixed_quantity,
        quantity=body.quantity,
        stop_atr=body.stop_atr,
        target_atr=body.target_atr,
        max_hold_bars=body.max_hold_bars,
        max_bars=body.max_bars,
    )
    try:
        config.validate()
    except ConfigError as exc:
        raise ValidationFailed(str(exc)) from exc
    return config


@router.post(
    "/sessions",
    status_code=201,
    summary="Create a replay session",
    description=(
        "Loads and validates the dataset, then holds the session at `queued` "
        "until it is started. Corrupt bars refuse the session rather than "
        "being repaired. Nothing is executed against a broker at any point: "
        "replay reaches simulated execution by construction."
    ),
)
async def create(
    body: CreateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
) -> dict[str, object]:
    config = _config(body)
    try:
        session = await _service(request).create(db, config, user.id)
    except UnknownStrategy as exc:
        raise NotFound(str(exc)) from exc
    except ReplayBusy as exc:
        raise Conflict(str(exc)) from exc
    except ProviderUnavailable as exc:
        from app.core.errors import DependencyUnavailable

        raise DependencyUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return {**session.snapshot(), "speeds": list(SPEEDS)}


@router.get("/sessions", summary="Replay sessions belonging to this user")
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = _RUN,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    rows = (
        await db.scalars(
            select(ReplaySessionRow)
            .where(ReplaySessionRow.requested_by_user_id == user.id)
            .order_by(ReplaySessionRow.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "sessions": [
            {
                "session_id": r.id,
                "status": r.status,
                "universe": r.universe,
                "timeframe": r.timeframe,
                "from_time": r.from_time.isoformat(),
                "to_time": r.to_time.isoformat(),
                "cursor_time": r.cursor_time.isoformat() if r.cursor_time else None,
                "speed": r.speed,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get(
    "/sessions/{session_id}",
    summary="Live state: simulated time, progress, position, balance",
    description=(
        "`simulated_time` is the timestamp of the bar last revealed, never the "
        "wall clock. That is the only time the strategy sees, which is what "
        "makes a replayed decision the decision that would have been made then."
    ),
)
async def state(request: Request, session_id: str, user: User = _RUN) -> dict[str, object]:
    return _session(request, session_id, user).snapshot()


def _control(name: str, summary: str, description: str = ""):  # noqa: ANN202
    """One route per control; they differ only in which service method runs."""

    @router.post(f"/sessions/{{session_id}}/{name}", summary=summary, description=description)
    async def handler(  # noqa: ANN202
        request: Request, session_id: str, user: User = _RUN
    ) -> dict[str, object]:
        session = _session(request, session_id, user)
        service = _service(request)
        try:
            await getattr(service, name)(session)
        except IllegalTransition as exc:
            raise Conflict(str(exc)) from exc
        return session.snapshot()

    return handler


_control(
    "start",
    "Start playing",
    "Runs in a background task. Closing the browser does not stop it: the "
    "frontend controls a backend session, it does not host one.",
)
_control(
    "pause",
    "Pause",
    "The cursor does not advance while paused, so resuming continues from exactly the same bar.",
)
_control("resume", "Resume from the paused bar")
_control(
    "stop",
    "Stop permanently",
    "Terminal. A stopped session cannot be restarted, because a second run "
    "under the same id would produce a second, different result.",
)


@router.post(
    "/sessions/{session_id}/step",
    summary="Advance exactly one bar",
    description=(
        "Reveals one bar, evaluates the strategy over the prefix ending at it, "
        "processes any exit or entry, updates equity and returns the events in "
        "sequence. Only allowed on a created or paused session: stepping a "
        "running one would race the driver and reveal two bars for one request."
    ),
)
async def step(request: Request, session_id: str, user: User = _RUN) -> dict[str, object]:
    session = _session(request, session_id, user)
    try:
        events = await _service(request).step(session)
    except IllegalTransition as exc:
        raise Conflict(str(exc)) from exc
    return {**session.snapshot(), "events": [e.as_dict() for e in events]}


@router.patch(
    "/sessions/{session_id}/speed",
    summary="Change playback speed",
    description=(
        "Speed paces the sleep between bars and nothing else. It cannot skip, "
        "reorder or merge an event, so the same session at 1x and 100x "
        "produces identical trades -- which a test asserts."
    ),
)
async def set_speed(
    body: SpeedIn, request: Request, session_id: str, user: User = _RUN
) -> dict[str, object]:
    session = _session(request, session_id, user)
    try:
        await _service(request).set_speed(session, body.speed)
    except SpeedError as exc:
        raise ValidationFailed(str(exc)) from exc
    return session.snapshot()


@router.get(
    "/sessions/{session_id}/events",
    summary="The most recent replay events, in sequence",
    description=(
        "Bounded to the recent window: a long session produces tens of "
        "thousands of events and keeping them all would grow without limit. "
        "The trades are the durable record."
    ),
)
async def events(
    request: Request, session_id: str, user: User = _RUN, limit: int = Query(100, ge=1, le=500)
) -> dict[str, object]:
    session = _session(request, session_id, user)
    return {
        "session_id": session_id,
        "events": [e.as_dict() for e in session.recent[-limit:]],
        "sequence": session.clock._seq,
    }


@router.get(
    "/sessions/{session_id}/trades",
    summary="Simulated trades so far",
    description=(
        "Every trade carries `execution_mode: REPLAY`, so a replay result can "
        "never be pooled with a live, paper or backtest record by accident."
    ),
)
async def trades(request: Request, session_id: str, user: User = _RUN) -> dict[str, object]:
    session = _session(request, session_id, user)
    return {
        "session_id": session_id,
        "execution_mode": "REPLAY",
        "trades": session.engine.portfolio.trades,
        "count": len(session.engine.portfolio.trades),
    }


@router.get(
    "/sessions/{session_id}/metrics",
    summary="Metrics so far, computed by L14's implementation",
    description=(
        "The same `compute_metrics` the backtester uses; there is one of those, "
        "not two. Ratios still report NOT_AVAILABLE below 20 trades."
    ),
)
async def metrics(request: Request, session_id: str, user: User = _RUN) -> dict[str, object]:
    session = _session(request, session_id, user)
    return {
        "session_id": session_id,
        "execution_mode": "REPLAY",
        "metrics": session.engine.metrics(),
        "equity_curve": session.engine.portfolio.equity_curve[-200:],
    }


@router.get(
    "/assumptions",
    summary="How replay executes, and how it relates to the backtester",
)
async def assumptions(_: User = Depends(current_user)) -> dict[str, object]:
    from app.backtest.config import ExecutionModel

    return {
        "execution": ExecutionModel().describe(),
        "relationship_to_backtest": (
            "The same rules, implemented incrementally so the session can be "
            "paused and stepped. A test runs the same strategy over the same "
            "bars through both and asserts the trade lists are identical."
        ),
        "clock": (
            "Simulated market time is the timestamp of the bar last revealed. "
            "The machine's clock is used only to pace playback and cannot reach "
            "a trading decision."
        ),
        "risk": (
            "Enforced. Every entry passes the same `app.risk.RiskEngine` the paper "
            "venue uses, and a veto stops the entry. `now` is the BAR's time, not "
            "the wall clock -- measured against the machine, every signal in a "
            "historical replay is years stale and nothing would ever trade. The "
            "default limits are permissive on purpose: a replay is compared "
            "trade-for-trade against `simulate()`, which has no limits, so "
            "imposing some by default would stop it reproducing the backtest."
        ),
        "safety": (
            "Replay holds no broker adapter and imports none. Simulated "
            "execution is structural, not configured."
        ),
        "limitations": [
            "One symbol per session; multi-symbol and multi-timeframe are not "
            "implemented and are not faked.",
            "Candle replay only. Tick replay needs tick data the platform does not store.",
            "The bracket comes from ATR multiples, as in L14; a strategy's "
            "suggested stop is not used.",
            "Position sizing is fixed quantity. L18 wired the risk modes into the "
            "BACKTESTER, not into replay: this engine carries one quantity on the "
            "portfolio and is proved trade-for-trade identical to `simulate()`, and "
            "making the size vary per position changes that engine rather than "
            "configuring it. Stated as a limitation rather than half-wired.",
        ],
    }
