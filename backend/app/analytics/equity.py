"""Equity curves and drawdown periods. Sections 7 and 8.

**Two curves, because there are two different facts, and they are never merged.**

    REALIZED     cumulative net P&L from `trades`, oldest close first.
                 Cannot contain a deposit by construction: every point is the
                 sum of trade results, and a trade result is a trade result.

    ACCOUNT      equity over time from `portfolio_snapshots` (L05).
                 CAN contain a deposit, and this platform records no cash
                 movements, so it cannot tell one from a profit.

Section 7 says not to interpret deposits and withdrawals as trading profit, and
asks that they be handled *if these exist in the project*. They do not: there is
no cash-movement table, and no column anywhere records a transfer. The honest
consequence is not to build a curve that silently treats an unexplained balance
jump as performance — it is to make the realized curve the one performance is
read from, and to label the account curve as including whatever else moved the
balance.

**Environments are never mixed.** Section 7 again, and §23. A curve is built for
ONE environment; a comparison puts two curves side by side and keeps both
labelled. `combine` refuses two curves whose environments differ.

**Drawdown is computed from time-ordered data or not at all.** Section 8. The
constructor sorts, and a curve built from an unordered read would otherwise
produce a peak that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.analytics.metrics import INSUFFICIENT_DATA


class EnvironmentMismatch(Exception):
    """Two curves from different environments were about to be merged."""


@dataclass(frozen=True)
class Point:
    """One equity reading, and when it was taken."""

    at: datetime
    value: float
    #: What produced this point: a trade id, a snapshot id, or nothing.
    ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "value": self.value, "ref": self.ref}


@dataclass
class Curve:
    """An ordered equity series for ONE environment."""

    points: list[Point]
    environment: str
    kind: str  # "realized" | "account"
    starting_value: float = 0.0

    @classmethod
    def realized_from(
        cls,
        results: list[tuple[datetime, float, str | None]],
        *,
        environment: str,
        starting_value: float = 0.0,
    ) -> Curve:
        """Cumulative net P&L, oldest close first.

        Starts at `starting_value` — 0.0 by default, which makes the curve a
        pure P&L series rather than an account balance. A caller that wants a
        balance-shaped curve passes the starting balance and gets one, and the
        `kind` still says `realized` so nobody reads it as the account's own
        equity.
        """
        running = starting_value
        points: list[Point] = []
        for at, value, ref in sorted(results, key=lambda row: row[0]):
            running += value
            points.append(Point(at=at, value=running, ref=ref))
        return cls(
            points=points,
            environment=environment,
            kind="realized",
            starting_value=starting_value,
        )

    @classmethod
    def account_from(
        cls, readings: list[tuple[datetime, float, str | None]], *, environment: str
    ) -> Curve:
        """Account equity over time, exactly as recorded. Nothing is inferred."""
        ordered = sorted(readings, key=lambda row: row[0])
        return cls(
            points=[Point(at=at, value=value, ref=ref) for at, value, ref in ordered],
            environment=environment,
            kind="account",
            starting_value=ordered[0][1] if ordered else 0.0,
        )

    def __len__(self) -> int:
        return len(self.points)

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "kind": self.kind,
            "points": [p.as_dict() for p in self.points],
            "count": len(self.points),
            "starting_value": self.starting_value,
            "final_value": self.points[-1].value if self.points else self.starting_value,
            "note": (
                "cumulative realised P&L from the trade journal. It cannot contain a "
                "deposit: every point is the sum of trade results."
                if self.kind == "realized"
                else "account equity as recorded. This platform stores no cash movements, "
                "so a balance change this curve shows is NOT necessarily trading "
                "performance -- a deposit and a profit look identical here. Read the "
                "realized curve for performance."
            ),
        }


def combine(first: Curve, second: Curve) -> None:
    """Refuse. Section 7: environments stay identifiable.

    A function that exists to say no, rather than an absent one, because the
    absence would be filled by a caller writing `first.points + second.points`.
    """
    raise EnvironmentMismatch(
        f"a {first.environment}/{first.kind} curve and a {second.environment}/"
        f"{second.kind} curve are not one series. Compare them side by side with "
        "both labelled; merging them produces a curve of nothing."
    )


# ================================================================ drawdown


@dataclass(frozen=True)
class Period:
    """One peak-to-trough-to-recovery episode. Section 8."""

    peak_at: datetime
    peak_value: float
    trough_at: datetime
    trough_value: float
    recovered_at: datetime | None = None

    @property
    def depth(self) -> float:
        return self.peak_value - self.trough_value

    @property
    def depth_pct(self) -> float | str:
        if self.peak_value <= 0:
            # A percentage of a non-positive peak is not a percentage. Reported
            # rather than divided, because the realized curve legitimately
            # starts at zero and passes through negative territory.
            return INSUFFICIENT_DATA
        return self.depth / self.peak_value

    @property
    def duration_seconds(self) -> float:
        return (self.trough_at - self.peak_at).total_seconds()

    @property
    def recovery_seconds(self) -> float | str:
        if self.recovered_at is None:
            return INSUFFICIENT_DATA
        return (self.recovered_at - self.trough_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "peak_at": self.peak_at.isoformat(),
            "peak_value": self.peak_value,
            "trough_at": self.trough_at.isoformat(),
            "trough_value": self.trough_value,
            "recovered_at": self.recovered_at.isoformat() if self.recovered_at else None,
            "depth": self.depth,
            "depth_pct": self.depth_pct,
            "duration_seconds": self.duration_seconds,
            "recovery_seconds": self.recovery_seconds,
            "recovered": self.recovered_at is not None,
        }


def periods(curve: Curve) -> list[Period]:
    """Every completed and in-progress drawdown, in order. Section 8.

    A period opens when the curve falls below a peak and closes when it regains
    it. The final one may be open — `recovered_at` is `None` — and that is the
    current drawdown, reported as unrecovered rather than as recovered at the
    last point in the series.

    Handles the cases §8 names: no points, one point, monotonically rising (no
    periods), monotonically falling (one open period), and several separate
    episodes.
    """
    if len(curve.points) < 2:
        return []

    found: list[Period] = []
    peak = curve.points[0]
    trough: Point | None = None

    for point in curve.points[1:]:
        if point.value >= peak.value:
            if trough is not None:
                found.append(
                    Period(
                        peak_at=peak.at,
                        peak_value=peak.value,
                        trough_at=trough.at,
                        trough_value=trough.value,
                        recovered_at=point.at,
                    )
                )
                trough = None
            peak = point
            continue
        if trough is None or point.value < trough.value:
            trough = point

    if trough is not None:
        found.append(
            Period(
                peak_at=peak.at,
                peak_value=peak.value,
                trough_at=trough.at,
                trough_value=trough.value,
                recovered_at=None,
            )
        )
    return found


def analyse(curve: Curve) -> dict[str, Any]:
    """Section 8's full block: current, maximum, average, and every period."""
    found = periods(curve)
    if not curve.points:
        return {
            "available": False,
            "why": "the curve has no points, so there is no drawdown to measure",
            "environment": curve.environment,
            "kind": curve.kind,
        }

    peak = max(p.value for p in curve.points)
    final = curve.points[-1].value
    open_period = found[-1] if found and found[-1].recovered_at is None else None
    deepest = max(found, key=lambda p: p.depth) if found else None
    recovered = [p for p in found if p.recovered_at is not None]

    return {
        "available": True,
        "environment": curve.environment,
        "kind": curve.kind,
        "peak_equity": peak,
        "current_equity": final,
        # Floored at zero: a new high is not a drawdown.
        "current_drawdown": max(0.0, peak - final),
        "in_drawdown": open_period is not None,
        "max_drawdown": deepest.depth if deepest else 0.0,
        "max_drawdown_pct": deepest.depth_pct if deepest else 0.0,
        "max_drawdown_at": deepest.trough_at.isoformat() if deepest else None,
        "average_drawdown": (sum(p.depth for p in found) / len(found)) if found else 0.0,
        "average_recovery_seconds": (
            sum(float(p.recovery_seconds) for p in recovered) / len(recovered)
            if recovered
            else INSUFFICIENT_DATA
        ),
        "recovery_factor": _recovery_factor(final, curve.starting_value, deepest),
        "periods": [p.as_dict() for p in found],
        "period_count": len(found),
        "note": (
            "peak-to-subsequent-trough, computed over time-ordered points. The last "
            "period is left OPEN when the curve has not regained its peak: reporting "
            "it as recovered at the final point would say the account came back when "
            "it has not."
        ),
    }


def _recovery_factor(final: float, start: float, deepest: Period | None) -> float | str:
    """net profit / maximum drawdown. Section 8, 'where meaningful'.

    `INSUFFICIENT_DATA` when there was no drawdown: dividing by zero is not a
    large recovery factor, it is an undefined one, and a large number here would
    read as a strong result from a sample that simply never fell.
    """
    if deepest is None or deepest.depth <= 0:
        return INSUFFICIENT_DATA
    return (final - start) / deepest.depth


def calmar(final: float, start: float, deepest_depth: float) -> float | str:
    """Return over maximum drawdown. Section 9.

    Not annualised, for the reason Sharpe is not: a fixed window supplies no
    periods-per-year figure, and inventing one is how a modest ratio becomes an
    impressive one.
    """
    if deepest_depth <= 0 or start <= 0:
        return INSUFFICIENT_DATA
    return ((final - start) / start) / (deepest_depth / start)


__all__ = [
    "Curve",
    "EnvironmentMismatch",
    "Period",
    "Point",
    "analyse",
    "calmar",
    "combine",
    "periods",
]
