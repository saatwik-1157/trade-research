"""Domain time for the paper venue: timezone-aware UTC, always.

`app.auth.models.utcnow` returns a **naive** datetime on purpose -- the
`DateTime` columns are naive, so that is the right helper for a database write.
It is the wrong helper for a trading decision, because the market-data layer is
timezone-aware: bar times, quote times and staleness all carry UTC.

Mixing the two raises `TypeError: can't subtract offset-naive and offset-aware
datetimes` at the first comparison. That is what happened here: every test
passed `now=` explicitly, so nothing exercised the default, and the fault only
appeared against a live feed.

So the rule is one line: **domain time is `now_utc()`, storage time is
`utcnow()`**, and the conversion happens at the persistence boundary with an
explicit `.replace(tzinfo=None)`.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_utc() -> datetime:
    """The current moment, timezone-aware, for comparing against market data."""
    return datetime.now(UTC)


def to_storage(value: datetime) -> datetime:
    """Aware UTC -> the naive UTC the DateTime columns hold."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def from_storage(value: datetime) -> datetime:
    """Naive UTC from a column -> aware, for comparing against market data."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
