"""Patterns across many trades. Sections 27 to 30.

**This module computes no performance metric.** §27 and §50: it consumes L32
analytics and looks for shapes in the answer. `AnalyticsService.by()` already
groups trades by strategy, symbol, bot, model version, regime and exit reason
and reports win rate, expectancy and profit factor per group -- recomputing any
of that here would be the duplicate engine §27 forbids and would eventually
disagree with the dashboard.

**Sample size is never hidden.** §28. Every observation carries `trades`, and
below the floor it is labelled INSUFFICIENT rather than reported as a pattern.
The floor is L32's own `MIN_TRADES_FOR_COMPARISON`, imported rather than
re-picked: a shape that is "not enough to compare" on the analytics screen must
not be "a recurring pattern" on this one.

**An observation is not a recommendation.** §29: *do not automatically disable
the strategy*. Nothing here writes to a strategy, a bot or a risk rule, and the
strongest thing it produces is a sentence with a count attached.

**Grouping is deterministic.** §30 permits ML clustering only where the project
already supports it. It does not, so grouping is by recorded attributes -- which
is what §30 calls an acceptable baseline, and what this repository can actually
justify.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.metrics import INSUFFICIENT_DATA
from app.analytics.service import MIN_TRADES_FOR_COMPARISON, AnalyticsService, Scope
from app.analytics.windows import Window

#: The dimensions worth looking along. Each is a question somebody would ask:
#: does this strategy behave differently by regime? does this symbol cost more
#: to execute? A dimension with no recorded values simply produces no groups.
DIMENSIONS: tuple[str, ...] = (
    "strategy",
    "symbol",
    "bot",
    "exit_reason",
    "regime",
    "model_version",
)


@dataclass(frozen=True)
class Observation:
    """One shape seen in the grouped data, with its sample size attached."""

    dimension: str
    group: str
    trades: int
    statement: str
    #: `True` only when the group cleared the sample floor. §28: a shape from
    #: four trades is reported AS a shape from four trades.
    reliable: bool
    metrics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "group": self.group,
            "trades": self.trades,
            "statement": self.statement,
            "reliable": self.reliable,
            "sample_note": (
                f"observed across {self.trades} trades."
                if self.reliable
                else f"observed in {self.trades} trades; insufficient sample for a "
                "reliable pattern."
            ),
            "metrics": self.metrics,
        }


class PatternFinder:
    """Reads analytics; concludes nothing about what to do next."""

    def __init__(self, minimum: int = MIN_TRADES_FOR_COMPARISON) -> None:
        self.minimum = minimum
        self.analytics = AnalyticsService()

    async def across(
        self,
        db: AsyncSession,
        *,
        environment: str | None = None,
        account_id: str | None = None,
        dimensions: tuple[str, ...] = DIMENSIONS,
    ) -> dict[str, Any]:
        """Every dimension, grouped by analytics, read for shapes."""
        scope = Scope(
            window=Window(None, None, "all_time"),
            environment=environment,
            account_id=account_id,
        )
        found: list[Observation] = []
        totals: dict[str, int] = {}

        for dimension in dimensions:
            block = await self.analytics.by(db, scope, dimension)
            totals[dimension] = block["group_count"]
            for group, stats in block["groups"].items():
                found.extend(self._read(dimension, group, stats))

        reliable = [o for o in found if o.reliable]
        return {
            "scope": scope.as_dict(),
            "observations": [o.as_dict() for o in found],
            "reliable_count": len(reliable),
            "total_observations": len(found),
            "groups_examined": totals,
            "minimum_sample": self.minimum,
            "method": (
                "grouped by recorded attributes and read from L32 analytics. No metric "
                "is recomputed here, and no clustering model is fitted -- this platform "
                "has none, and section 30 permits a deterministic baseline."
            ),
            "authority": (
                "observations, never actions. Nothing here disables a strategy, changes "
                "a risk rule or stops a bot. A shape in the data is a reason to look, "
                "and looking is a person's job."
            ),
            "caution": (
                "these groups were not chosen in advance. Looking along six dimensions "
                "and reporting whichever looks worst is a search, and this repository's "
                "own record is a long list of searches whose best candidate did not "
                "survive out of sample."
            ),
        }

    def _read(self, dimension: str, group: str, stats: dict[str, Any]) -> list[Observation]:
        """Shapes worth naming in one group. Facts, with their counts."""
        trades = int(stats.get("trades") or 0)
        if trades == 0:
            return []
        reliable = trades >= self.minimum
        currency = stats.get("currency") or {}
        out: list[Observation] = []

        expectancy = currency.get("expectancy")
        win_rate = currency.get("win_rate")
        metrics = {
            "expectancy": expectancy,
            "win_rate": win_rate,
            "net_profit": currency.get("net_profit"),
            "profit_factor": currency.get("profit_factor"),
        }

        if isinstance(expectancy, (int, float)) and expectancy < 0:
            out.append(
                Observation(
                    dimension=dimension,
                    group=group,
                    trades=trades,
                    statement=(
                        f"{dimension} {group!r} has negative expectancy "
                        f"({expectancy:.4f} per trade) over {trades} trades."
                    ),
                    reliable=reliable,
                    metrics=metrics,
                )
            )

        streaks = currency.get("streaks") or {}
        longest_loss = streaks.get("longest_loss") or 0
        if isinstance(longest_loss, int) and longest_loss >= 5:
            out.append(
                Observation(
                    dimension=dimension,
                    group=group,
                    trades=trades,
                    statement=(
                        f"{dimension} {group!r} ran {longest_loss} consecutive losing trades."
                    ),
                    reliable=reliable,
                    metrics={"longest_loss_streak": longest_loss, **metrics},
                )
            )

        if isinstance(win_rate, (int, float)) and win_rate <= 0.3 and trades >= 5:
            out.append(
                Observation(
                    dimension=dimension,
                    group=group,
                    trades=trades,
                    statement=(f"{dimension} {group!r} won {win_rate:.0%} of {trades} trades."),
                    reliable=reliable,
                    metrics=metrics,
                )
            )

        if currency.get("profit_factor") == INSUFFICIENT_DATA and trades >= 5:
            out.append(
                Observation(
                    dimension=dimension,
                    group=group,
                    trades=trades,
                    statement=(
                        f"{dimension} {group!r} has no losing trade in {trades}, so its "
                        "profit factor is undefined rather than large."
                    ),
                    reliable=reliable,
                    metrics=metrics,
                )
            )
        return out


__all__ = ["DIMENSIONS", "Observation", "PatternFinder"]
