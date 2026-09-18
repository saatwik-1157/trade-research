"""Response models for the v1 surface.

Shapes, not tables. A response model is what a caller may rely on; the ORM row
is what happens to be stored. Keeping them apart is what lets L19 add columns
to `orders` without changing what a client sees.

Three conventions:

  * Decimals stay Decimal. A price serialised through float is a price the
    caller cannot compare to the one the broker sent.
  * `symbol` is the internal code, never the raw id, and never the broker's
    name for it. The three names are separate on purpose (see
    `app.symbols.service`).
  * A field that is genuinely unknown is `null` and stays `null`. Nothing here
    substitutes a zero for an absent figure -- an unrealised P&L of `null` and
    one of `0` are different claims.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

_ORM = ConfigDict(from_attributes=True)


class OrderOut(BaseModel):
    model_config = _ORM

    id: str
    intent_id: str
    mode: str
    # Filled from the page's one symbol lookup after validation, so it
    # defaults to None rather than being required off the ORM row.
    symbol: str | None = None
    side: str
    order_type: str
    quantity: Decimal
    requested_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    status: str
    broker_order_id: str | None
    magic: int | None
    source: str
    signal_id: str | None
    strategy_version_id: str | None
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OrderEventOut(BaseModel):
    model_config = _ORM

    id: str
    order_id: str
    event_type: str
    occurred_at: datetime
    retcode: int | None
    comment: str | None


class ExecutionOut(BaseModel):
    model_config = _ORM

    id: str
    order_id: str
    broker_deal_id: str | None
    executed_at: datetime
    price: Decimal
    quantity: Decimal
    commission: Decimal | None
    swap: Decimal | None
    slippage_points: Decimal | None
    # 'simulator' or 'broker'. The label travels with the row so a simulated
    # fill can never be pooled with a broker fill by accident.
    fill_source: str


class PositionOut(BaseModel):
    model_config = _ORM

    id: str
    mode: str
    # Filled from the page's one symbol lookup after validation, so it
    # defaults to None rather than being required off the ORM row.
    symbol: str | None = None
    side: str
    # What is OPEN now, beside what it opened at and what has gone. Three
    # fields because a position cut down by a scale-out is not the position
    # that was risk-sized, and one number cannot say that.
    quantity: Decimal
    initial_quantity: Decimal
    closed_quantity: Decimal
    entry_price: Decimal
    # What this platform INTENDS.
    stop_loss: Decimal | None
    take_profit: Decimal | None
    # What the VENUE last reported, and when it was read. Separate from the
    # intended levels on purpose: a stop we asked for and a stop the venue is
    # holding are different facts, and `broker_synced_at` of None means never
    # read rather than agreed.
    broker_stop_loss: Decimal | None = None
    broker_take_profit: Decimal | None = None
    broker_synced_at: datetime | None = None
    realized_pnl: Decimal | None = None
    status: str
    broker_position_id: str | None
    source: str
    opened_at: datetime
    closed_at: datetime | None


class TradeOut(BaseModel):
    """A closed round trip. `r_multiple` is the figure to read, not net_profit:
    net currency cannot be pooled across trades sized by different stop
    distances."""

    model_config = _ORM

    id: str
    mode: str
    # Filled from the page's one symbol lookup after validation, so it
    # defaults to None rather than being required off the ORM row.
    symbol: str | None = None
    side: str
    volume: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_profit: Decimal
    commission: Decimal
    swap: Decimal
    net_profit: Decimal
    r_multiple: Decimal | None
    close_reason: int | None
    source: str
    broker_position_id: str | None
    # L31. Attribution and lifecycle. Every one is nullable: the 252 imported
    # rows carry none of it, and rendering a null as a blank cell is the honest
    # shape -- a dash means nobody recorded it, not that it was nothing.
    status: str = "closed"
    exit_reason: str | None = None
    strategy_version_id: str | None = None
    bot_id: str | None = None
    position_id: str | None = None
    order_id: str | None = None
    signal_id: str | None = None
    ai_decision_id: str | None = None
    risk_event_id: str | None = None
    fees: Decimal | None = None
    currency: str | None = None
    requested_entry_price: Decimal | None = None
    entry_slippage_points: Decimal | None = None
    data_quality: dict | None = None
    bracket: dict | None


class SymbolOut(BaseModel):
    model_config = _ORM

    id: str
    code: str
    asset_class: str
    base_currency: str | None
    quote_currency: str | None
    digits: int | None
    point_size: Decimal | None
    # 'points' or 'percent'. Pooling points across symbols whose point sizes
    # differ by 59x is the metals error this project keeps finding.
    unit_class: str
    is_active: bool


class MappingOut(BaseModel):
    model_config = _ORM

    id: str
    provider: str
    provider_symbol: str
    is_active: bool
    spec_source: str | None
    spec_updated_at: datetime | None
    # Computed from the mapping's spec columns after validation, so it needs
    # a default. Without one `model_validate` fails on the ORM row before
    # `model_copy` can fill it in -- which is exactly what broke
    # GET /v1/symbols/{code}/mappings between L06 and L11, unnoticed because
    # no test covered that route. False is the safe default: a mapping is not
    # assumed to carry a usable spec.
    has_complete_spec: bool = False


class ContractSpecOut(BaseModel):
    """A broker's contract terms. Every field is measured; a spec missing any
    required field is refused rather than served with defaults."""

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


class BrokerAccountOut(BaseModel):
    model_config = _ORM

    id: str
    name: str
    broker: str
    account_mode: str
    server: str | None
    currency: str | None
    is_active: bool
    created_at: datetime
    # `login` is deliberately absent. An account number is a credential-shaped
    # identifier and nothing in the UI needs it.


class PaperAccountOut(BaseModel):
    model_config = _ORM

    id: str
    name: str
    currency: str
    starting_balance: Decimal
    balance: Decimal
    equity: Decimal
    is_active: bool
    created_at: datetime


class AuditLogOut(BaseModel):
    model_config = _ORM

    id: str
    actor_user_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    occurred_at: datetime
    ip: str | None
    request_id: str | None
    details: dict | None


class WebhookEventOut(BaseModel):
    """One recorded alert, metadata only.

    The payload is deliberately absent: a 200-row page carrying bodies would
    spill far more than the question "did our alert arrive" needs. Read one
    event for it.
    """

    model_config = _ORM

    id: str
    provider: str
    received_at: datetime
    status: str
    auth_strength: str
    source_ip: str | None
    signal_id: str | None
    error: str | None
    # For a rejection this is `tv:rej:<logical key>:<timestamp>`, because a
    # rejection stored under the alert's own key would occupy the UNIQUE index
    # and block the corrected resend. It is the only thing tying a rejection
    # back to the alert it came from, so it is served as text and nothing
    # joins on it.
    idempotency_key: str


class WebhookEventDetailOut(WebhookEventOut):
    """The same row, with the payload exactly as it is stored.

    It was redacted on write (`app/webhooks/gateway.py`), by the only code
    that ever holds the secret. Nothing here re-redacts, for the reason
    `AuditLogOut` does not: scrubbing on read would require handing the read
    path the live secret, which is strictly worse than not having it.
    """

    payload: dict | None


class TradingSafetyOut(BaseModel):
    """What the platform is allowed to do, and why it is not allowed to do more.

    `live_execution_blockers` is the honest field: it lists every gate that is
    not built, so "live is off" is a derived statement with its reasons
    attached rather than a flag someone could flip.
    """

    trading_mode: str
    live_trading: bool
    live_execution_allowed: bool
    live_execution_blockers: list[str]
    gates: dict[str, bool]


class ApiVersionOut(BaseModel):
    service: str
    version: str
    api_version: str
    environment: str
    documented_at: str
