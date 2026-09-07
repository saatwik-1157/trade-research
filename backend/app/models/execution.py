"""orders, order_events, executions, positions, position_events, trades.

The order row is the OMS state machine; every transition is an order_event.
`intent_id` is the idempotency key: the same intent can never produce two
orders. `status = 'unknown'` exists on purpose - it is what an IPC timeout
after `order_send` looks like, and the only legal exit from it is
reconciliation, never a retry.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import MODE_CHECK, IdMixin, JSONType, Money, Price, Qty, Ratio, TimestampMixin

# The twelve order states. Eight predate L19; `submitting`, `cancel_requested`,
# `expired` and `failed` were added there, each because a real outcome had
# nowhere to go. `app/oms/state.py` is the machine over them and asserts at
# import that it can reach nothing this tuple lacks.
#
# `failed` and `rejected` are deliberately NOT synonyms: `failed` means the
# request never reached the venue and a fresh order is safe, `rejected` means
# the venue refused. Collapsing them loses the distinction that decides
# whether a re-send is safe.
ORDER_STATUSES = (
    "intent",
    "submitting",
    "submitted",
    "accepted",
    "partially_filled",
    "filled",
    "cancel_requested",
    "cancelled",
    "rejected",
    "expired",
    "failed",
    "unknown",
)

# Time-in-force values the platform accepts. MT5's market orders are filled or
# killed at the venue, so GTC is the meaningful default for the pending order
# types and IOC/FOK are carried through rather than reinterpreted.
TIME_IN_FORCE = ("gtc", "ioc", "fok", "day")
SOURCE_CHECK = "source IN ('pipeline','jsonl_import','manual','simulator')"

# The position lifecycle. Three predate L21; four were added there because a
# real state had nowhere to go:
#
#   * `opening`     -- an order is in flight and no fill has confirmed. A
#                      position that exists locally before the venue confirms
#                      is a position that may not exist at all.
#   * `partially_closed` -- some of it was taken off. Distinct from `open`
#                      because the remaining size is no longer what was sized.
#   * `closing`     -- a close was requested and the venue has not confirmed.
#                      "We asked" and "it is closed" are different facts.
#   * `reconciling` -- being settled against the venue. Distinct from
#                      `unknown`: `unknown` is "nobody is looking", this is
#                      "somebody is looking right now", and only the second
#                      means an answer is coming.
POSITION_STATUSES = (
    "opening",
    "open",
    "partially_closed",
    "closing",
    "closed",
    "unknown",
    "reconciling",
)


class Order(IdMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(MODE_CHECK, name="mode"),
        CheckConstraint("side IN ('buy','sell')", name="side"),
        CheckConstraint("order_type IN ('market','limit','stop')", name="order_type"),
        CheckConstraint("status IN ('" + "','".join(ORDER_STATUSES) + "')", name="status"),
        CheckConstraint(SOURCE_CHECK, name="source"),
        CheckConstraint("time_in_force IN ('" + "','".join(TIME_IN_FORCE) + "')", name="tif"),
        # The venue cannot have filled more than was asked for. A database
        # that permits it is a database where an overfill becomes history.
        CheckConstraint("filled_quantity >= 0 AND filled_quantity <= quantity", name="filled_qty"),
    )

    intent_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    broker_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    paper_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="SET NULL"), nullable=True
    )
    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False, default="market")
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    requested_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="intent", index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    magic: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sizing: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="pipeline")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---------------------------------------------------------------- L19
    # What the venue has actually done. `filled_quantity` moves only because
    # an execution was recorded; `remaining` is derived and never stored,
    # because two fields that must agree eventually disagree.
    filled_quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False, default=Decimal("0"))
    # Quantity-weighted, recomputed from the executions rather than updated in
    # place, so a restart that reloads them gets the same number.
    average_fill_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    # The approval that authorised this order. Nullable because the imported
    # rows predate the Risk Engine and a backfilled id would be an invented
    # reference to a decision nobody made.
    risk_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    time_in_force: Mapped[str] = mapped_column(String(4), nullable=False, default="gtc")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Why the venue refused, in its own words, and its own code.
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)


class OrderEvent(IdMixin, Base):
    """One entry in an order's audit trail. Append-only; nothing updates a row.

    L19 lifted `previous_status`, `new_status`, `source` and `broker_order_id`
    out of the free-text comment. They were reconstructible from the sequence
    before, which is not the same as recorded: a reader had to assume no event
    was missing to know what a transition was from.
    """

    __tablename__ = "order_events"

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # Position in this order's audit trail, from 0. `occurred_at` cannot order
    # it: a submit that completes inside one clock reading produces four
    # transitions with identical timestamps, and an audit trail whose rows
    # cannot be put in order is not an audit trail. Nullable for the rows that
    # predate L19, whose order is genuinely not recorded.
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Who caused it: 'pipeline', 'operator', 'reconciliation', 'broker',
    # 'recovery'. A state change with no author cannot be audited.
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Execution(IdMixin, Base):
    """A fill as the venue reported it. Never the quote we sent."""

    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint("fill_source IN ('broker','simulator','import')", name="fill_source"),
    )

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    broker_deal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    commission: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    swap: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    slippage_points: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    fill_source: Mapped[str] = mapped_column(String(16), nullable=False)


class Position(IdMixin, TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint(MODE_CHECK, name="mode"),
        CheckConstraint("side IN ('long','short')", name="side"),
        CheckConstraint("status IN ('" + "','".join(POSITION_STATUSES) + "')", name="status"),
        CheckConstraint(SOURCE_CHECK, name="source"),
        # A position cannot have closed more than it opened. A database that
        # permits it is one where an over-close becomes history.
        CheckConstraint(
            "closed_quantity >= 0 AND closed_quantity <= initial_quantity",
            name="closed_qty",
        ),
    )

    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    broker_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    paper_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="SET NULL"), nullable=True
    )
    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    broker_position_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    # String(24), not String(8). L21 added `partially_closed` (16 chars) and
    # `reconciling` (11) to a column sized for `open`/`closed`/`unknown`.
    # SQLite ignores VARCHAR length so every test passed; PostgreSQL enforces
    # it, so a position reaching either state would have failed in production.
    # Found by running the suite against a real database for the first time.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="pipeline")

    # ---------------------------------------------------------------- L21
    # `quantity` is what is OPEN now. These two are the history it was cut
    # from, and they are separate columns because a partial close must not
    # overwrite what the position originally was -- "70 open" and "100 opened,
    # 30 closed" are different facts, and only the second can be audited.
    initial_quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False, default=Decimal("0"))
    closed_quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False, default=Decimal("0"))
    # Money booked by the closes so far. Unrealised P&L is deliberately NOT a
    # column: it is a function of a price that changes every tick, and a
    # stored one is a number that is wrong the moment it is written.
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # Where the venue's own protective levels were last read to be. Separate
    # from `stop_loss`/`take_profit`, which are what this platform INTENDS:
    # a stop we asked for and a stop the venue is holding are different facts,
    # and a silent disagreement between them is the thing L21 must surface.
    broker_stop_loss: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    broker_take_profit: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    broker_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PositionEvent(Base):
    """Append-only audit log for one position.

    The primary key is an autoincrementing integer rather than the UUID the
    other tables use, and that is deliberate. `occurred_at` is domain time,
    and several events in one pass legitimately share it - an exit decision
    and its fill are the same instant - so it cannot order the log. A UUID
    orders nothing at all. An audit log has to be replayable in the order it
    was written, so the key provides that order.
    """

    __tablename__ = "position_events"

    # BigInteger on PostgreSQL; Integer on SQLite, which only autoincrements a
    # column declared exactly INTEGER PRIMARY KEY.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    position_id: Mapped[str] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


#: The journal's own states. Section 15 of L31.
#:
#: Only what is REACHABLE, which is the rule L28's lifecycle established. There
#: is no `cancelled` or `rejected` here: a journal row is written when a
#: POSITION closes, and a cancelled order never opened one -- it lives in
#: `orders.status`, where it already has both words. Recording a rejection as a
#: trade is precisely the confusion section 25 warns against.
TRADE_STATUSES = ("open", "partially_closed", "closed", "reconciliation_required", "unknown")
TRADE_STATUS_CHECK = "status IN ('" + "','".join(TRADE_STATUSES) + "')"


class Trade(IdMixin, Base):
    """A closed round trip. The journal's unit of account; R is the figure to read.

    **One row per POSITION EPISODE**, not per fill and not per order. L31
    section 4: an order is an instruction, a fill is what the venue did, a
    position is what is held, and a trade is the completed episode. A position
    that filled in two parts and closed in three is ONE trade, and the six
    events are its timeline.

    **Extended at L31, never replaced.** Every column below the original block
    is nullable and additive: the 252 imported rows keep meaning exactly what
    they meant, and a row this platform writes carries the attribution they
    cannot.
    """

    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint(MODE_CHECK, name="mode"),
        CheckConstraint("side IN ('long','short')", name="side"),
        CheckConstraint(SOURCE_CHECK, name="source"),
        CheckConstraint(TRADE_STATUS_CHECK, name="status"),
        # Section 27. One journal row per position episode, as a database
        # guarantee rather than a check the writer has to remember: a duplicate
        # fill or close event must not produce a second trade.
        #
        # NULLs are DISTINCT in a unique index, which L28 learned the hard way.
        # Here that is the WANTED behaviour rather than a trap -- the imported
        # rows carry no `position_id`, they are not position episodes, and a
        # constraint that collapsed them into one would destroy the record.
        Index(
            "uq_trades_position_id",
            "position_id",
            unique=True,
            postgresql_where=text("position_id IS NOT NULL"),
            sqlite_where=text("position_id IS NOT NULL"),
        ),
        Index("ix_trades_paper_account_closed", "paper_account_id", "closed_at"),
        Index("ix_trades_broker_account_closed", "broker_account_id", "closed_at"),
    )

    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    position_id: Mapped[str | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    broker_position_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    symbol_id: Mapped[str] = mapped_column(ForeignKey("symbols.id"), index=True)
    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    gross_profit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    commission: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    swap: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    net_profit: Mapped[Decimal] = mapped_column(Money, nullable=False)
    r_multiple: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    bracket: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    #: The BROKER's numeric close code, as the venue reported it. KEPT, and not
    #: merged into `exit_reason`: this is what the venue said and that is what
    #: the platform decided, and collapsing them would lose whichever half
    #: disagreed.
    close_reason: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="pipeline")
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---------------------------------------------------------------- L31
    #
    # Attribution. Every one is a REFERENCE to the row that already holds the
    # fact -- section 31: do not duplicate Order, Position, Execution or
    # AIInference. The journal links; the owning system keeps.

    #: Section 15. `closed` for a finished episode; `reconciliation_required`
    #: when the platform and the venue disagree (section 28), which is never
    #: resolved by guessing.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="closed")

    #: Section 30. Carried on the trade rather than reached through the
    #: position, because a position row can be deleted and the historical trade
    #: must still say which account it belonged to. Two columns, never one
    #: nullable `account_id`: the L05 decision that makes pooling paper and live
    #: a schema error rather than a forgotten filter.
    broker_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    paper_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="SET NULL"), nullable=True
    )

    #: Sections 6 and 12. The ENTRY order. Exits have their own orders and are
    #: reached through the position's events, because a trade with two order
    #: columns would still be wrong for a position closed in three parts.
    order_id: Mapped[str | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    #: Section 8. The signal that caused it -- which carries the TradingView
    #: event and the webhook row behind it, so no secret is copied here.
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    #: Section 9. The AI decision as it stood at execution, at an EXACT model
    #: version. The row it points at records the model, the probability, the
    #: regime and the verdict; section 9 forbids storing "latest model", and a
    #: reference to a recorded decision cannot degrade into one.
    ai_decision_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), nullable=True
    )
    #: Section 10. The risk decision, whose `snapshot` and
    #: `configuration_version` were written when the trade opened. Referenced
    #: rather than recomputed: section 10 forbids recalculating historical risk
    #: context with today's values.
    risk_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("risk_events.id", ondelete="SET NULL"), nullable=True
    )
    #: Section 6. The bot that ran it, where one did.
    bot_id: Mapped[str | None] = mapped_column(
        ForeignKey("bots.id", ondelete="SET NULL"), nullable=True
    )

    #: Section 17. L21's own vocabulary, not a second one.
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Sections 12 and 16. What the platform ASKED for against what it GOT.
    #: Stored rather than derived, because section 32 makes a completed trade a
    #: historical fact and the orders behind it can be archived. `CLAUDE.md`
    #: records the live trade whose 279-point gap stayed invisible for three
    #: days because the log kept the requested price as the entry.
    requested_entry_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    entry_slippage_points: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)

    #: Section 18. `fees` is distinct from `commission`: MT5 reports both, and
    #: folding one into the other makes the total right and each part wrong.
    fees: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    #: Section 19. Every P&L value must identify its currency. NULL means the
    #: account currency was not recorded, which is a gap and not a default.
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    #: Section 44. What did not add up, recorded beside the row rather than
    #: fixed. `None` means the checks have not run; `[]` means they ran and
    #: found nothing, and those are different facts.
    data_quality: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
