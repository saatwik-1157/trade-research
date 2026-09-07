"""Broker-constraint validation, run before anything is sent to a venue.

The rule this module exists to enforce: **never silently round a value in a
way that changes intended risk.**

That is not a style preference. `mt5_paper.lot_for_risk` sizes a position so
its stop costs a fixed sum; if this layer quietly rounded 0.037 up to 0.04
because the step is 0.01, the trade would risk 8% more than the caller
budgeted and nothing in the record would say so. Rounding *down* is safe
because it can only risk less than intended, and that is what the sizing
module already does. Rounding up here would silently undo it.

So a volume that is not already a multiple of the step is **refused**, with a
message naming the nearest valid value below it. The caller decides; this
layer does not decide for them.

The specs come from the venue, per symbol. They are not universal: DE40 has
contract size 1 and minimum volume 0.1 where the FX pairs have 100,000 and
0.01, both measured from this broker's terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.brokers.base import OrderRequest, SymbolInfo
from app.symbols.precision import floor_to_step, is_step_multiple

SIDES = ("buy", "sell")


class OrderRejected(Exception):
    """The request cannot be sent. Refused here, before the venue sees it."""


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    problems: list[str]

    def raise_if_bad(self) -> None:
        if not self.ok:
            raise OrderRejected("; ".join(self.problems))


# One implementation, and it lives in the symbol layer. This module kept its
# own copy until L11; two functions that round a lot size differently is how
# the same signal sends two different volumes depending on which path reached
# the venue.
#
# The imported versions also carry the float-floor guard `mt5_paper` already
# paid for: a step count arriving as 4.999999999 is five steps, and a bare
# floor turns it into four with nothing in the record to say which was meant.
# The old local `%` check did not have that guard.
_step_multiple = is_step_multiple


def validate_order(
    request: OrderRequest, info: SymbolInfo | None, quote_bid: Decimal | None = None
) -> ValidationReport:
    """Check a request against the venue's own specification for the symbol.

    A missing `SymbolInfo` is itself a refusal. Sending an order for a symbol
    the venue does not list, or whose terms we could not read, is how an order
    goes out for the wrong amount — the same reasoning that makes
    `lot_for_risk` refuse rather than default.
    """
    problems: list[str] = []

    if info is None:
        return ValidationReport(
            False,
            [
                f"no symbol specification for {request.symbol!r}; the venue may not list "
                "it. Refusing rather than sending an order priced from a guess."
            ],
        )

    if request.side not in SIDES:
        problems.append(f"side must be one of {', '.join(SIDES)}, not {request.side!r}")

    volume = request.volume
    if volume <= 0:
        problems.append(f"volume must be positive, not {volume}")
    else:
        if info.volume_min is not None and volume < info.volume_min:
            problems.append(
                f"volume {volume} is below the venue minimum {info.volume_min} for {info.symbol}"
            )
        if info.volume_max is not None and volume > info.volume_max:
            problems.append(
                f"volume {volume} is above the venue maximum {info.volume_max} for {info.symbol}"
            )
        if info.volume_step is not None and not _step_multiple(volume, info.volume_step):
            nearest = floor_to_step(volume, info.volume_step)
            problems.append(
                f"volume {volume} is not a multiple of the step {info.volume_step}; the "
                f"nearest valid volume at or below it is {nearest}. Refusing rather than "
                "rounding, because rounding up would risk more than was budgeted"
            )

    for name, level in (("stop_loss", request.stop_loss), ("take_profit", request.take_profit)):
        if level is not None and level <= 0:
            problems.append(f"{name} must be a positive price level, not {level}")

    # Bracket geometry. Checked against the *quote* only as a sanity gate; the
    # authoritative check is against the FILL and lives in
    # `mt5_paper.bracket_is_sane`, because a bracket that straddles the quote
    # can still sit the wrong side of where the order actually filled. That
    # distinction cost this project a real trade: NZDUSD 10200315596 filled 279
    # points away from its quote and every exit branch was a loss.
    if quote_bid is not None and request.stop_loss and request.take_profit:
        if request.side == "buy" and not (request.stop_loss < quote_bid < request.take_profit):
            problems.append(
                f"buy bracket does not straddle the quote {quote_bid}: "
                f"sl={request.stop_loss} tp={request.take_profit}"
            )
        if request.side == "sell" and not (request.take_profit < quote_bid < request.stop_loss):
            problems.append(
                f"sell bracket does not straddle the quote {quote_bid}: "
                f"sl={request.stop_loss} tp={request.take_profit}"
            )

    return ValidationReport(not problems, problems)
