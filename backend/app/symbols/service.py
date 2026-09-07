"""Symbol mapping: TradingView -> internal -> MT5 broker symbol.

The three names are separate on purpose:

  source_symbol    what a provider sends, e.g. "OANDA:EURUSD", "GER40"
  internal_symbol  this platform's canonical code, e.g. "EURUSD", "DE40"
  broker_symbol    what the broker lists, e.g. "EURUSD.r", "GER40.cash"

None of the three can be derived from another. Brokers add suffixes for
account type, TradingView prefixes an exchange, and index naming differs
outright ("DE40" against "GER40" against "DAX"). Anything that assumes they
match is a bug waiting for the first broker that disagrees, so resolution is
always a table lookup and a miss is an error, never a fallback to the input
string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import utcnow
from app.models.market import Symbol, SymbolMapping
from app.symbols.errors import (
    AmbiguousSourceSymbol,
    DuplicateMapping,
    IncompleteContractSpec,
    InvalidSymbolCode,
    NoProviderMapping,
    SymbolInactive,
    UnknownSourceSymbol,
    UnknownSymbol,
)

PROVIDERS = ("mt5", "tradingview", "ccxt", "yfinance", "simulator")
ASSET_CLASSES = ("fx", "index", "metal", "crypto", "equity", "other")
BROKER_PROVIDER = "mt5"

MAX_CODE = 32
MAX_PROVIDER_SYMBOL = 64


def normalise_code(code: str) -> str:
    """Canonical internal code: trimmed, upper case, no separators.

    Raises rather than returning a best effort. An empty or oversized code is
    a caller bug and silently normalising it away hides where it came from.
    """
    if not isinstance(code, str):
        raise InvalidSymbolCode("symbol code must be a string")
    cleaned = code.strip().upper()
    if not cleaned:
        raise InvalidSymbolCode("symbol code is empty")
    if len(cleaned) > MAX_CODE:
        raise InvalidSymbolCode(f"symbol code longer than {MAX_CODE} characters: {cleaned[:40]!r}")
    if any(c.isspace() for c in cleaned):
        raise InvalidSymbolCode(f"symbol code contains whitespace: {code!r}")
    return cleaned


def normalise_provider_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidSymbolCode("provider symbol must be a string")
    cleaned = value.strip().upper()
    if not cleaned:
        raise InvalidSymbolCode("provider symbol is empty")
    if len(cleaned) > MAX_PROVIDER_SYMBOL:
        raise InvalidSymbolCode(f"provider symbol longer than {MAX_PROVIDER_SYMBOL} characters")
    return cleaned


def strip_exchange_prefix(value: str) -> str:
    """ "OANDA:EURUSD" -> "EURUSD".

    TradingView's {{ticker}} may or may not carry an exchange prefix
    depending on how the alert was written, so both forms have to resolve.
    The prefixed form is tried first: a mapping stored with its exchange is
    more specific and must win.
    """
    return value.split(":", 1)[1] if ":" in value else value


def check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise InvalidSymbolCode(f"unknown provider {provider!r}; expected one of {PROVIDERS}")
    return provider


@dataclass(frozen=True)
class ContractSpec:
    """A broker's contract terms for one symbol. Every field is measured."""

    internal_symbol: str
    broker_symbol: str
    provider: str
    contract_size: Decimal
    tick_size: Decimal
    tick_value: Decimal
    minimum_volume: Decimal
    maximum_volume: Decimal
    volume_step: Decimal
    price_precision: int
    volume_precision: int
    trading_hours: dict | None
    spec_source: str
    spec_updated_at: datetime | None


# Fields a spec must carry before anything may size or price an order with it.
REQUIRED_SPEC_FIELDS = (
    "contract_size",
    "tick_size",
    "tick_value",
    "minimum_volume",
    "maximum_volume",
    "volume_step",
    "price_precision",
    "volume_precision",
)


async def get_symbol(db: AsyncSession, internal_symbol: str) -> Symbol:
    code = normalise_code(internal_symbol)
    symbol = await db.scalar(select(Symbol).where(Symbol.code == code))
    if symbol is None:
        raise UnknownSymbol(f"no internal symbol {code!r}")
    return symbol


async def resolve_source(
    db: AsyncSession, provider: str, source_symbol: str, *, allow_inactive: bool = False
) -> Symbol:
    """A provider's symbol -> the internal symbol. Never falls back to the input."""
    check_provider(provider)
    raw = normalise_provider_symbol(source_symbol)

    async def finish(mapping: SymbolMapping, shown: str) -> Symbol:
        if not mapping.is_active and not allow_inactive:
            raise SymbolInactive(f"mapping {provider}:{shown} is inactive")
        symbol = await db.get(Symbol, mapping.symbol_id)
        if symbol is None:  # pragma: no cover - the foreign key prevents this
            raise UnknownSymbol(f"mapping {provider}:{shown} points at a missing symbol")
        if not symbol.is_active and not allow_inactive:
            raise SymbolInactive(f"symbol {symbol.code} is inactive")
        return symbol

    # 1. Exact match. A mapping stored the way the provider sends it wins
    #    outright, including when it carries an exchange prefix.
    for candidate in dict.fromkeys([raw, strip_exchange_prefix(raw)]):
        exact = await db.scalar(
            select(SymbolMapping).where(
                SymbolMapping.provider == provider,
                SymbolMapping.provider_symbol == candidate,
            )
        )
        if exact is not None:
            return await finish(exact, candidate)

    # 2. A bare ticker against exchange-qualified mappings. TradingView's
    #    {{ticker}} arrives with or without its exchange depending on how the
    #    alert was written, so "EURUSD" has to reach "OANDA:EURUSD". This is
    #    allowed only when exactly one instrument matches: two exchanges can
    #    list the same ticker for different instruments, and guessing between
    #    them would route an order to the wrong one.
    if ":" not in raw:
        qualified = (
            await db.scalars(
                select(SymbolMapping).where(
                    SymbolMapping.provider == provider,
                    SymbolMapping.provider_symbol.like(f"%:{raw}"),
                )
            )
        ).all()
        distinct = {m.symbol_id: m for m in qualified}
        if len(distinct) == 1:
            mapping = next(iter(distinct.values()))
            return await finish(mapping, mapping.provider_symbol)
        if len(distinct) > 1:
            names = sorted(m.provider_symbol for m in qualified)
            raise AmbiguousSourceSymbol(
                f"{provider} ticker {raw!r} matches {len(distinct)} instruments "
                f"({', '.join(names)}); send the exchange-qualified symbol"
            )

    raise UnknownSourceSymbol(
        f"{provider} symbol {raw!r} has no mapping; "
        "add one rather than assuming it matches a broker symbol"
    )


async def mapping_for(
    db: AsyncSession, internal_symbol: str, provider: str, *, allow_inactive: bool = False
) -> SymbolMapping:
    """The internal symbol's row at one provider."""
    check_provider(provider)
    symbol = await get_symbol(db, internal_symbol)
    mapping = await db.scalar(
        select(SymbolMapping).where(
            SymbolMapping.symbol_id == symbol.id, SymbolMapping.provider == provider
        )
    )
    if mapping is None:
        raise NoProviderMapping(f"{symbol.code} is not mapped to {provider}")
    if not mapping.is_active and not allow_inactive:
        raise SymbolInactive(f"mapping {provider}:{mapping.provider_symbol} is inactive")
    return mapping


async def broker_symbol_for(
    db: AsyncSession, internal_symbol: str, provider: str = BROKER_PROVIDER
) -> str:
    return (await mapping_for(db, internal_symbol, provider)).provider_symbol


async def translate(
    db: AsyncSession, source_provider: str, source_symbol: str, target_provider: str
) -> str:
    """TradingView symbol -> broker symbol, through the internal code."""
    symbol = await resolve_source(db, source_provider, source_symbol)
    return await broker_symbol_for(db, symbol.code, target_provider)


async def contract_spec(
    db: AsyncSession, internal_symbol: str, provider: str = BROKER_PROVIDER
) -> ContractSpec:
    """The broker's contract terms, or a refusal naming what is missing."""
    mapping = await mapping_for(db, internal_symbol, provider)
    symbol = await get_symbol(db, internal_symbol)
    missing = [f for f in REQUIRED_SPEC_FIELDS if getattr(mapping, f) is None]
    if missing:
        raise IncompleteContractSpec(
            f"{symbol.code} at {provider} ({mapping.provider_symbol}) is missing "
            f"{', '.join(missing)}; sync the spec from the terminal rather than "
            "assuming a default"
        )
    # Past the guard above every required field is non-null. The columns are
    # nullable so an unsynced mapping can exist, not so a spec can be partial;
    # `values` re-reads them once the guard has established that.
    values = {field: getattr(mapping, field) for field in REQUIRED_SPEC_FIELDS}
    return ContractSpec(
        internal_symbol=symbol.code,
        broker_symbol=mapping.provider_symbol,
        provider=provider,
        contract_size=values["contract_size"],
        tick_size=values["tick_size"],
        tick_value=values["tick_value"],
        minimum_volume=values["minimum_volume"],
        maximum_volume=values["maximum_volume"],
        volume_step=values["volume_step"],
        price_precision=values["price_precision"],
        volume_precision=values["volume_precision"],
        trading_hours=mapping.trading_hours,
        spec_source=mapping.spec_source or "unknown",
        spec_updated_at=mapping.spec_updated_at,
    )


async def upsert_symbol(
    db: AsyncSession,
    internal_symbol: str,
    asset_class: str,
    *,
    base_currency: str | None = None,
    quote_currency: str | None = None,
    unit_class: str = "points",
    digits: int | None = None,
    point_size: Decimal | None = None,
) -> Symbol:
    code = normalise_code(internal_symbol)
    if asset_class not in ASSET_CLASSES:
        raise InvalidSymbolCode(f"unknown asset class {asset_class!r}; expected {ASSET_CLASSES}")
    if unit_class not in ("points", "percent"):
        raise InvalidSymbolCode(f"unknown unit class {unit_class!r}")
    symbol = await db.scalar(select(Symbol).where(Symbol.code == code))
    if symbol is None:
        symbol = Symbol(code=code, asset_class=asset_class, unit_class=unit_class)
        db.add(symbol)
    symbol.asset_class = asset_class
    symbol.unit_class = unit_class
    if base_currency is not None:
        symbol.base_currency = base_currency
    if quote_currency is not None:
        symbol.quote_currency = quote_currency
    if digits is not None:
        symbol.digits = digits
    if point_size is not None:
        symbol.point_size = point_size
    await db.flush()
    return symbol


async def upsert_mapping(
    db: AsyncSession,
    internal_symbol: str,
    provider: str,
    provider_symbol: str,
    **spec: object,
) -> SymbolMapping:
    """Create or update one provider mapping.

    A provider symbol already pointing at a different internal symbol is a
    conflict, not an update: silently repointing it would send orders for one
    instrument to another.
    """
    check_provider(provider)
    symbol = await get_symbol(db, internal_symbol)
    name = normalise_provider_symbol(provider_symbol)

    clash = await db.scalar(
        select(SymbolMapping).where(
            SymbolMapping.provider == provider, SymbolMapping.provider_symbol == name
        )
    )
    if clash is not None and clash.symbol_id != symbol.id:
        other = await db.get(Symbol, clash.symbol_id)
        raise DuplicateMapping(
            f"{provider} symbol {name!r} is already mapped to "
            f"{other.code if other else clash.symbol_id}"
        )

    mapping = clash or await db.scalar(
        select(SymbolMapping).where(
            SymbolMapping.symbol_id == symbol.id, SymbolMapping.provider == provider
        )
    )
    if mapping is None:
        mapping = SymbolMapping(symbol_id=symbol.id, provider=provider, provider_symbol=name)
        db.add(mapping)
    mapping.provider_symbol = name
    for key, value in spec.items():
        if not hasattr(mapping, key):
            raise InvalidSymbolCode(f"unknown mapping field {key!r}")
        setattr(mapping, key, value)
    if spec:
        mapping.spec_updated_at = utcnow()
    await db.flush()
    return mapping
