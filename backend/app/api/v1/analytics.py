"""Performance analytics over what actually happened.

**Nothing here executes.** Section 49. This router imports no order manager, no
sizer, no risk decision and no broker write path; every verb is GET, and a
parsed test keeps it that way.

**Nothing here owns a fact.** Section 3. Completed trades come from the journal
(L31), account state from the portfolio engine (L30), orders and fills from the
OMS (L19), model identity from `ai_decisions` (L27), and backtest results from
the backtest engine (L14). Analytics reads all of them and duplicates none.

**Nothing here is fabricated, and nothing is hidden.** Sections 25 and 48. An
empty filter answers with an empty metric block that says so; a ratio from too
small a sample answers `INSUFFICIENT_DATA`; every response carries its sample
size; and no losing trade or unprofitable period is dropped anywhere.

**Environments are never merged silently.** Section 23. `environment` is a
filter and never a default, and every response reports which environments the
rows it summarised actually spanned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import windows as window_engine
from app.analytics.metrics import MIN_TRADES_FOR_RATIO
from app.analytics.service import (
    AI_DIMENSIONS,
    DIMENSIONS,
    MIN_TRADES_FOR_COMPARISON,
    AnalyticsError,
    AnalyticsService,
    Scope,
    utcnow,
)
from app.analytics.windows import Bucket, WindowError
from app.auth.deps import get_db, require_permission
from app.auth.models import User
from app.auth.permissions import Permission
from app.core.errors import NotFound, ValidationFailed
from app.models.accounts import BrokerAccount, PaperAccount

router = APIRouter(prefix="/analytics", tags=["analytics"])

_READ = Depends(require_permission(Permission.view_analytics))

#: One instance. It holds no state.
_SERVICE = AnalyticsService()


# ============================================================= scope building


async def _owned(db: AsyncSession, user: User, account_id: str | None) -> str | None:
    """An account the caller owns, or 404. Section 39.

    Scoped in the query. A missing account and somebody else's give the same
    404, because distinguishing them makes this a membership oracle -- the same
    rule `channels.py` and the portfolio router already record.
    """
    if not account_id:
        return None
    from sqlalchemy import select

    paper = await db.scalar(
        select(PaperAccount.id).where(
            PaperAccount.id == account_id, PaperAccount.user_id == user.id
        )
    )
    if paper is not None:
        return account_id
    broker = await db.scalar(
        select(BrokerAccount.id).where(
            BrokerAccount.id == account_id, BrokerAccount.user_id == user.id
        )
    )
    if broker is not None:
        return account_id
    raise NotFound("no such account for this user")


async def _scope(
    db: AsyncSession,
    user: User,
    *,
    environment: str | None,
    account_id: str | None,
    strategy_version_id: str | None,
    symbol: str | None,
    bot_id: str | None,
    model_key: str | None,
    model_version: str | None,
    ai_mode: str | None,
    period: str | None,
    from_time: datetime | None,
    to_time: datetime | None,
    label: str = "",
) -> Scope:
    try:
        window = window_engine.resolve(period, now=utcnow(), start=from_time, end=to_time)
    except WindowError as exc:
        raise ValidationFailed(str(exc)) from exc
    return Scope(
        window=window,
        environment=environment,
        account_id=await _owned(db, user, account_id),
        strategy_version_id=strategy_version_id,
        symbol=symbol,
        bot_id=bot_id,
        model_key=model_key,
        model_version=model_version,
        ai_mode=ai_mode,
        label=label,
    )


_ENV = Annotated[str | None, Query(description="paper | demo | live. Never defaulted.")]
_PERIOD = Annotated[str | None, Query(description="today | last_7d | this_month | all_time | …")]


def _filters(
    environment: _ENV = None,
    account_id: str | None = Query(None, max_length=36),
    strategy_version_id: str | None = Query(None, max_length=36),
    symbol: str | None = Query(None, max_length=32),
    bot_id: str | None = Query(None, max_length=36),
    model_key: str | None = Query(None, max_length=64),
    model_version: str | None = Query(None, max_length=32),
    ai_mode: str | None = Query(None, max_length=32),
    period: _PERIOD = None,
    from_time: datetime | None = Query(None, description="closed_at lower bound, UTC."),
    to_time: datetime | None = Query(None, description="closed_at upper bound, UTC, exclusive."),
) -> dict[str, Any]:
    return {
        "environment": environment,
        "account_id": account_id,
        "strategy_version_id": strategy_version_id,
        "symbol": symbol,
        "bot_id": bot_id,
        "model_key": model_key,
        "model_version": model_version,
        "ai_mode": ai_mode,
        "period": period,
        "from_time": from_time,
        "to_time": to_time,
    }


def _fail(exc: AnalyticsError) -> ValidationFailed:
    return ValidationFailed(str(exc))


# =================================================================== routes


@router.get(
    "",
    summary="What analytics measures, and what it will not",
    description=(
        "The contract: metric definitions, the sample-size floors, the timezone "
        "policy, the dimensions available, and the figures this level "
        "deliberately does not compute."
    ),
)
async def contract(_: User = _READ) -> dict[str, Any]:
    return {
        "source_of_truth": {
            "trades": "the trade journal (L31)",
            "positions": "the position manager (L21), through the portfolio engine",
            "account": "the portfolio engine (L30). Analytics keeps no second state.",
            "orders": "the OMS (L19)",
            "ai": "ai_decisions (L27) and the model registry (L28)",
            "backtests": "the backtest engine (L14). Analytics runs no simulator.",
        },
        "definitions": {
            "win_rate": "winning trades / total closed trades. A break-even trade "
            "counts in the denominator and in neither numerator.",
            "profit_factor": "gross profit / abs(gross loss). INSUFFICIENT_DATA when "
            "there are no losers -- not infinity, and not a large number.",
            "expectancy": "the average trade result.",
            "payoff_ratio": "average win / abs(average loss).",
            "sharpe_per_trade": "mean / standard deviation, PER TRADE. Not annualised: "
            "a fixed window supplies no trades-per-year figure, and inventing one is "
            "how a Sharpe of 0.3 becomes a Sharpe of 2.",
            "sortino_per_trade": "mean / downside deviation, where downside deviation "
            "divides by the count of downside observations.",
            "risk_free_rate": "zero, stated. These are per-trade results over no "
            "position, and financing belongs in the trade's own costs.",
            "max_drawdown": "peak-to-subsequent-trough over time-ordered points.",
            "recovery_factor": "net profit / maximum drawdown. INSUFFICIENT_DATA when "
            "there was no drawdown -- dividing by zero is undefined, not impressive.",
        },
        "sample_floors": {
            "ratio": MIN_TRADES_FOR_RATIO,
            "comparison": MIN_TRADES_FOR_COMPARISON,
            "note": "below the floor a figure is INSUFFICIENT_DATA rather than a number.",
        },
        "dimensions": sorted(set(DIMENSIONS) | set(AI_DIMENSIONS)),
        "windows": window_engine.describe(),
        "units": {
            "r": "poolable across trades and symbols. The figure to read.",
            "currency": "NOT poolable across trades sized by different stop distances.",
            "points": "NOT poolable across symbols whose point sizes differ.",
        },
        "does_not": [
            "place, modify or cancel anything",
            "own account state -- the portfolio engine does",
            "run a backtest -- the backtest engine does",
            "change a risk limit, a kill switch or a sizing rule",
            "annualise a ratio",
            "trim an outlier or hide a losing period",
            "claim a cause from a comparison",
        ],
    }


@router.get(
    "/summary",
    summary="Section 29's contract, in both units",
    description=(
        "Trade counts, win rate, gross and net, profit factor, expectancy, "
        "Sharpe and Sortino per trade, streaks and distribution -- computed "
        "twice, once in account currency and once in R. Read R when pooling: "
        "net currency cannot be pooled across trades sized by different stop "
        "distances."
    ),
)
async def summary(
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    try:
        return await _SERVICE.summary(db, await _scope(db, user, **filters))
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/equity",
    summary="The realized curve and the account curve, never merged",
    description=(
        "The realized curve is cumulative net P&L from the journal and cannot "
        "contain a deposit. The account curve is equity as recorded and CAN: "
        "this platform stores no cash movements, so a deposit and a profit look "
        "identical there. Read the realized curve for performance."
    ),
)
async def equity(
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    try:
        return await _SERVICE.equity(db, await _scope(db, user, **filters))
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/drawdown",
    summary="Peak, trough, recovery, and every period",
    description=(
        "Computed over the time-ordered realized curve. The last period is left "
        "OPEN when the curve has not regained its peak: reporting it as "
        "recovered at the final point would say the account came back when it "
        "has not."
    ),
)
async def drawdown(
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    try:
        return await _SERVICE.drawdown(db, await _scope(db, user, **filters))
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/breakdown",
    summary="Performance grouped by one dimension",
    description=(
        "strategy | symbol | bot | exit_reason | mode | side | model | "
        "model_version | ai_mode | regime. Every group carries its sample size, "
        "because a difference between 43 trades and 11 is not a finding."
    ),
)
async def breakdown(
    dimension: str = Query(..., description="One of the dimensions in GET /analytics"),
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    try:
        return await _SERVICE.by(db, await _scope(db, user, **filters), dimension)
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/time-breakdown",
    summary="Performance by hour, day, week, month, quarter, year or weekday",
    description=(
        "Bucketed in Python from UTC rather than with `date_trunc`, whose "
        "behaviour differs between PostgreSQL and SQLite -- a boundary that "
        "moved with the engine would make the tests agree with a production "
        "they do not match."
    ),
)
async def time_breakdown(
    bucket: str = Query(
        "day", description="hour | day | week | month | quarter | year | day_of_week"
    ),
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    try:
        chosen = Bucket(bucket)
    except ValueError as exc:
        raise ValidationFailed(f"{bucket!r} is not a bucket") from exc
    try:
        return await _SERVICE.time_breakdown(db, await _scope(db, user, **filters), chosen)
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/execution",
    summary="Order and fill quality from the OMS",
    description=(
        "Fill ratio, rejection ratio, partial fills, latency and slippage. "
        "Latency is measured only between timestamps that both exist -- an "
        "order missing one is excluded, never given an assumed value, and the "
        "count of what was measurable is reported beside the figure."
    ),
)
async def execution(
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    try:
        return await _SERVICE.execution(db, await _scope(db, user, **filters))
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/exposure",
    summary="Current exposure, from the portfolio engine",
    description=(
        "Delegated, never recomputed. The portfolio engine owns balance, "
        "equity, margin and exposure; a second calculation here would be a "
        "second state, and the two would eventually disagree. Analytics reports "
        "it and changes nothing -- the risk engine decides what a limit permits."
    ),
)
async def exposure(
    request: Request,
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    scope = await _scope(db, user, **filters)
    adapter = None
    if scope.account_id and scope.environment and scope.environment != "paper":
        registry = getattr(request.app.state, "brokers", None)
        if registry is not None:
            try:
                adapter = registry.get(scope.account_id)
            except Exception:  # noqa: BLE001 - not connected is a state
                adapter = None
    try:
        return await _SERVICE.exposure(db, scope, adapter=adapter)
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/positions",
    summary="How many positions are open",
    description=(
        "Counted here; VALUED by the portfolio engine, which owns unrealised "
        "P&L and refuses a total when any position cannot be marked."
    ),
)
async def positions(
    filters: dict[str, Any] = Depends(_filters),
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    return await _SERVICE.open_positions(db, await _scope(db, user, **filters))


@router.get(
    "/compare",
    summary="Two sets side by side, with their sample sizes",
    description=(
        "Observational. This is a difference between two SELECTED sets, not a "
        "measured effect: nothing was randomised, so whatever separates them "
        "may be what the filter selected for rather than what it did. Say 'this "
        "set had a higher win rate in this sample', never 'X improved "
        "performance'."
    ),
)
async def compare(
    left_environment: str | None = Query(None),
    left_strategy_version_id: str | None = Query(None, max_length=36),
    left_ai_mode: str | None = Query(None, max_length=32),
    left_symbol: str | None = Query(None, max_length=32),
    right_environment: str | None = Query(None),
    right_strategy_version_id: str | None = Query(None, max_length=36),
    right_ai_mode: str | None = Query(None, max_length=32),
    right_symbol: str | None = Query(None, max_length=32),
    account_id: str | None = Query(None, max_length=36),
    period: _PERIOD = None,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    async def side(
        environment: str | None,
        strategy_version_id: str | None,
        ai_mode: str | None,
        symbol: str | None,
        label: str,
    ) -> Scope:
        return await _scope(
            db,
            user,
            environment=environment,
            account_id=account_id,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            bot_id=None,
            model_key=None,
            model_version=None,
            ai_mode=ai_mode,
            period=period,
            from_time=None,
            to_time=None,
            label=label,
        )

    left = await side(left_environment, left_strategy_version_id, left_ai_mode, left_symbol, "left")
    right = await side(
        right_environment, right_strategy_version_id, right_ai_mode, right_symbol, "right"
    )
    try:
        return await _SERVICE.compare(db, [left, right])
    except AnalyticsError as exc:
        raise _fail(exc) from exc


@router.get(
    "/backtest/{backtest_id}",
    summary="A backtest's own metrics, read not recomputed",
    description=(
        "Served from the backtest record the engine wrote. Analytics runs no "
        "simulator: a second one would produce a second answer, and the "
        "measured figures in this repository all came from the first."
    ),
)
async def backtest(
    backtest_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = _READ,
) -> dict[str, Any]:
    from app.models.research import Backtest

    row = await db.get(Backtest, backtest_id)
    if row is None or row.requested_by_user_id != user.id:
        # The same 404 for missing and not-yours. A membership oracle is a
        # membership oracle whatever it is guarding.
        raise NotFound("no such backtest for this user")
    return {
        "backtest_id": row.id,
        "environment": "backtest",
        "status": row.status,
        "metrics": (row.summary or {}).get("metrics") if row.summary else None,
        "summary": row.summary,
        "source": "the backtest engine (L14). Read, never recomputed.",
        "note": (
            "a backtest is NOT a live result and the two are never combined. Its "
            "figures are in POINTS and its Sharpe is per trade, not annualised."
        ),
    }


__all__ = ["router"]
