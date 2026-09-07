"""Gross, net, by symbol, by strategy, by bot, by currency.

Sections 10 to 19. Two rules run through all of it:

**Gross and net are different numbers and are never conflated.** Section 11 and
§12. Long 50,000 and short 30,000 is 80,000 gross and +20,000 net, and a report
that showed one where the other was meant would understate the position by more
than half. Every aggregate here carries both.

**Notional comes from the instrument's own metadata, never a formula.** Section
13 warns that `quantity × contract size × price` does not work identically for
every asset, and this repository has the scar: pooling price-scaled quantities
across symbols is the +4,236-point metals error `CLAUDE.md` records at length.
So `notional_of` requires a `ContractSpec` — the measured contract terms L11
already established — and **refuses rather than guessing** when one is missing.
A position whose notional cannot be computed is reported as uncomputable, not
as zero.

**Currency exposure is derived from symbol metadata, never from the name.**
Section 14. `EURUSD` long is EUR long and USD short — but only because the
symbol row says its base is EUR and its quote is USD. Splitting a six-letter
ticker in half would work for the majors and be wrong for everything else, which
is exactly the class of error §14 says not to implement.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


@dataclass(frozen=True)
class PositionView:
    """One position, as the exposure engine needs it.

    Built from the existing `Position` row (L21) rather than being a second
    representation of it — §9 is explicit about that. This carries only what an
    exposure calculation reads.
    """

    position_id: str
    symbol: str
    side: str  # "long" | "short"
    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    strategy_id: str | None = None
    bot_id: str | None = None
    asset_class: str | None = None
    base_currency: str | None = None
    quote_currency: str | None = None
    contract_size: Decimal | None = None
    tick_size: Decimal | None = None
    tick_value: Decimal | None = None

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def price(self) -> Decimal | None:
        """The price to value at: the current one, or the entry as a fallback.

        Named rather than silently substituted -- `notional_of` reports which
        was used, because a position valued at entry in a market that has moved
        is a stale exposure figure and a reader should know.
        """
        return self.current_price if self.current_price is not None else self.entry_price


@dataclass(frozen=True)
class Notional:
    """One position's exposure in account currency, or a refusal."""

    value: Decimal | None
    computable: bool
    reason: str = ""
    priced_at: str = "current"

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else str(self.value),
            "computable": self.computable,
            "reason": self.reason,
            "priced_at": self.priced_at,
        }


def notional_of(position: PositionView) -> Notional:
    """Quantity × contract size × price, when the metadata says that is right.

    Refused rather than approximated when `contract_size` is missing. §13 and
    the metals lesson: a notional computed from an assumed contract size is a
    number in the wrong unit, and pooling numbers in different units is how this
    repository produced a +4,236-point 'result' that was arithmetic rather than
    a finding.
    """
    if position.contract_size is None:
        return Notional(
            None,
            False,
            reason=(
                f"{position.symbol} has no measured contract size, so its notional "
                "cannot be computed. Refused rather than assumed: a notional in the "
                "wrong unit pools with everything else and corrupts the total."
            ),
        )
    price = position.price
    if price is None:
        return Notional(
            None, False, reason=f"{position.symbol} has no price to value the position at"
        )
    return Notional(
        abs(position.quantity) * position.contract_size * price,
        True,
        priced_at="current" if position.current_price is not None else "entry",
    )


def mark_to_market(position: PositionView) -> Decimal | None:
    """One position's unrealized P&L in account currency, or None.

    **Through the platform's single conversion**, `value_per_price_unit` --
    L18 moved it out of the paper portfolio precisely because position sizing
    had grown its own copy of the same arithmetic, and two expressions of one
    conversion is how the money a stop costs comes out differently depending
    on which module asked. A third copy here would undo that.

    `None` when the mark or the contract terms are missing, never zero. §59:
    an unmarkable position is an acknowledged gap, and `pnl.unrealized_of`
    refuses the whole total rather than reporting a partial one as complete.
    """
    from app.symbols.precision import SpecIncomplete, value_per_price_unit

    if position.current_price is None:
        return None
    try:
        per_unit = value_per_price_unit(position.tick_value, position.tick_size)
    except SpecIncomplete:
        return None
    move = (position.current_price - position.entry_price) * position.direction
    return move * abs(position.quantity) * per_unit


@dataclass
class Bucket:
    """Gross and net for one grouping. Section 11 and §12."""

    long_value: Decimal = ZERO
    short_value: Decimal = ZERO
    long_positions: int = 0
    short_positions: int = 0
    #: Positions whose notional could not be computed. Counted rather than
    #: dropped: a total that silently excluded three positions would be wrong
    #: in a way nobody could see.
    uncomputable: int = 0

    @property
    def gross(self) -> Decimal:
        """Total ABSOLUTE exposure. Long 50k + short 30k = 80k."""
        return self.long_value + self.short_value

    @property
    def net(self) -> Decimal:
        """Directional difference. Long 50k - short 30k = +20k."""
        return self.long_value - self.short_value

    @property
    def positions(self) -> int:
        return self.long_positions + self.short_positions

    def add(self, position: PositionView, value: Decimal) -> None:
        if position.direction > 0:
            self.long_value += value
            self.long_positions += 1
        else:
            self.short_value += value
            self.short_positions += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "long": str(self.long_value),
            "short": str(self.short_value),
            "gross": str(self.gross),
            "net": str(self.net),
            "long_positions": self.long_positions,
            "short_positions": self.short_positions,
            "positions": self.positions,
            "uncomputable": self.uncomputable,
        }


@dataclass
class ExposureReport:
    """Everything §10 to §19 asks for, computed once."""

    total: Bucket = field(default_factory=Bucket)
    by_symbol: dict[str, Bucket] = field(default_factory=dict)
    by_strategy: dict[str, Bucket] = field(default_factory=dict)
    by_bot: dict[str, Bucket] = field(default_factory=dict)
    by_asset_class: dict[str, Bucket] = field(default_factory=dict)
    #: §14. Signed: a EURUSD long is EUR positive and USD negative.
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    currency_available: bool = True
    uncomputable: list[dict[str, Any]] = field(default_factory=list)

    def concentration(self) -> dict[str, Any]:
        """Share of gross exposure per symbol. Section 18.

        Reported, never enforced: §18 says not to block a trade here unless the
        risk engine consumes the figure, and the risk engine is what decides.
        """
        gross = self.total.gross
        if gross <= 0:
            return {
                "by_symbol": {},
                "largest": None,
                "note": "no computable exposure, so there is no concentration to report",
            }
        shares = {
            symbol: float(bucket.gross / gross)
            for symbol, bucket in self.by_symbol.items()
            if bucket.gross > 0
        }
        largest = max(shares.items(), key=lambda kv: kv[1], default=None)
        return {
            "by_symbol": {k: round(v, 6) for k, v in sorted(shares.items())},
            "largest": {"symbol": largest[0], "share": round(largest[1], 6)} if largest else None,
            "note": (
                "reported, never enforced. The RISK ENGINE decides whether a "
                "concentration permits a new trade; this only measures it."
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total.as_dict(),
            "by_symbol": {k: v.as_dict() for k, v in sorted(self.by_symbol.items())},
            "by_strategy": {k: v.as_dict() for k, v in sorted(self.by_strategy.items())},
            "by_bot": {k: v.as_dict() for k, v in sorted(self.by_bot.items())},
            "by_asset_class": {k: v.as_dict() for k, v in sorted(self.by_asset_class.items())},
            "by_currency": (
                {k: str(v) for k, v in sorted(self.by_currency.items())}
                if self.currency_available
                else None
            ),
            "currency_note": (
                "derived from each symbol's recorded base and quote currency. A EURUSD "
                "long is EUR long and USD short."
                if self.currency_available
                else (
                    "unavailable: one or more symbols carry no base/quote currency. "
                    "Splitting a ticker in half would work for the majors and be wrong "
                    "for everything else, so it is reported as unavailable instead."
                )
            ),
            "concentration": self.concentration(),
            "uncomputable": self.uncomputable,
            "gross_vs_net": (
                "gross is total ABSOLUTE exposure; net is the directional difference. "
                "Long 50,000 and short 30,000 is 80,000 gross and +20,000 net."
            ),
        }


def _bucket(store: dict[str, Bucket], key: str) -> Bucket:
    if key not in store:
        store[key] = Bucket()
    return store[key]


def compute(positions: list[PositionView]) -> ExposureReport:
    """Every aggregate §10 to §19 asks for, from one pass over the positions."""
    report = ExposureReport()
    currency_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    currency_available = True

    for position in positions:
        notional = notional_of(position)
        if not notional.computable or notional.value is None:
            report.total.uncomputable += 1
            _bucket(report.by_symbol, position.symbol).uncomputable += 1
            report.uncomputable.append(
                {
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "reason": notional.reason,
                }
            )
            continue

        value = notional.value
        report.total.add(position, value)
        _bucket(report.by_symbol, position.symbol).add(position, value)
        _bucket(report.by_strategy, position.strategy_id or "unattributed").add(position, value)
        _bucket(report.by_bot, position.bot_id or "unattributed").add(position, value)
        _bucket(report.by_asset_class, position.asset_class or "unknown").add(position, value)

        # §14. Both currencies must be recorded, or the whole breakdown is
        # withheld -- a partial currency map reads as a complete one.
        if position.base_currency and position.quote_currency:
            signed = value * position.direction
            currency_totals[position.base_currency] += signed
            currency_totals[position.quote_currency] -= signed
        else:
            currency_available = False

    report.by_currency = dict(currency_totals) if currency_available else {}
    report.currency_available = currency_available
    return report


def open_risk(positions: list[PositionView]) -> dict[str, Any]:
    """Potential loss to stop, per position and aggregated. Section 26.

    Uses the same tick-value arithmetic `app.sizing` does — distance to stop, in
    ticks, times the value of a tick — rather than a second formula. §26 is
    explicit: do not duplicate financial formulas when a shared calculator
    exists.

    **A position with no stop has no computable risk to stop**, and that is
    reported rather than treated as zero. Zero would mean "this position cannot
    lose", which is the opposite of what an unstopped position means.
    """
    per_position: list[dict[str, Any]] = []
    total = ZERO
    unstopped = 0
    uncomputable = 0

    for position in positions:
        if position.stop_loss is None:
            unstopped += 1
            per_position.append(
                {
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "risk": None,
                    "reason": (
                        "no stop loss, so there is no risk-to-stop. NOT zero: an "
                        "unstopped position is the one whose loss has no floor."
                    ),
                }
            )
            continue
        if position.tick_size is None or position.tick_value is None or not position.tick_size:
            uncomputable += 1
            per_position.append(
                {
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "risk": None,
                    "reason": (
                        f"{position.symbol} has no measured tick value, so a stop "
                        "distance cannot be turned into money. Refused rather than "
                        "guessed -- the rule `app.sizing` already applies."
                    ),
                }
            )
            continue

        distance = abs(position.entry_price - position.stop_loss)
        ticks = distance / position.tick_size
        risk = ticks * position.tick_value * abs(position.quantity)
        total += risk
        per_position.append(
            {
                "position_id": position.position_id,
                "symbol": position.symbol,
                "risk": str(risk),
                "reason": "",
            }
        )

    by_symbol: dict[str, Decimal] = defaultdict(lambda: ZERO)
    by_strategy: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for position, entry in zip(positions, per_position, strict=True):
        if entry["risk"] is None:
            continue
        by_symbol[position.symbol] += Decimal(entry["risk"])
        by_strategy[position.strategy_id or "unattributed"] += Decimal(entry["risk"])

    return {
        "total": str(total),
        "positions": per_position,
        "by_symbol": {k: str(v) for k, v in sorted(by_symbol.items())},
        "by_strategy": {k: str(v) for k, v in sorted(by_strategy.items())},
        "unstopped_positions": unstopped,
        "uncomputable_positions": uncomputable,
        "complete": unstopped == 0 and uncomputable == 0,
        "note": (
            "the sum covers only the positions whose risk to stop could be computed. "
            f"{unstopped} have no stop and {uncomputable} lack a measured tick value; "
            "neither is counted as zero, because zero would say the position cannot lose."
        ),
    }


def correlation_note() -> dict[str, Any]:
    """Section 27. Reported as unavailable rather than approximated.

    §27 asks for correlated-exposure analysis only *if sufficient market data
    exists*, and says not to create a fake correlation engine. The analysis has
    not been run, and the currency breakdown above is the one real proxy this
    platform can offer for the common-factor risk §27 describes.

    **The reason changed at L66 and the verdict did not.** When this was
    written `market_bars` was empty. It now holds 700 H1 bars -- all of them
    EURUSD, over one month. Correlation needs two or more instruments across a
    common window, so one instrument is still nothing to correlate, but "the
    table is empty" had stopped being true and a reader checking whether the
    situation had changed would have been told the wrong thing.
    """
    return {
        "available": False,
        "reason": (
            "no correlation analysis has been run. It needs a common window of market "
            "data across two or more held instruments, and this deployment has bars "
            "for one instrument only."
        ),
        "closest_available": (
            "the currency breakdown. Several USD-long positions show up there as one "
            "large USD figure, which is the common risk factor §27 describes -- measured "
            "from recorded symbol metadata rather than estimated from prices nobody has."
        ),
    }
