"""Does the platform's record of a trade agree with the venue's?

    python -m app.brokers.venue_audit --mode demo
    python -m app.brokers.venue_audit --mode demo --json
    python -m app.brokers.venue_audit --mode demo --strict   # exit 1 on any disagreement

Every other check in this platform compares the platform against itself. This
one asks the only question that cannot be answered that way: for each position
the platform believes it opened, does MetaTrader's own deal history say the
same thing about the entry, the size, the exit and the money?

**The venue is the authority and this module never writes.** It reads
`positions` and reads MT5, and reports. A disagreement is a finding for a person
-- the same rule `app/brokers/reconcile.py` states, for the same reason:
the natural instinct on finding a mismatch is to correct one side, and both
directions of that instinct are wrong.

**Why it is not part of reconciliation**, though it sits beside it.
`reconcile.py` asks "does the venue still hold what we think it holds" -- a
question about OPEN positions, answered against the venue's current book. This
asks "was what we recorded true", which is a question about CLOSED ones,
answered against the deal history. A position can reconcile perfectly all the
way to a close and still have been booked at the wrong price.

**Why it lives here and not in `app/journal/`.** That package is forbidden by
test from importing `app.brokers`, `MetaTrader5` or even `app.core.settings` --
deliberately, so the journal cannot reach an execution path. This reads a
terminal, so it belongs on the venue side of that line.

**Windows only**, like everything that reads a terminal.

Exit codes: 0 when the report was produced, 1 with `--strict` when anything
disagreed, 2 when the audit itself could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.session import make_engine, make_session_factory
from app.models.execution import Position

#: How far apart two prices may be and still be called the same. The venue and
#: the platform both store more digits than any instrument quotes, so an exact
#: comparison would report a disagreement on floating-point dust.
PRICE_TOLERANCE = Decimal("0.00001")

#: Money is booked to the cent by the venue. Anything larger is a real
#: difference, not rounding.
MONEY_TOLERANCE = Decimal("0.01")

_TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools",
)


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass
class VenueTrade:
    """What MetaTrader's deal history says about one position."""

    position_id: str
    entry_price: Decimal | None = None
    volume: Decimal | None = None
    exit_price: Decimal | None = None
    realized: Decimal | None = None
    closed: bool = False
    deals: int = 0


@dataclass
class Finding:
    position_id: str
    field: str
    ours: str | None
    theirs: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "field": self.field,
            "ours": self.ours,
            "theirs": self.theirs,
            "detail": self.detail,
        }


@dataclass
class Report:
    checked: int = 0
    agreed: int = 0
    findings: list[Finding] = field(default_factory=list)
    unknown_to_venue: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings and not self.unknown_to_venue

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "agreed": self.agreed,
            "clean": self.clean,
            "findings": [f.as_dict() for f in self.findings],
            "unknown_to_venue": self.unknown_to_venue,
        }


def read_venue(position_ids: set[str], days: int = 30) -> tuple[dict[str, VenueTrade], str]:
    """MetaTrader's own history for these positions. Read-only, always."""
    if _TOOLS not in sys.path:
        sys.path.insert(0, _TOOLS)
    try:
        import MetaTrader5 as mt5  # noqa: N813
    except ImportError:
        return {}, "the MetaTrader5 package is not installed in this environment"

    from datetime import datetime, timedelta

    try:
        if not mt5.initialize():
            return {}, f"terminal did not initialise: {mt5.last_error()}"
    except Exception as exc:  # noqa: BLE001 - a terminal fails in many ways
        return {}, f"terminal initialise raised {type(exc).__name__}: {exc}"

    try:
        now = datetime.now()
        deals = mt5.history_deals_get(now - timedelta(days=days), now + timedelta(days=1)) or []
        found: dict[str, VenueTrade] = {}
        for deal in deals:
            pid = str(getattr(deal, "position_id", ""))
            if pid not in position_ids:
                continue
            trade = found.setdefault(pid, VenueTrade(position_id=pid))
            trade.deals += 1
            price = _dec(getattr(deal, "price", None))
            if getattr(deal, "entry", 1) == 0:
                # DEAL_ENTRY_IN: the open.
                trade.entry_price = price
                trade.volume = _dec(getattr(deal, "volume", None))
            else:
                # Anything else takes size off. The last one is the close.
                trade.exit_price = price
                trade.closed = True
                money = (
                    (_dec(getattr(deal, "profit", 0)) or Decimal(0))
                    + (_dec(getattr(deal, "swap", 0)) or Decimal(0))
                    + (_dec(getattr(deal, "commission", 0)) or Decimal(0))
                )
                trade.realized = (trade.realized or Decimal(0)) + money
        return found, f"{len(deals)} deals read over {days} days"
    finally:
        mt5.shutdown()


def compare(rows: list[Position], venue: dict[str, VenueTrade]) -> Report:
    """Compare what we recorded against what the venue booked."""
    report = Report()
    for row in rows:
        pid = str(row.broker_position_id)
        report.checked += 1
        theirs = venue.get(pid)
        if theirs is None:
            # Not a disagreement about a value -- the venue has no record of it
            # at all, which is a bigger thing and is reported separately.
            report.unknown_to_venue.append(pid)
            continue

        before = len(report.findings)

        ours_entry = _dec(row.entry_price)
        if ours_entry is not None and theirs.entry_price is not None:
            if abs(ours_entry - theirs.entry_price) > PRICE_TOLERANCE:
                report.findings.append(
                    Finding(
                        pid,
                        "entry_price",
                        str(ours_entry),
                        str(theirs.entry_price),
                        "the platform booked an entry the venue did not fill at",
                    )
                )

        ours_volume = _dec(row.initial_quantity or row.quantity)
        if ours_volume is not None and theirs.volume is not None:
            if ours_volume != theirs.volume:
                report.findings.append(
                    Finding(
                        pid,
                        "volume",
                        str(ours_volume),
                        str(theirs.volume),
                        "the size opened is not the size the venue filled",
                    )
                )

        closed_here = str(row.status) == "closed"
        if closed_here != theirs.closed:
            report.findings.append(
                Finding(
                    pid,
                    "status",
                    "closed" if closed_here else str(row.status),
                    "closed" if theirs.closed else "open",
                    "the two disagree about whether this position is finished",
                )
            )

        # Money, and only where the venue has closed it. An open position has
        # nothing realised to compare.
        if theirs.closed and theirs.realized is not None:
            ours_money = _dec(row.realized_pnl)
            if ours_money is None:
                report.findings.append(
                    Finding(
                        pid,
                        "realized_pnl",
                        None,
                        str(theirs.realized),
                        "the venue booked money and the platform recorded none; a gap, "
                        "not a zero -- see the position event's realized_pnl_source",
                    )
                )
            elif abs(ours_money - theirs.realized) > MONEY_TOLERANCE:
                report.findings.append(
                    Finding(
                        pid,
                        "realized_pnl",
                        str(ours_money),
                        str(theirs.realized),
                        "the money the platform booked is not the money the account received",
                    )
                )

        if len(report.findings) == before:
            report.agreed += 1
    return report


async def load_positions(mode: str) -> list[Position]:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as db:
            rows = await db.scalars(
                select(Position)
                .where(Position.mode == mode, Position.broker_position_id.is_not(None))
                .order_by(Position.created_at)
            )
            return list(rows)
    finally:
        await engine.dispose()


def render(report: Report, detail: str, mode: str) -> str:
    lines = ["", f"  VENUE AUDIT  ({mode})", f"  {detail}", ""]
    lines.append(f"  {report.checked} position(s) checked, {report.agreed} agree with the venue")
    if report.unknown_to_venue:
        lines.append("")
        lines.append("  THE VENUE HAS NO RECORD OF:")
        for pid in report.unknown_to_venue:
            lines.append(f"    {pid}")
        lines.append("    (outside the history window, or a position this venue never held)")
    if report.findings:
        lines.append("")
        lines.append("  DISAGREEMENTS")
        for f in report.findings:
            lines.append(f"    {f.position_id}  {f.field}")
            lines.append(f"        ours:   {f.ours}")
            lines.append(f"        venue:  {f.theirs}")
            lines.append(f"        {f.detail}")
    lines.append("")
    lines.append("  CLEAN" if report.clean else "  DISAGREEMENTS FOUND")
    lines.append("")
    if not report.clean:
        lines.append("  Nothing was written. The venue is the authority on what it did;")
        lines.append("  what our record should say about it is a decision for a person.")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="demo", help="demo | live | paper (default demo)")
    ap.add_argument("--days", type=int, default=30, help="history window to read")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any disagreement")
    args = ap.parse_args(argv)

    try:
        rows = asyncio.run(load_positions(args.mode))
        venue, detail = read_venue({str(r.broker_position_id) for r in rows}, args.days)
        if not venue and rows:
            message = f"no venue history could be read: {detail}"
            if args.json:
                print(json.dumps({"error": message}, indent=2))
            else:
                print(f"\n  {message}\n")
            return 2
        report = compare(rows, venue)
    except Exception as exc:  # noqa: BLE001 - an audit that crashes proves nothing
        message = f"the audit could not run: {type(exc).__name__}: {exc}"
        if args.json:
            print(json.dumps({"error": message}, indent=2))
        else:
            print(f"\n  {message}\n")
        return 2

    if args.json:
        print(json.dumps({**report.as_dict(), "detail": detail, "mode": args.mode}, indent=2))
    else:
        print(render(report, detail, args.mode))
    return 1 if args.strict and not report.clean else 0


if __name__ == "__main__":
    raise SystemExit(main())
