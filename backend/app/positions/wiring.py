"""The one place the deployed position monitor is assembled.

L21 built nine exit policies, a manager and a monitor, and all three were
complete and tested. None of them ran: `PositionMonitor` had no construction
site outside tests, the one production `PositionManager` only ever called
`close_now` (which takes the caller's decision and never consults a policy),
and there was no settings key, API field or column by which a trail, a
break-even or a maximum hold could be asked for. So `PolicySet.decide` had no
production caller at all.

This module is the missing caller. It builds the policy set from settings,
picks the executor by mode, reads the quotes a decision is made against, and
reports what it could not read rather than defaulting around it.

**Two tiers, and the split is not arbitrary.** `atr_multiple`, `fraction` and
`max_hold` are dimensionless or time, so they mean the same thing on every
instrument and can live in settings. `distance`, `trigger_distance`, `buffer`
and `level` are PRICES -- 0.0001 on EURUSD is 0.10 on XAUUSD -- so a
deployment-wide value would be wrong for every symbol but one. That is why no
settings key for them ever existed, and it is a reason rather than an
oversight. `PartialTakeProfitPolicy.level` is a per-position price and stays
out of tier 1 entirely; the break-even buffer is expressed as an ATR multiple
so it can be configured without naming a price.

**Everything defaults to off, and that is a measurement.** A 3.0 ATR trail had
a median out-of-sample expectancy of -217 at D1 against -58 for the fixed
1.5x1.5 bracket, and move-to-breakeven -123, across a 16x8 grid
(`reports/exit_search_d1.json`, and the comments on the policies themselves).
All eight exits in that grid were negative. Turning one on by default here
would be overruling the repository's own data from the wiring layer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.marketdata.types import Bar, Provider, Timeframe
from app.models.accounts import BrokerAccount
from app.models.execution import Position
from app.oms.registry import OrderManagerRegistry
from app.positions.broker_executor import BrokerExitExecutor
from app.positions.executor import ExitExecutor, PaperExitExecutor
from app.positions.ingest import BROKER_MODES
from app.positions.manager import PositionManager
from app.positions.monitor import PositionMonitor
from app.positions.policies import (
    BreakEvenPolicy,
    MarketState,
    PolicySet,
    RiskContext,
    TrailingStopPolicy,
)
from app.risk.engine import RiskEngine
from app.symbols.service import broker_symbol_for

log = logging.getLogger("app.positions.wiring")


class MarketReader(Protocol):
    """The two reads the monitor makes of the market data service.

    Narrower than `MarketDataService` on purpose. The defect this module
    fixes was a call with the WRONG SIGNATURE that a bare `except Exception`
    hid, so the contract worth stating is the signature itself -- and a test
    double then satisfies it by matching, rather than by being cast past the
    type checker.
    """

    async def get_quote(
        self, db: AsyncSession, internal_symbol: str, provider: Provider
    ) -> Any: ...

    async def get_bars(
        self,
        db: AsyncSession,
        internal_symbol: str,
        timeframe: Timeframe,
        provider: Provider,
        *,
        limit: int = 500,
    ) -> Any: ...


#: How long one symbol's ATR is reused. An ATR is a property of a bar series,
#: so recomputing it every 5s pass would be the same answer at 12x the cost --
#: and 12 `get_bars` calls a minute per symbol is real load on a provider that
#: rate limits. Keyed by (code, timeframe, period).
ATR_CACHE_SECONDS = 300.0


# ============================================================ tier 1: policies


def policy_set_for(settings: Settings) -> PolicySet:
    """The configured policy set, or the inert default when nothing is set.

    An unconfigured deployment gets exactly `PolicySet.default()`: a trail
    with no distance and no multiple (whose `proposed_stop` returns None on
    every call) and a time exit with no maximum hold. That is today's
    behaviour and it is preserved deliberately -- see the module docstring.
    """
    trailing = TrailingStopPolicy(atr_multiple=settings.position_trail_atr_multiple)
    break_even = (
        BreakEvenPolicy(
            atr_multiple=settings.position_break_even_atr_multiple,
            # A multiple, not a price, so it can be configured at all. A
            # break-even stop placed exactly at the entry loses the spread
            # every time it fires, which is not break-even -- and the size of
            # the spread is per instrument, so only an ATR-relative buffer can
            # be stated once.
            buffer_atr_multiple=settings.position_break_even_buffer_atr,
        )
        if settings.position_break_even_atr_multiple is not None
        else None
    )
    max_hold = (
        timedelta(hours=float(settings.position_max_hold_hours))
        if settings.position_max_hold_hours
        else None
    )
    # `partial=` is deliberately not passed. `PartialTakeProfitPolicy.level`
    # is an absolute price and belongs to a position, not to a deployment.
    return PolicySet.default(trailing=trailing, max_hold=max_hold, break_even=break_even)


def configured(settings: Settings) -> dict[str, object]:
    """What the operator asked for, for the status route to show.

    "The trail is off" is worth being able to read. An inert policy and an
    absent one look identical from outside, and the whole defect this module
    fixes was a mechanism that existed and did nothing.
    """
    return {
        "trail_atr_multiple": (
            str(settings.position_trail_atr_multiple)
            if settings.position_trail_atr_multiple is not None
            else None
        ),
        "break_even_atr_multiple": (
            str(settings.position_break_even_atr_multiple)
            if settings.position_break_even_atr_multiple is not None
            else None
        ),
        "break_even_buffer_atr": (
            str(settings.position_break_even_buffer_atr)
            if settings.position_break_even_buffer_atr is not None
            else None
        ),
        "max_hold_hours": settings.position_max_hold_hours,
        "atr_timeframe": settings.position_atr_timeframe,
        "atr_period": settings.position_atr_period,
    }


# =========================================================== tier 1: executor


def executor_for(
    *,
    managers: OrderManagerRegistry,
    risk: RiskEngine,
    sessions: async_sessionmaker[AsyncSession],
    mode: str,
) -> ExitExecutor:
    """The executor for a mode, chosen by the mode alone.

    Paper closes in the simulator; demo and live go through the account's
    order manager to the adapter. There is no branch by which a paper position
    could reach a venue or a demo position be closed by the simulator.

    Lifted out of `api/v1/positions.py::_executor` so the route and the
    monitor make the same choice from the same code rather than from two
    copies that can drift.
    """
    if mode == "paper":
        return PaperExitExecutor()
    from app.execution.store import store_for

    return BrokerExitExecutor(
        managers,
        mode=mode,
        # L45 C-2: the engine that mints the close's Approval. The OMS creates
        # nothing without one.
        risk=risk,
        # L45 C-1, for closes. An order the venue may hold and the database
        # has never heard of is the state no guard can reason about.
        store=store_for(sessions),
    )


# ============================================================== tier 1: quotes


class AtrCache:
    """One symbol's ATR, reused for `ttl` seconds.

    Not a general cache: it holds the last computed value per (code,
    timeframe, period) and the monotonic time it was computed, and that is
    all the monitor needs. A miss costs one `get_bars`.
    """

    def __init__(self, ttl_seconds: float = ATR_CACHE_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._values: dict[tuple[str, str, int], tuple[datetime, Decimal | None]] = {}

    def get(self, key: tuple[str, str, int], now: datetime) -> tuple[bool, Decimal | None]:
        found = self._values.get(key)
        if found is None:
            return False, None
        at, value = found
        if (now - at).total_seconds() >= self.ttl_seconds:
            return False, None
        return True, value

    def put(self, key: tuple[str, str, int], now: datetime, value: Decimal | None) -> None:
        self._values[key] = (now, value)


def true_range_mean(bars: list[Bar], period: int) -> Decimal | None:
    """ATR as a simple mean of true range over `period` bars.

    This is the definition `tools/rule_backtest.atr_series` uses and its
    docstring names -- "simple mean of true range over n bars, matches
    atr_from() in mt5_paper" -- reproduced here in Decimal rather than
    imported, because importing it would pull numpy and `tools/` onto the
    monitor's per-pass path for ten lines of arithmetic. A second
    implementation is a real cost, so it must compute the SAME quantity: a
    test asserts these agree with the toolkit's on the same bars.

    Needs `period + 1` bars, because the first true range needs a previous
    close. Fewer returns None, which makes both ATR policies refuse.
    """
    if period < 1 or len(bars) < period + 1:
        return None
    ranges: list[Decimal] = []
    for previous, bar in zip(bars[:-1], bars[1:], strict=True):
        prior_close, high, low = previous.close, bar.high, bar.low
        ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    window = ranges[-period:]
    return sum(window, Decimal("0")) / Decimal(period)


async def atr_for(
    db: AsyncSession,
    code: str,
    *,
    market_data: MarketReader,
    settings: Settings,
    cache: AtrCache,
    provider: Provider = Provider.mt5,
    now: datetime | None = None,
) -> Decimal | None:
    """The ATR a stop move is measured against, or None.

    None is the fail-closed answer and both ATR policies already honour it:
    "no ATR, no trail: nothing is invented". A provider that cannot serve
    bars therefore freezes the stop where it is rather than moving it by a
    guessed distance.
    """
    stamp = now or datetime.now(UTC)
    key = (code, str(settings.position_atr_timeframe), int(settings.position_atr_period))
    hit, value = cache.get(key, stamp)
    if hit:
        return value
    try:
        series = await market_data.get_bars(
            db,
            code,
            Timeframe(settings.position_atr_timeframe),
            provider,
            limit=int(settings.position_atr_period) + 1,
        )
        atr = true_range_mean(list(series.series.bars), int(settings.position_atr_period))
    except Exception as exc:  # noqa: BLE001 - an unreadable ATR is not a trail
        log.warning(
            "no ATR for this symbol; stop movers will propose nothing",
            extra={"event": "position_atr_unavailable", "symbol": code, "reason": str(exc)[:200]},
        )
        atr = None
    cache.put(key, stamp, atr)
    return atr


async def venue_quote_for(
    db: AsyncSession, row: Position, code: str, *, brokers: object
) -> MarketState | None:
    """The quote from the venue that actually holds this position, or None.

    **Only for a position held at a venue.** A broker position is closed AT a
    broker, and that broker's own book is the authoritative price for it;
    pricing the exit off a research feed instead would value a trade at a
    number the counterparty never quoted. Nothing is pooled or stored, so
    this does not merge the broker book with the normalized feed.
    """
    if row.mode not in BROKER_MODES or not row.broker_account_id:
        return None
    account = await db.get(BrokerAccount, row.broker_account_id)
    if account is None or brokers is None:
        return None
    try:
        adapter = brokers.get(account.name)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not registered in this process
        return None
    try:
        venue_symbol = await broker_symbol_for(db, code, account.broker)
        quote = await adapter.get_quote(venue_symbol or code)
    except Exception:  # noqa: BLE001 - a venue that cannot quote is a refusal
        return None
    if quote is None or quote.bid is None or quote.ask is None:
        return None
    return MarketState(
        symbol=code,
        bid=Decimal(str(quote.bid)),
        ask=Decimal(str(quote.ask)),
        as_of=getattr(quote, "at", None) or datetime.now(UTC),
    )


async def feed_quote_for(
    db: AsyncSession,
    code: str,
    *,
    market_data: MarketReader | None,
    provider: Provider = Provider.mt5,
) -> MarketState | None:
    """The normalized feed's quote for a code, or None.

    **This path had never worked.** `api/v1/positions.py` called
    `service.get_quote(db, code)` where the method is
    `get_quote(self, db, internal_symbol, provider)` -- a TypeError, swallowed
    by a bare `except Exception`, returning None every time -- and then read
    `quote.bid` although the method returns a `QuoteResult` whose quote is at
    `.quote`. Two defects on one line, each of which alone made the fallback
    dead, which is why the close route could only ever price a position from a
    venue.
    """
    if market_data is None:  # pragma: no cover - wired at startup
        return None
    try:
        result = await market_data.get_quote(db, code, provider)
    except Exception as exc:  # noqa: BLE001 - a feed that cannot quote is a refusal
        log.debug(
            "the feed could not quote this symbol",
            extra={"event": "position_feed_quote_failed", "symbol": code, "reason": str(exc)[:200]},
        )
        return None
    quote = getattr(result, "quote", None)
    if quote is None or quote.bid is None or quote.ask is None:
        return None
    return MarketState(
        symbol=code,
        bid=Decimal(str(quote.bid)),
        ask=Decimal(str(quote.ask)),
        as_of=getattr(quote, "at", None) or datetime.now(UTC),
    )


def quotes_source(
    *,
    market_data: MarketReader,
    brokers: object,
    settings: Settings,
    mode: str,
    cache: AtrCache | None = None,
) -> Callable[[AsyncSession, dict[str, Position]], Awaitable[dict[str, MarketState]]]:
    """Build the monitor's quote source.

    Takes the open positions keyed by their tradable CODE -- which is what
    `PositionManager.open_by_code` resolves, and the same key `run_once`
    looks quotes up under. The row is needed as well as the code because a
    broker position is priced at the venue that holds it, and only the row
    says which venue that is.

    One quote per code, the venue's first and the feed's otherwise, each
    carrying the ATR the stop movers need. A symbol that cannot be quoted is
    simply absent from the map, and `run_once` already records that as `no
    quote available` and leaves the position alone -- no quote is not a
    reason to close, and not a reason to pretend the position is fine
    either.
    """
    atr_cache = cache or AtrCache()

    async def source(db: AsyncSession, rows: dict[str, Position]) -> dict[str, MarketState]:
        quotes: dict[str, MarketState] = {}
        for code, row in rows.items():
            state = await venue_quote_for(db, row, code, brokers=brokers)
            if state is None:
                state = await feed_quote_for(db, code, market_data=market_data)
            if state is None:
                continue
            atr = await atr_for(
                db, code, market_data=market_data, settings=settings, cache=atr_cache
            )
            quotes[code] = state if atr is None else replace_atr(state, atr)
        return quotes

    return source


def replace_atr(state: MarketState, atr: Decimal) -> MarketState:
    """`MarketState` is frozen, so the ATR is attached by rebuilding it."""
    return MarketState(
        symbol=state.symbol, bid=state.bid, ask=state.ask, as_of=state.as_of, atr=atr
    )


# ====================================================== tier 1: risk context


async def context_for() -> RiskContext:
    """The account state a position-level policy may consult.

    **Every field is left at its default, and that is the decision rather
    than a stub.** A risk HALT is not a FLATTEN. `risk/engine.py` says it of
    a loss streak -- "a halt would stop the session managing what is already
    open, and a streak is a reason to stop OPENING, never a reason to stop
    watching" -- and `tools/risk_gate.py` says it of the kill switch, that
    feeding the stop file into the engine "would convert an operator stop
    that flushes into one that abandons every open position".

    `RiskState.emergency_stop` and `daily_loss_locked` are in BLOCKING, which
    means no NEW order may be created. Mapping either onto
    `RiskContext.emergency_stop` or `daily_loss_breached` would make this
    monitor liquidate the whole book the moment a daily limit tripped, which
    is the opposite of what those controls are for. A flatten is a separate,
    explicit operator instruction and needs its own control; leaving these
    False with the reasoning written down is the correct answer, not a gap.
    """
    return RiskContext()


# ===================================================== tier 1: floating P&L


async def floating_for(
    db: AsyncSession, *, managers: OrderManagerRegistry, rows: list[Position], mode: str
) -> dict[str, Decimal]:
    """Each open position's floating P&L as the VENUE reports it, by our id.

    Paper returns nothing, so `RiskExitPolicy` refuses on an unknown figure,
    which is its documented contract. The substitute a caller is tempted to
    compute -- (price - entry) x quantity -- is measured wrong in this
    repository: `positions/manager.py` records a live MT5 close that booked
    -0.0000004 where the account received -0.04, because that arithmetic
    omits contract size, and no single multiplier repairs it across EURUSD,
    USDJPY, XAUUSD and DE40.

    `profit + swap`, never `profit` alone: a position held overnight reads
    positive gross and negative net of financing, and it is the net figure a
    risk exit should act on.
    """
    if mode not in BROKER_MODES:
        return {}
    # Every open row, not one per symbol: two positions can share a code and
    # each has its own floating figure.
    wanted = {r.broker_position_id: r.id for r in rows if r.broker_position_id is not None}
    if not wanted:
        return {}
    floating: dict[str, Decimal] = {}
    for account_id, manager in managers.managers.items():
        try:
            positions = await manager.adapter.get_positions()
        except Exception as exc:  # noqa: BLE001 - one venue down is not every venue
            log.warning(
                "could not read floating P&L from a venue",
                extra={
                    "event": "position_floating_unavailable",
                    "account": account_id,
                    "reason": str(exc)[:200],
                },
            )
            continue
        for held in positions:
            ours = wanted.get(held.position_id)
            if ours is None:
                continue
            profit = held.profit if held.profit is not None else None
            if profit is None:
                continue
            floating[ours] = Decimal(str(profit)) + Decimal(str(getattr(held, "swap", 0) or 0))
    return floating


# ================================================ the deployed monitor itself


def monitor_for(
    *,
    sessions: async_sessionmaker[AsyncSession],
    managers: OrderManagerRegistry,
    risk: object,
    market_data: MarketReader,
    brokers: object,
    hub: object,
    settings: Settings,
) -> PositionMonitor:
    """Assemble the position monitor this deployment would run.

    One monitor for the process's own trading mode: `PositionManager` holds
    one executor and `executor_for` picks it by mode, so a paper monitor
    cannot reach a venue and a demo monitor cannot be answered by the
    simulator.

    `risk` is the RiskService, not an engine, and `engine_for_close()` is
    called per pass rather than once here. `RiskService.load()` replaces its
    switch set wholesale during startup recovery, so an engine captured at
    construction would close positions against switches that had since been
    replaced.
    """
    mode = settings.trading_mode.value
    cache = AtrCache()

    def build(db: AsyncSession) -> PositionManager:
        return PositionManager(
            db,
            executor_for(
                managers=managers,
                risk=risk.engine_for_close(),  # type: ignore[attr-defined]
                sessions=sessions,
                mode=mode,
            ),
            policy_set_for(settings),
        )

    async def floating(db: AsyncSession, rows: list[Position]) -> dict[str, Decimal]:
        return await floating_for(db, managers=managers, rows=rows, mode=mode)

    async def publish(event: object) -> None:
        await hub.publish(event)  # type: ignore[attr-defined]

    return PositionMonitor(
        sessions,
        build,
        quotes_source(
            market_data=market_data,
            brokers=brokers,
            settings=settings,
            mode=mode,
            cache=cache,
        ),
        context_for,
        floating=floating,
        interval_seconds=settings.position_monitor_interval_seconds,
        mode=mode,
        publish=publish if hub is not None else None,
    )


__all__ = [
    "ATR_CACHE_SECONDS",
    "AtrCache",
    "atr_for",
    "configured",
    "context_for",
    "executor_for",
    "feed_quote_for",
    "floating_for",
    "monitor_for",
    "policy_set_for",
    "quotes_source",
    "true_range_mean",
    "venue_quote_for",
]
