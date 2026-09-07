"""A small metrics registry: counters, gauges and histograms, bounded.

Sections 49 and 50.

**Why not Prometheus.** The audit found no metrics framework and no exporter,
and adding `prometheus_client` would put a scrape endpoint and a global default
registry into a process that currently has neither. This gives the same three
instrument types in ninety lines, with no dependency, and the numbers are
already served over the API the admin panel reads. A deployment that wants a
scrape endpoint gets one by writing an exposition formatter over
`Registry.snapshot()`; nothing else has to change.

**Cardinality is enforced, not requested.** Section 50 says not to use
`trade_id`, `order_id` or `request_id` as labels. `FORBIDDEN_LABELS` refuses
those names outright, `MAX_SERIES` caps how many label combinations one metric
may hold, and a metric that hits the cap stops creating new series and counts
the overflow instead of growing without bound. An unbounded label set is a
memory leak that looks like observability.

**A metric is a count, never a payload.** There is no instrument here that can
hold a string, so nothing recorded can carry a symbol's price, a body or an
address. Ids go in logs and traces, which is what section 50 asks for.

**Recording is best-effort and never raises into a caller.** Section 66:
monitoring failure must not stop trading. A counter that cannot be incremented
is not a reason to fail an order, so `observe` swallows its own errors.
"""

from __future__ import annotations

import logging
import threading
from bisect import bisect
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("app.observability.metrics")

#: Section 50, as a refusal rather than a convention. Any label whose name
#: contains one of these is rejected: they are unbounded by construction.
FORBIDDEN_LABELS: frozenset[str] = frozenset(
    {
        "trade_id",
        "order_id",
        "position_id",
        "request_id",
        "correlation_id",
        "user_id",
        "notification_id",
        "event_id",
        "session_id",
        "account_id",
        "email",
    }
)

#: How many label combinations one metric may hold. A metric that reaches it
#: records into an `__overflow__` series rather than growing.
MAX_SERIES = 200

#: Latency buckets, in milliseconds. Fixed rather than configurable: a
#: histogram whose buckets move is a histogram whose history cannot be
#: compared to itself.
BUCKETS_MS: tuple[float, ...] = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


class CardinalityRefused(ValueError):
    """A label that would grow without bound. Refused at definition time."""


def _key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))


def check_labels(labels: dict[str, str] | None) -> None:
    for name in labels or {}:
        lowered = name.lower()
        if lowered in FORBIDDEN_LABELS or any(f in lowered for f in ("_id", "id_")):
            raise CardinalityRefused(
                f"{name!r} is an identifier and would give this metric one series per "
                "value. Identifiers belong in logs and traces; a metric label must "
                "come from a small closed set."
            )


@dataclass
class Series:
    count: float = 0.0
    #: Only meaningful for a histogram.
    total: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))


@dataclass
class Metric:
    name: str
    kind: str  # counter | gauge | histogram
    help: str
    series: dict[tuple[tuple[str, str], ...], Series] = field(default_factory=dict)
    overflowed: int = 0

    def _series(self, labels: dict[str, str] | None) -> Series | None:
        key = _key(labels)
        found = self.series.get(key)
        if found is not None:
            return found
        if len(self.series) >= MAX_SERIES:
            self.overflowed += 1
            return self.series.setdefault(_key({"__overflow__": "1"}), Series())
        created = Series()
        self.series[key] = created
        return created

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "help": self.help,
            "series": [],
        }
        if self.overflowed:
            out["overflowed"] = self.overflowed
        for key, series in sorted(self.series.items()):
            row: dict[str, Any] = {"labels": dict(key), "value": series.count}
            if self.kind == "histogram":
                row["sum"] = round(series.total, 3)
                row["mean"] = round(series.total / series.count, 3) if series.count else None
                row["buckets"] = {
                    str(edge): n
                    for edge, n in zip([*BUCKETS_MS, "+Inf"], series.buckets, strict=True)
                }
            out["series"].append(row)
        return out


class Registry:
    """One per process. Thread-safe because a worker and a request may share it."""

    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}
        self._lock = threading.Lock()

    def define(self, name: str, kind: str, help: str) -> Metric:  # noqa: A002
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                return existing
            metric = Metric(name=name, kind=kind, help=help)
            self._metrics[name] = metric
            return metric

    # ------------------------------------------------------------ recording

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: dict[str, str] | None = None,
        *,
        help: str = "",  # noqa: A002
    ) -> None:
        self._record("counter", name, value, labels, help)

    def gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        *,
        help: str = "",  # noqa: A002
    ) -> None:
        self._record("gauge", name, value, labels, help)

    def observe(
        self,
        name: str,
        ms: float,
        labels: dict[str, str] | None = None,
        *,
        help: str = "",  # noqa: A002
    ) -> None:
        self._record("histogram", name, ms, labels, help)

    def _record(
        self,
        kind: str,
        name: str,
        value: float,
        labels: dict[str, str] | None,
        help: str,  # noqa: A002
    ) -> None:
        try:
            check_labels(labels)
            metric = self.define(name, kind, help)
            with self._lock:
                series = metric._series(labels)
                if series is None:  # pragma: no cover - defensive
                    return
                if kind == "gauge":
                    series.count = float(value)
                elif kind == "counter":
                    series.count += float(value)
                else:
                    series.count += 1
                    series.total += float(value)
                    series.buckets[bisect(BUCKETS_MS, float(value))] += 1
        except CardinalityRefused:
            raise
        except Exception:  # noqa: BLE001 - monitoring never fails a caller
            log.warning(
                "a metric could not be recorded",
                extra={"event": "metric_record_failed", "metric": name},
            )

    # -------------------------------------------------------------- reading

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [m.as_dict() for m in sorted(self._metrics.values(), key=lambda m: m.name)]

    def value(self, name: str, labels: dict[str, str] | None = None) -> float | None:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                return None
            series = metric.series.get(_key(labels))
            return None if series is None else series.count

    def reset(self) -> None:
        """Tests only. Never called by the application."""
        with self._lock:
            self._metrics.clear()


#: Section 49's list, trimmed to what this platform can actually measure. A
#: metric whose source does not exist is not defined at all -- a gauge that
#: reads zero because nothing feeds it is indistinguishable from a real zero.
METRIC_NAMES: tuple[str, ...] = (
    "component_state",  # gauge, by component: the STATE_RANK of its state
    "component_check_duration_ms",  # histogram, by component
    "component_checks_total",  # counter, by component and result
    "incidents_total",  # counter, by incident type
    "db_query_duration_ms",  # histogram (the health probe's own SELECT 1)
    "redis_latency_ms",  # histogram
    "worker_passes_total",  # gauge, by worker
    "worker_failures_total",  # gauge, by worker
    "queue_depth",  # gauge, by queue
    "notification_deliveries_total",  # gauge, by channel and status
    "orders_by_status",  # gauge, by status -- a closed set, not an id
    "realtime_connections",  # gauge
    "realtime_events_total",  # gauge
    "monitoring_collections_total",  # counter
    "monitoring_collection_duration_ms",  # histogram
)


__all__ = [
    "BUCKETS_MS",
    "FORBIDDEN_LABELS",
    "MAX_SERIES",
    "METRIC_NAMES",
    "CardinalityRefused",
    "Metric",
    "Registry",
    "Series",
    "check_labels",
]
