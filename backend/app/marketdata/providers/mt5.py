"""MT5 market data, wrapping the toolkit's existing fetchers unchanged.

`tools/rule_backtest.fetch_rates` is **called, not copied**. Its stepping-down
retry exists because the terminal rejects an over-large request with "Invalid
params" rather than returning what it has, so an unconditional big ask
silently yields nothing for every symbol. That behaviour is load-bearing and
this adapter must not reimplement it.

`trim_to_years` is likewise reused: the D1 series on this feed reaches back to
1971, decades before EURUSD existed, and those backfilled bars carry synthetic
prices and a placeholder spread of 50 points against 3 for a real quote.

**This adapter reads. It cannot trade.** Execution is the broker adapter's job
(`app.brokers`), and the split is deliberate -- Step 18 of this level and the
whole of L10 depend on market data and execution being separately replaceable.
The connection is shared rather than duplicated: `connect()` here is the
toolkit's, and merging the four copies of it is L10's work, not this level's.

MT5 runs only on Windows with a logged-in terminal. On any other host, or with
the package absent, `status()` says so and every call raises
`ProviderUnavailable`. It never falls back to another provider on its own: a
silent substitution would put another venue's prices behind a broker symbol.

**The server clock is not UTC, and this adapter measures the difference rather
than assuming it.** MT5 encodes a stamp as the broker's *wall clock* rendered
as a UTC epoch, so reading it back with `fromtimestamp(t, tz=UTC)` yields a
time shifted by the broker's offset -- on this broker, three hours into the
future. That was caught here by this level's own validator, which flagged a
live quote as "stamped in the future" by exactly 3h00m. The same subtlety is
why `mt5_paper.server_day_start` exists.

See `_server_offset_seconds` for how the offset is measured and what the
measurement cannot distinguish.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType

from app.core import toolkit
from app.marketdata.base import (
    HistoricalProvider,
    ProviderStatus,
    ProviderUnavailable,
    QuoteProvider,
)
from app.marketdata.types import Availability, Bar, Provider, Quote, Timeframe

log = logging.getLogger("app.marketdata.mt5")

# The three the toolkit's `timeframe_const` maps. Adding one means adding it
# there too, and a timeframe this adapter claims but the toolkit cannot
# translate would fail at the terminal rather than here.
SUPPORTED: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4, Timeframe.D1)


def _toolkit() -> ModuleType:
    """Import `rule_backtest` without moving it.

    The toolkit is a sibling of the backend, not a package it depends on --
    unless `pip install -e ./tools` has made it one, which `app.core.toolkit`
    prefers when present. Either way the source is the same file, so every
    command in README.md and NIGHTLY.md keeps working against it.
    """
    return toolkit.load("rule_backtest")


class MT5MarketData(QuoteProvider, HistoricalProvider):
    name = Provider.mt5
    supports_quotes = True
    supports_history = True
    timeframes = SUPPORTED

    def __init__(self, terminal_path: str | None = None) -> None:
        self.terminal_path = terminal_path
        self._mt5 = None
        self._offset_seconds: int | None = None

    # --------------------------------------------------------------- clock

    # Broker server offsets are whole or half hours in practice. Rounding the
    # raw difference to the nearest half hour is what separates the offset from
    # a stale tick: a 3h00m gap is an offset with a fresh tick, a 3h07m gap is
    # the same offset with a seven-minute-old tick.
    OFFSET_QUANTUM_SECONDS = 1800

    def _server_offset_seconds(self, mt5) -> int:  # noqa: ANN001 - the MT5 module
        """How far the broker's wall clock runs ahead of true UTC, in seconds.

        Measured, not configured: a tick's stamp is compared against this
        machine's UTC clock and the difference rounded to the nearest half hour.

        **What this cannot distinguish**, stated plainly: a tick that is itself
        more than fifteen minutes stale will have part of its age absorbed into
        the offset. That is acceptable only because the offset needs to be right
        to the half hour, while a quarter-hour-stale tick is already the alarm
        this level raises separately. It is measured per adapter instance rather
        than cached across restarts, so a broker that shifts at a DST boundary
        is picked up on the next connect.
        """
        if self._offset_seconds is not None:
            return self._offset_seconds
        for symbol in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
            tick = mt5.symbol_info_tick(symbol)
            if tick and getattr(tick, "time", 0):
                raw = tick.time - datetime.now(UTC).timestamp()
                quantum = self.OFFSET_QUANTUM_SECONDS
                self._offset_seconds = int(round(raw / quantum) * quantum)
                log.info(
                    "measured broker server offset",
                    extra={
                        "event": "mt5_server_offset",
                        "offset_seconds": self._offset_seconds,
                        "raw_difference_seconds": round(raw, 1),
                    },
                )
                return self._offset_seconds
        # No tick from any liquid symbol: the terminal is up but the feed is
        # not. Refusing is right -- an assumed offset would silently shift every
        # timestamp this adapter produces.
        raise ProviderUnavailable(
            "cannot measure the broker's server offset: no tick from any of "
            "EURUSD, GBPUSD, USDJPY, XAUUSD"
        )

    def _to_utc(self, server_epoch: int | float, offset: int) -> datetime:
        """A server stamp to a true UTC instant."""
        return datetime.fromtimestamp(server_epoch - offset, tz=UTC)

    # ----------------------------------------------------------- connection

    def _import_mt5(self):  # noqa: ANN202 - the MetaTrader5 module
        if self._mt5 is not None:
            return self._mt5
        if sys.platform != "win32":
            raise ProviderUnavailable(
                "MetaTrader5 runs only on Windows with a logged-in terminal; "
                f"this host is {sys.platform}. See DEPLOYMENT.md."
            )
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise ProviderUnavailable(f"MetaTrader5 package is not installed: {exc}") from exc
        self._mt5 = mt5
        return mt5

    def _connect(self):  # noqa: ANN202
        """The toolkit's connect, not a fifth copy of it.

        `ARCHITECTURE_MIGRATION.md` records four existing copies of this
        routine and schedules the merge for L10. This adapter deliberately
        adds none: it calls one of the existing ones.
        """
        toolkit = _toolkit()
        mt5 = self._import_mt5()
        try:
            toolkit.connect(self.terminal_path)
        except Exception as exc:  # noqa: BLE001 - reported as unavailable, not raised raw
            raise ProviderUnavailable(f"MT5 connect failed: {type(exc).__name__}") from exc
        return mt5, toolkit

    async def status(self) -> ProviderStatus:
        try:
            await asyncio.to_thread(self._connect)
        except ProviderUnavailable as exc:
            return ProviderStatus(self.name, False, str(exc), True, True, SUPPORTED)
        except Exception as exc:  # noqa: BLE001 - status never raises
            return ProviderStatus(self.name, False, f"{type(exc).__name__}", True, True, SUPPORTED)
        try:
            mt5 = self._import_mt5()
            offset = await asyncio.to_thread(self._server_offset_seconds, mt5)
        except ProviderUnavailable as exc:
            return ProviderStatus(self.name, False, str(exc), True, True, SUPPORTED)
        hours = offset / 3600
        return ProviderStatus(
            self.name,
            True,
            f"terminal reachable; broker server clock is UTC{hours:+g}h, measured",
            True,
            True,
            SUPPORTED,
        )

    # --------------------------------------------------------------- quotes

    def _quote_sync(self, provider_symbol: str) -> Quote:
        mt5, _ = self._connect()
        tick = mt5.symbol_info_tick(provider_symbol)
        if tick is None:
            raise ProviderUnavailable(f"no tick for {provider_symbol!r} from the terminal")
        # The terminal stamps a tick with the SERVER's wall clock rendered as
        # a UTC epoch. Reading it as UTC directly puts every timestamp this
        # adapter produces ahead by the broker's offset -- three hours here --
        # and every staleness check with it. The offset is measured and removed
        # so `at` is a true UTC instant.
        at = self._to_utc(tick.time, self._server_offset_seconds(mt5))
        return Quote(
            symbol=provider_symbol,
            provider=self.name,
            at=at,
            received_at=datetime.now(UTC),
            bid=Decimal(str(tick.bid)) if tick.bid else None,
            ask=Decimal(str(tick.ask)) if tick.ask else None,
            last=Decimal(str(tick.last)) if getattr(tick, "last", 0) else None,
            volume=None,
            spread_availability=Availability.derived,
        )

    async def get_quote(self, provider_symbol: str) -> Quote:
        return await asyncio.to_thread(self._quote_sync, provider_symbol)

    # -------------------------------------------------------------- history

    def _bars_sync(self, provider_symbol: str, timeframe: Timeframe, limit: int) -> list[Bar]:
        mt5, toolkit = self._connect()
        offset = self._server_offset_seconds(mt5)
        rates = toolkit.fetch_rates(
            mt5, provider_symbol, limit, timeframe=str(timeframe), min_bars=1
        )
        if rates is None:
            raise ProviderUnavailable(
                f"the terminal returned no {timeframe} history for {provider_symbol!r}"
            )
        bars: list[Bar] = []
        for row in rates:
            # MT5 stamps a bar with its OPEN time on the SERVER's clock. That
            # open-time convention matches the normalized Bar; the zone does
            # not, so the measured offset is removed and `bar_time` is true UTC,
            # which is what `market_bars` stores.
            spread_points = int(row["spread"]) if "spread" in rates.dtype.names else 0
            bars.append(
                Bar(
                    symbol=provider_symbol,
                    provider=self.name,
                    timeframe=timeframe,
                    bar_time=self._to_utc(int(row["time"]), offset),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=(
                        Decimal(str(row["tick_volume"]))
                        if "tick_volume" in rates.dtype.names
                        else None
                    ),
                    # A recorded 0 is UNRECORDED, not free. `cost_profile.py`
                    # measured that averaging zeros in halves the apparent cost
                    # of trading, so the absence is preserved as None.
                    spread=Decimal(spread_points) if spread_points > 0 else None,
                    spread_availability=(
                        Availability.available if spread_points > 0 else Availability.not_available
                    ),
                    complete=True,
                )
            )
        return bars[-limit:]

    async def get_bars(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        *,
        limit: int = 500,
        end: datetime | None = None,
    ) -> list[Bar]:
        self.require_timeframe(timeframe)
        if end is not None:
            # `copy_rates_from_pos` counts back from the newest bar and takes
            # no end bound. Claiming to honour one would silently return the
            # wrong window, so it is refused instead.
            raise ProviderUnavailable(
                "the MT5 adapter reads back from the newest bar and cannot take an "
                "end bound; ask for more bars and slice, or use the replay store"
            )
        return await asyncio.to_thread(self._bars_sync, provider_symbol, timeframe, limit)
