"""Every number the monitor compares against, in one place.

Section 44: *do not hard-code thresholds throughout the codebase*. A threshold
scattered across five collectors is five numbers that drift, and the one an
operator reads on a dashboard is never the one the alert used.

Section 45's hysteresis lives here too. A service that fails for 100ms must not
produce DOWN/UP/DOWN/UP: a condition has to be observed `consecutive_failures`
times before it is raised and `consecutive_successes` times before it is
cleared. Two of each, on a fifteen-second interval, means a real fault is
reported within thirty seconds and a blip is not reported at all.

**Nothing here is a trading limit.** These decide when to *say* something. The
RiskEngine owns what may be traded, and `app/observability` cannot reach it --
a parse test keeps that true. A threshold in this file being wrong makes the
platform noisy or quiet; it cannot make it trade.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Thresholds:
    """Every bar the monitor uses. Overridable per deployment, not per call."""

    # --- Hysteresis (§45) --------------------------------------------------
    consecutive_failures: int = 2
    consecutive_successes: int = 2

    # --- Collection --------------------------------------------------------
    #: How often the monitoring worker runs. Section 75: monitoring must not
    #: become a load. Fifteen seconds is four database round trips a minute.
    interval_seconds: float = 15.0
    #: Per-probe ceiling. A probe that hangs must not hold the pass open.
    probe_timeout_seconds: float = 3.0

    # --- Infrastructure ----------------------------------------------------
    database_latency_warning_ms: float = 250.0
    database_latency_critical_ms: float = 1000.0
    redis_latency_warning_ms: float = 100.0
    redis_latency_critical_ms: float = 500.0

    # --- Workers and queues (§15, §16) -------------------------------------
    #: A worker is stale when its own `WorkerStatus.is_stale` says so -- that
    #: logic is L02's and is not duplicated here. This is the extra bar for
    #: "some workers are failing but still ticking".
    worker_failure_rate_warning: float = 0.2
    queue_backlog_warning: int = 50
    queue_backlog_critical: int = 250
    #: Oldest pending item, in seconds. A short queue that is not moving is
    #: worse than a long one that is.
    queue_age_warning_seconds: float = 300.0

    # --- Market data (§20, §21) --------------------------------------------
    #: Freshness, measured against the newest bar the platform has stored.
    #: Generous because a stored-bar platform with no live feed is the normal
    #: state here, and crying wolf about it every fifteen seconds would make
    #: the whole dashboard ignorable.
    market_data_stale_seconds: float = 900.0
    market_data_critical_seconds: float = 3600.0

    # --- Notifications (§34) -----------------------------------------------
    notification_failure_rate_warning: float = 0.1
    notification_queue_warning: int = 100

    # --- Trading (§27) -----------------------------------------------------
    #: Orders whose broker state the OMS could not establish. Any at all is
    #: worth saying; section 17 is explicit that this is reported as
    #: RECONCILIATION_REQUIRED and never as a failure.
    unknown_orders_warning: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "hysteresis": {
                "consecutive_failures": self.consecutive_failures,
                "consecutive_successes": self.consecutive_successes,
                "note": (
                    "a condition is raised only after it is observed this many times "
                    "in a row, and cleared only after it is absent this many times. "
                    "A service that fails for 100ms produces no incident at all."
                ),
            },
            "collection": {
                "interval_seconds": self.interval_seconds,
                "probe_timeout_seconds": self.probe_timeout_seconds,
            },
            "database": {
                "latency_warning_ms": self.database_latency_warning_ms,
                "latency_critical_ms": self.database_latency_critical_ms,
            },
            "redis": {
                "latency_warning_ms": self.redis_latency_warning_ms,
                "latency_critical_ms": self.redis_latency_critical_ms,
            },
            "workers": {
                "failure_rate_warning": self.worker_failure_rate_warning,
                "staleness": "each worker's own interval x2, floored at 30s (L02)",
            },
            "queues": {
                "backlog_warning": self.queue_backlog_warning,
                "backlog_critical": self.queue_backlog_critical,
                "age_warning_seconds": self.queue_age_warning_seconds,
            },
            "market_data": {
                "stale_seconds": self.market_data_stale_seconds,
                "critical_seconds": self.market_data_critical_seconds,
            },
            "notifications": {
                "failure_rate_warning": self.notification_failure_rate_warning,
                "queue_warning": self.notification_queue_warning,
            },
            "trading": {"unknown_orders_warning": self.unknown_orders_warning},
            "authority": (
                "these decide when the platform SAYS something. None of them is a "
                "trading limit: the RiskEngine owns what may be traded, and nothing "
                "in app/observability can reach it."
            ),
        }

    def with_overrides(self, **overrides: Any) -> Thresholds:
        known = {k: v for k, v in overrides.items() if hasattr(self, k) and v is not None}
        return replace(self, **known)


DEFAULT_THRESHOLDS = Thresholds()


def from_settings(settings: Any) -> Thresholds:
    """Thresholds for this deployment. Absent settings keep the defaults."""
    return DEFAULT_THRESHOLDS.with_overrides(
        interval_seconds=getattr(settings, "monitoring_interval_seconds", None),
        market_data_stale_seconds=getattr(settings, "market_data_stale_seconds", None),
        queue_backlog_warning=getattr(settings, "queue_backlog_warning", None),
    )


__all__ = ["DEFAULT_THRESHOLDS", "Thresholds", "from_settings"]
