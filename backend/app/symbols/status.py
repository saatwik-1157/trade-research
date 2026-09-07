"""Symbol status, and the single gate a symbol passes before it may be traded.

Two things live here, and they answer different questions.

`status_of` answers **"what is the state of this symbol right now?"** without
raising, so a UI can render a list of instruments where some are broken. The
rule that shapes it is Step 12's: **UNKNOWN is never reported as ACTIVE.** A
symbol whose mapping could not be resolved, or whose spec has never been
synced, is not "fine but unconfigured" -- it is a symbol nothing should size an
order from, and it says so.

`validate_for_trading` answers **"may this symbol enter a trading flow?"** and
is the one gate that checks every condition at once: mapping exists, internal
symbol exists, broker symbol exists, both are active, and the contract spec is
complete. It exists so that L18 and L19 do not each assemble their own subset
of those checks and disagree about which ones matter.

Neither function authorizes a trade. Passing this gate means the *symbol* is
usable; whether the *order* may be sent is Risk's decision, and nothing here
shortens that path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.symbols import service
from app.symbols.errors import (
    AmbiguousSourceSymbol,
    IncompleteContractSpec,
    InvalidSymbolCode,
    NoProviderMapping,
    SymbolError,
    SymbolInactive,
    UnknownSourceSymbol,
    UnknownSymbol,
)


class SymbolStatus(StrEnum):
    """Where a symbol stands. Ordered from usable to unusable."""

    active = "active"  # resolvable, active, and its spec is complete
    # Everything below this line is a refusal, not a warning.
    inactive = "inactive"  # deliberately switched off by an operator
    not_found = "not_found"  # no such internal symbol
    not_tradable = "not_tradable"  # resolvable but the spec is missing fields
    mapping_error = "mapping_error"  # ambiguous, absent or conflicting mapping
    unknown = "unknown"  # could not be determined; never read as active

    @property
    def tradable(self) -> bool:
        return self is SymbolStatus.active


# Which refusal each `SymbolError` maps to. Explicit rather than derived from
# the class name, so adding an error forces a decision about what it means for
# tradability instead of defaulting to something benign.
_STATUS_FOR: dict[type[SymbolError], SymbolStatus] = {
    UnknownSymbol: SymbolStatus.not_found,
    UnknownSourceSymbol: SymbolStatus.mapping_error,
    AmbiguousSourceSymbol: SymbolStatus.mapping_error,
    NoProviderMapping: SymbolStatus.mapping_error,
    SymbolInactive: SymbolStatus.inactive,
    IncompleteContractSpec: SymbolStatus.not_tradable,
    InvalidSymbolCode: SymbolStatus.mapping_error,
}


def status_for_error(exc: SymbolError) -> SymbolStatus:
    """The status an error implies. Unrecognised errors are UNKNOWN, not active."""
    return _STATUS_FOR.get(type(exc), SymbolStatus.unknown)


@dataclass(frozen=True)
class SymbolReport:
    """The full picture for one symbol at one provider."""

    internal_symbol: str
    provider: str
    status: SymbolStatus
    detail: str
    broker_symbol: str | None = None
    spec_complete: bool = False
    missing_spec_fields: tuple[str, ...] = ()

    @property
    def tradable(self) -> bool:
        return self.status.tradable

    def as_dict(self) -> dict[str, object]:
        return {
            "internal_symbol": self.internal_symbol,
            "provider": self.provider,
            "status": str(self.status),
            "tradable": self.tradable,
            "detail": self.detail,
            "broker_symbol": self.broker_symbol,
            "spec_complete": self.spec_complete,
            "missing_spec_fields": list(self.missing_spec_fields),
        }


async def status_of(
    db: AsyncSession, internal_symbol: str, provider: str = service.BROKER_PROVIDER
) -> SymbolReport:
    """Describe a symbol without raising.

    Every failure becomes a named status rather than an exception, so a caller
    listing instruments gets a row for each one. What it never does is report a
    symbol it could not resolve as ACTIVE.
    """
    try:
        mapping = await service.mapping_for(db, internal_symbol, provider)
    except SymbolError as exc:
        return SymbolReport(
            internal_symbol=internal_symbol,
            provider=provider,
            status=status_for_error(exc),
            detail=str(exc),
        )

    missing = tuple(
        field for field in service.REQUIRED_SPEC_FIELDS if getattr(mapping, field) is None
    )
    if missing:
        return SymbolReport(
            internal_symbol=internal_symbol,
            provider=provider,
            status=SymbolStatus.not_tradable,
            detail=(
                f"resolvable but the contract spec is incomplete; missing "
                f"{', '.join(missing)}. Sync it from the terminal rather than "
                "assuming a default"
            ),
            broker_symbol=mapping.provider_symbol,
            spec_complete=False,
            missing_spec_fields=missing,
        )

    return SymbolReport(
        internal_symbol=internal_symbol,
        provider=provider,
        status=SymbolStatus.active,
        detail="mapped, active, and the contract spec is complete",
        broker_symbol=mapping.provider_symbol,
        spec_complete=True,
    )


class NotTradable(SymbolError):
    """This symbol may not enter a trading flow. Carries why."""

    status_code = 409


async def validate_for_trading(
    db: AsyncSession, internal_symbol: str, provider: str = service.BROKER_PROVIDER
) -> service.ContractSpec:
    """The one gate a symbol passes before an order may be built for it.

    Checks, in order: the internal symbol exists, it is active, it is mapped at
    this provider, that mapping is active, and its contract spec is complete.
    Returns the spec, because every caller that needs the gate also needs the
    numbers behind it and a second lookup would be a second chance to disagree.

    **Passing is not authorization.** It says the symbol is usable, not that
    the trade may happen. Risk decides that, and nothing here shortens the path
    Signal -> Strategy -> AI -> Risk -> Sizing -> OMS -> BrokerAdapter.
    """
    report = await status_of(db, internal_symbol, provider)
    if not report.tradable:
        raise NotTradable(f"{internal_symbol} at {provider} is {report.status}: {report.detail}")
    # Past the gate the spec must exist; `contract_spec` re-reads it and raises
    # its own precise error if anything changed underneath.
    return await service.contract_spec(db, internal_symbol, provider)
