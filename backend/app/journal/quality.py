"""What did not add up. Section 44.

**Detect, flag, and never correct.** §32 makes a completed trade a historical
fact and §44 says not to silently correct one. So every check here returns a
FINDING and nothing here writes a price, a quantity or a timestamp.

That distinction is the whole module. A journal that quietly repaired an
impossible timestamp would leave a record that looks clean and is wrong, and the
repair would be invisible — which is the same failure `CLAUDE.md` records about
the order log that stored the requested price as the entry: the arithmetic was
right at every step and the number that reached the record was still wrong,
because nothing said which was which.

A finding is not a fault in the trade. `missing_broker_id` on a paper trade is
expected — there is no broker — so the checks that only apply to a venue-facing
trade say so rather than firing on every simulated one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


@dataclass(frozen=True)
class Finding:
    """One thing about this row that does not add up."""

    code: str
    detail: str
    #: `warning` when the figure is usable and suspect; `error` when it is not
    #: usable at all. Two levels rather than five: §44 asks for detection, and a
    #: severity ladder nobody acts on differently is a ladder nobody reads.
    severity: str = "warning"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


def check(trade: Any, *, closes: int | None = None) -> list[Finding]:
    """Every §44 check, against one recorded trade.

    Returns `[]` when nothing is wrong — which is a different fact from `None`,
    and the column stores the difference: `None` means the checks have not run.
    """
    findings: list[Finding] = []
    mode = getattr(trade, "mode", None)
    opened_at = getattr(trade, "opened_at", None)
    closed_at = getattr(trade, "closed_at", None)

    if getattr(trade, "entry_price", None) is None:
        findings.append(Finding("missing_entry", "no entry price is recorded", "error"))
    if getattr(trade, "exit_price", None) is None:
        findings.append(Finding("missing_exit", "no exit price is recorded", "error"))

    if opened_at is None or closed_at is None:
        findings.append(
            Finding("missing_timestamp", "the trade cannot say when it opened or closed", "error")
        )
    elif closed_at < opened_at:
        findings.append(
            Finding(
                "negative_duration",
                f"closed_at {closed_at.isoformat()} precedes opened_at "
                f"{opened_at.isoformat()}. Reported, never reordered: swapping them "
                "would produce a plausible trade that did not happen.",
                "error",
            )
        )

    volume = getattr(trade, "volume", None)
    if volume is None or volume <= ZERO:
        findings.append(
            Finding(
                "invalid_quantity",
                f"volume is {volume}, which is not a tradeable size",
                "error",
            )
        )

    gross = getattr(trade, "gross_profit", None)
    net = getattr(trade, "net_profit", None)
    commission = getattr(trade, "commission", None) or ZERO
    swap = getattr(trade, "swap", None) or ZERO
    fees = getattr(trade, "fees", None) or ZERO
    if gross is not None and net is not None:
        expected = gross - commission - swap - fees
        # Costs are quoted to four decimals; a discrepancy below that is
        # rounding and above it is a cost booked twice or not at all -- which
        # is §18's double-counting, made detectable.
        if abs(net - expected) > Decimal("0.0001"):
            findings.append(
                Finding(
                    "pnl_does_not_reconcile",
                    f"net {net} is not gross {gross} less commission {commission}, "
                    f"swap {swap} and fees {fees} (expected {expected}). One of the "
                    "costs is counted twice or not at all.",
                    "error",
                )
            )

    if mode != "paper" and not getattr(trade, "broker_position_id", None):
        findings.append(
            Finding(
                "missing_broker_id",
                "a venue-facing trade carries no broker position id, so it cannot be "
                "reconciled against the venue by id",
            )
        )

    if getattr(trade, "status", None) == "reconciliation_required":
        findings.append(
            Finding(
                "reconciliation_required",
                "the platform and the venue disagree about this position. The figures "
                "here are the platform's and are NOT confirmed.",
                "error",
            )
        )

    if not getattr(trade, "exit_reason", None):
        findings.append(
            Finding("missing_exit_reason", "nothing recorded why this trade was closed")
        )

    if closes is not None and closes == 0 and getattr(trade, "position_id", None):
        findings.append(
            Finding(
                "no_confirmed_close",
                "the position is linked but no close event carried a fill price, so the "
                "exit price could not be derived from a confirmed fill",
                "error",
            )
        )

    return findings


def summarise(findings: list[Finding]) -> dict[str, Any]:
    """The `data_quality` column's shape.

    Always a dict with a `findings` list, even when empty: a column holding `[]`
    and one holding `{"findings": []}` read differently to a query, and the
    second says "checked" while the first could be either.
    """
    return {
        "checked": True,
        "findings": [finding.as_dict() for finding in findings],
        "errors": sum(1 for finding in findings if finding.severity == "error"),
        "warnings": sum(1 for finding in findings if finding.severity == "warning"),
        "note": (
            "detected and flagged, never corrected. A completed trade is a historical "
            "fact; a silently repaired one looks clean and is wrong."
        ),
    }


__all__ = ["Finding", "check", "summarise"]
