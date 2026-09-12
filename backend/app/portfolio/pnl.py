"""Realized, unrealized, daily — and the day boundary, stated once.

Sections 20 to 23.

**Realized P&L comes from the trade journal, never from a second history.**
Section 51. `trades` is L19's record of what a venue confirmed; this reads it.
Recomputing realized profit from position events would produce a second answer
that eventually disagrees with the first, and the one nobody looked at is the
one somebody would quote.

**Realized and unrealized are never double-counted.** Section 20. A closed trade
contributes to realized and has no position; an open position contributes to
unrealized and no trade row. The total is their sum precisely because the two
sets are disjoint.

**The trading day boundary is `app.risk.state.day_start`, not a new one.**
Section 21 asks that the boundary be defined explicitly and consistently. It
already is: L17 chose it, and `CLAUDE.md` records why the obvious alternative is
wrong — MT5 renders a server timestamp through the LOCAL zone, so
`.replace(hour=0)` lands on midnight of the operator's clock and a daily loss
limit silently counts from 18:30 the previous day on a UTC+5:30 machine. A
second definition here would reintroduce exactly that bug.

**Drawdown never resets its peak by accident.** Section 22. `peak_equity` is
carried in, not recomputed from whatever window happens to be in memory: a peak
derived from the last 30 days would fall every time the window rolled past the
old high, and the drawdown would shrink without the account recovering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


@dataclass(frozen=True)
class PnL:
    """The four figures, and what each is made of. Section 20."""

    realized: Decimal | None
    unrealized: Decimal | None
    realized_today: Decimal | None
    trades_today: int | None
    day_start: datetime | None
    #: How many closed trades the realized figure covers. §13's rule applied
    #: here: a P&L without its trade count invites being read as a track record.
    realized_trades: int = 0
    open_positions: int = 0
    unrealized_unavailable: tuple[str, ...] = ()
    #: The same figure as `realized_today` over the trading week. Read by the
    #: risk engine's weekly-loss veto, which treats None as a veto rather than
    #: as zero. Defaulted so a caller that has not computed it says "unknown"
    #: instead of claiming a flat week.
    realized_week: Decimal | None = None
    week_start: datetime | None = None

    @property
    def total(self) -> Decimal | None:
        """Realized + unrealized, or None when either is unknown.

        `None` rather than treating a missing half as zero: a total that
        silently omitted the unrealized side would read as a complete figure.
        """
        if self.realized is None or self.unrealized is None:
            return None
        return self.realized + self.unrealized

    def as_dict(self) -> dict[str, Any]:
        return {
            "realized": _money(self.realized),
            "unrealized": _money(self.unrealized),
            "total": _money(self.total),
            "realized_today": _money(self.realized_today),
            "trades_today": self.trades_today,
            "realized_trades": self.realized_trades,
            "open_positions": self.open_positions,
            "realized_week": _money(self.realized_week),
            "day_start": self.day_start.isoformat() if self.day_start else None,
            "week_start": self.week_start.isoformat() if self.week_start else None,
            "unrealized_unavailable": list(self.unrealized_unavailable),
            "sources": {
                "realized": "the trade journal (L19) -- what a venue confirmed",
                "unrealized": "open positions valued at their current mark",
                "day_boundary": (
                    "app.risk.state.day_start, the boundary L17 already chose. A second "
                    "definition here would reintroduce the bug CLAUDE.md records: MT5 "
                    "renders a server stamp through the LOCAL zone, so a naive midnight "
                    "counts from 18:30 the previous day on a UTC+5:30 machine."
                ),
            },
            "no_double_counting": (
                "a closed trade contributes to realized and has no position; an open "
                "position contributes to unrealized and has no trade row. The total is "
                "their sum because the two sets are disjoint."
            ),
        }


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def unrealized_of(marked: list[Decimal | None]) -> tuple[Decimal | None, int]:
    """Sum of position marks, or None when any position could not be marked.

    All-or-nothing deliberately. A partial unrealized total is a number a reader
    will treat as the whole, and §59 forbids presenting an incomplete figure as
    a real one. The count of unmarked positions is returned so the caller can
    name them.
    """
    missing = sum(1 for value in marked if value is None)
    if missing:
        return None, missing
    return sum((value for value in marked if value is not None), ZERO), 0


@dataclass
class Drawdown:
    """Peak, current, maximum and recovery. Section 22."""

    peak_equity: Decimal | None = None
    peak_at: datetime | None = None
    current_equity: Decimal | None = None
    max_drawdown: Decimal | None = None
    max_drawdown_at: datetime | None = None

    @property
    def current(self) -> Decimal | None:
        """Peak minus current, floored at zero. A new high is not a drawdown."""
        if self.peak_equity is None or self.current_equity is None:
            return None
        return max(ZERO, self.peak_equity - self.current_equity)

    @property
    def current_pct(self) -> Decimal | None:
        current = self.current
        if current is None or not self.peak_equity:
            return None
        return current / self.peak_equity

    @property
    def recovered(self) -> bool | None:
        """Whether equity is back at or above its peak.

        `None` when either figure is missing -- "not recovered" and "we cannot
        tell" are different, and only the first is a reason to keep the position
        smaller.
        """
        if self.peak_equity is None or self.current_equity is None:
            return None
        return self.current_equity >= self.peak_equity

    def observe(self, equity: Decimal, at: datetime) -> None:
        """Fold one equity reading in. The peak only ever rises.

        §22: do not reset the historical peak accidentally. This method has no
        branch that lowers `peak_equity`, so a caller cannot cause one by
        passing a shorter window.
        """
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
            self.peak_at = at
        self.current_equity = equity
        drop = self.peak_equity - equity
        if drop > ZERO and (self.max_drawdown is None or drop > self.max_drawdown):
            self.max_drawdown = drop
            self.max_drawdown_at = at

    def as_dict(self) -> dict[str, Any]:
        return {
            "peak_equity": _money(self.peak_equity),
            "peak_at": self.peak_at.isoformat() if self.peak_at else None,
            "current_equity": _money(self.current_equity),
            "current": _money(self.current),
            "current_pct": None if self.current_pct is None else float(self.current_pct),
            "max_drawdown": _money(self.max_drawdown),
            "max_drawdown_at": self.max_drawdown_at.isoformat() if self.max_drawdown_at else None,
            "recovered": self.recovered,
            "rule": (
                "the peak only ever rises. A peak recomputed from a rolling window would "
                "fall as the window passed the old high, and the drawdown would shrink "
                "without the account recovering."
            ),
        }


def from_curve(points: list[tuple[datetime, Decimal]]) -> Drawdown:
    """A drawdown built from an equity curve, oldest first."""
    drawdown = Drawdown()
    for at, equity in points:
        drawdown.observe(equity, at)
    return drawdown


@dataclass(frozen=True)
class MarginUtilisation:
    """Used margin against equity. Section 24."""

    used: Decimal | None
    equity: Decimal | None
    ratio: Decimal | None
    warning_at: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "used": _money(self.used),
            "equity": _money(self.equity),
            "ratio": None if self.ratio is None else float(self.ratio),
            "warning_at": None if self.warning_at is None else float(self.warning_at),
            "note": (
                "margin utilisation is not portfolio risk. It says how much of the "
                "account the broker is holding against open positions, which is a "
                "different question from how much could be lost."
            ),
        }


def margin_utilisation(
    used: Decimal | None, equity: Decimal | None, *, warning_at: Decimal | None = None
) -> MarginUtilisation:
    """§24, and the ratio is None whenever either input is."""
    ratio: Decimal | None = None
    if used is not None and equity is not None and equity > ZERO:
        ratio = used / equity
    return MarginUtilisation(used=used, equity=equity, ratio=ratio, warning_at=warning_at)
