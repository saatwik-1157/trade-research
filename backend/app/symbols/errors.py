"""Symbol resolution failures.

Every one of these is raised rather than swallowed. A symbol that cannot be
resolved is not a symbol to guess at: the whole point of this layer is that
"EURUSD" on TradingView and "EURUSD" at the broker are two strings that
happen to look alike, and the moment one of them is missing the pipeline has
to stop rather than proceed on the other.
"""

from __future__ import annotations


class SymbolError(Exception):
    """Base class. Carries an HTTP status for the API layer to reuse."""

    status_code = 400


class InvalidSymbolCode(SymbolError):
    status_code = 422


class UnknownSourceSymbol(SymbolError):
    """The provider sent a symbol with no mapping. Never resolve by guessing."""

    status_code = 404


class AmbiguousSourceSymbol(SymbolError):
    """A bare ticker matches more than one exchange-qualified mapping.

    Picking one would route an order to whichever instrument happened to sort
    first, so the caller is told to qualify the symbol instead.
    """

    status_code = 409


class UnknownSymbol(SymbolError):
    status_code = 404


class NoProviderMapping(SymbolError):
    """The internal symbol exists but is not listed at the target provider."""

    status_code = 404


class DuplicateMapping(SymbolError):
    status_code = 409


class SymbolInactive(SymbolError):
    status_code = 409


class IncompleteContractSpec(SymbolError):
    """A spec field a caller needs is missing.

    Refusing is the point. `tools/mt5_paper.lot_for_risk` already refuses to
    size a position from a missing tick value, because a lot computed from a
    default is a real order for the wrong amount.
    """

    status_code = 409
