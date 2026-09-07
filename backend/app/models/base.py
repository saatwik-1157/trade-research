"""Shared column types and mixins for every platform table.

Conventions, all deliberate:
  - ids are UUID strings (portable across SQLite in tests and PostgreSQL);
  - timestamps are naive UTC (see app.auth.models.utcnow);
  - prices and quantities are Numeric, never float;
  - JSON is JSONB on PostgreSQL and JSON elsewhere;
  - every execution-side row carries `mode` in {paper, demo, live} so results
    from the simulator, the MT5 demo account and a real account can never be
    pooled by accident.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import new_id, utcnow

JSONType = JSON().with_variant(JSONB(), "postgresql")
# The same JSON, except that a Python `None` becomes SQL NULL rather than the
# JSON literal `null`. SQLAlchemy's default is the latter, which is correct for
# a column where "the value is JSON null" is meaningful -- and wrong for one a
# CHECK constraint tests with `IS NULL`. Found at L24: a refused prediction
# stored `'null'` and tripped `refusal_carries_no_value`, which is the
# constraint that exists to stop a refusal carrying a value.
NullableJSONType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
Price = Numeric(18, 8)
Qty = Numeric(18, 8)
Money = Numeric(18, 4)
Ratio = Numeric(12, 6)

MODES = ("paper", "demo", "live")
MODE_CHECK = "mode IN ('paper','demo','live')"


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class CreatedMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class TimestampMixin(CreatedMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
