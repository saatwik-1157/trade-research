"""Date ranges, buckets and the timezone policy. Sections 10 and 43.

**One canonical timestamp: naive UTC in the database.** Section 43 asks that the
policy be defined and documented, and this platform already has one — every
`DateTime` column stores a naive value that means UTC, and every service writes
`datetime.now(UTC).replace(tzinfo=None)`. This module states it rather than
inventing a second convention.

**The trading day is `app.risk.state.day_start`, not a new one.** L17 chose it
and L30 reused it; a third definition here would reintroduce the bug `CLAUDE.md`
records at length — MT5 renders a server stamp through the LOCAL zone, so a naive
`.replace(hour=0)` starts the day at 18:30 the previous evening on a UTC+5:30
machine. Section 43's own example is exactly this: *23:59 UTC must not
accidentally become the next trading day.*

**Aware in, naive out.** `day_start` works in aware UTC and the database stores
naive UTC, so the conversion happens once, here, with the reason attached. That
is the seam L30 found and it is crossed the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

#: Where the boundary between one reporting day and the next sits. Documented
#: as a policy rather than a constant somebody could change per screen.
TIMEZONE_POLICY = {
    "database": "naive datetimes that mean UTC",
    "api": "ISO 8601, naive, meaning UTC",
    "trading_day": (
        "app.risk.state.day_start -- L17's boundary, reused. A second definition "
        "would reintroduce the local-zone bug CLAUDE.md records: a naive "
        "`.replace(hour=0)` starts the day at 18:30 the previous evening on a "
        "UTC+5:30 machine and is correct on a UTC one."
    ),
    "display": "converted at the presentation boundary only, never in the query",
    "session": (
        "not modelled. The broker's session boundaries are a venue fact this "
        "platform does not record, and bucketing by an assumed one would label "
        "trades with a session they were not in."
    ),
}


class Bucket(StrEnum):
    """Section 10's breakdowns. Only the ones a UTC timestamp can answer."""

    hour = "hour"
    day = "day"
    week = "week"
    month = "month"
    quarter = "quarter"
    year = "year"
    day_of_week = "day_of_week"


class Preset(StrEnum):
    """Section 10's named ranges."""

    today = "today"
    yesterday = "yesterday"
    last_7d = "last_7d"
    last_30d = "last_30d"
    last_90d = "last_90d"
    this_month = "this_month"
    previous_month = "previous_month"
    this_year = "this_year"
    all_time = "all_time"


class WindowError(Exception):
    """A range that cannot be interpreted."""


@dataclass(frozen=True)
class Window:
    """A half-open range `[start, end)` in naive UTC.

    Half-open deliberately: a closed range double-counts a trade that closed
    exactly on a boundary when two adjacent windows are summed, and §41 asks
    that analytics agree with the journal's own count.
    """

    start: datetime | None
    end: datetime | None
    label: str = "custom"

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat() if self.start else None,
            "to": self.end.isoformat() if self.end else None,
            "label": self.label,
            "bounds": "half-open [start, end)",
            "timezone": "naive UTC",
        }


def _day_start(moment: datetime) -> datetime:
    """L17's boundary, crossed from naive UTC and back.

    Aware for the calculation and naive for the comparison. Doing neither is the
    bug `CLAUDE.md` documents; doing it twice, differently, is how two screens
    disagree about which day a trade belongs to.
    """
    from app.risk.state import day_start

    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return day_start(aware).replace(tzinfo=None)


def resolve(
    preset: str | None, *, now: datetime, start: datetime | None = None, end: datetime | None = None
) -> Window:
    """A named range or an explicit one. Section 10.

    An explicit `start`/`end` wins over a preset, and a reversed range is
    REFUSED rather than swapped: swapping produces a plausible window the caller
    did not ask for, and the figures that come back would be attributed to the
    wrong period.
    """
    if start is not None and end is not None and start >= end:
        raise WindowError(
            f"the window {start.isoformat()} to {end.isoformat()} ends before it "
            "begins. Refused rather than swapped: a swapped window returns real "
            "figures for a period nobody asked about."
        )
    if start is not None or end is not None:
        return Window(start=start, end=end, label="custom")

    if preset is None or preset == str(Preset.all_time):
        return Window(start=None, end=None, label=str(Preset.all_time))

    try:
        chosen = Preset(preset)
    except ValueError as exc:
        raise WindowError(f"{preset!r} is not a known range") from exc

    today = _day_start(now)
    if chosen is Preset.today:
        return Window(today, today + timedelta(days=1), str(chosen))
    if chosen is Preset.yesterday:
        return Window(today - timedelta(days=1), today, str(chosen))
    if chosen is Preset.last_7d:
        return Window(today - timedelta(days=7), today + timedelta(days=1), str(chosen))
    if chosen is Preset.last_30d:
        return Window(today - timedelta(days=30), today + timedelta(days=1), str(chosen))
    if chosen is Preset.last_90d:
        return Window(today - timedelta(days=90), today + timedelta(days=1), str(chosen))
    if chosen is Preset.this_month:
        first = today.replace(day=1)
        return Window(first, _next_month(first), str(chosen))
    if chosen is Preset.previous_month:
        first = today.replace(day=1)
        return Window(_previous_month(first), first, str(chosen))
    # this_year
    first = today.replace(month=1, day=1)
    return Window(first, first.replace(year=first.year + 1), str(chosen))


def _next_month(first: datetime) -> datetime:
    return (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )


def _previous_month(first: datetime) -> datetime:
    return (
        first.replace(year=first.year - 1, month=12)
        if first.month == 1
        else first.replace(month=first.month - 1)
    )


def key_for(moment: datetime, bucket: Bucket) -> str:
    """The bucket label one timestamp falls in. Computed in Python, from UTC.

    In Python rather than in SQL deliberately: `date_trunc` behaves differently
    across PostgreSQL and SQLite, and a bucket boundary that moved with the
    engine would make the test suite agree with a production it does not match.
    """
    if bucket is Bucket.hour:
        return moment.strftime("%Y-%m-%dT%H")
    if bucket is Bucket.day:
        return moment.strftime("%Y-%m-%d")
    if bucket is Bucket.week:
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"
    if bucket is Bucket.month:
        return moment.strftime("%Y-%m")
    if bucket is Bucket.quarter:
        return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"
    if bucket is Bucket.year:
        return str(moment.year)
    return moment.strftime("%A")


def describe() -> dict[str, Any]:
    """The policy, served rather than restated in a docstring somewhere else."""
    return {
        "timezone": TIMEZONE_POLICY,
        "buckets": [str(b) for b in Bucket],
        "presets": [str(p) for p in Preset],
        "bounds": (
            "every window is half-open [start, end). A closed range double-counts a "
            "trade closing exactly on a boundary when two adjacent windows are summed."
        ),
    }


__all__ = [
    "TIMEZONE_POLICY",
    "Bucket",
    "Preset",
    "Window",
    "WindowError",
    "describe",
    "key_for",
    "resolve",
]
