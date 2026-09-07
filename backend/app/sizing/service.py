"""Observability and logging around the sizing engine.

The engine in `calculator.py` is pure: no clock, no counters, no logging, so
the same request always produces the same result and a test can assert it
without stubbing anything. Everything that is *not* deterministic lives here.

This follows the shape `app.risk.service` already uses -- in-process counters
exposed through a `status()` dict rather than a second metrics stack. The
project has no Prometheus registry; adding one for six counters would be the
"duplicate monitoring infrastructure" the brief warns against. When one
arrives, it reads these fields.

**Nothing secret is logged.** A sizing log line carries a symbol, a side, two
prices and three money figures. It never carries an account credential, a
broker password or a token, because none of those are reachable from here --
the service holds no broker connection at all.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from decimal import Decimal

from app.sizing.calculator import (
    SizingRequest,
    SizingResult,
    calculate,
)

log = logging.getLogger("app.sizing")


class SizingService:
    """Counts, times and logs sizing decisions. Holds no trading authority."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        # Milliseconds, newest last. Bounded: this is a latency sample, not a
        # history, and an unbounded list in a long-running bot is a leak.
        self._latencies: list[float] = []
        self._max_samples = 256

    def size(self, request: SizingRequest, *, context: str = "") -> SizingResult:
        """Size one order, recording what happened. Never raises."""
        self._counts["requests_total"] += 1
        started = time.perf_counter()
        result = calculate(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._latencies.append(elapsed_ms)
        if len(self._latencies) > self._max_samples:
            del self._latencies[: -self._max_samples]

        if result.ok:
            self._counts["success_total"] += 1
            log.info(
                "position size calculated",
                extra={
                    "event": "position_size_calculated",
                    "context": context,
                    "symbol": result.symbol,
                    "side": result.side,
                    "sizing_mode": str(result.method),
                    "entry": str(result.entry_price) if result.entry_price else None,
                    "stop": str(result.stop_loss) if result.stop_loss else None,
                    "risk_amount": str(result.risk_requested) if result.risk_requested else None,
                    "raw_quantity": str(result.raw_volume) if result.raw_volume else None,
                    "final_quantity": str(result.volume),
                    "actual_risk": str(result.risk_actual) if result.risk_actual else None,
                    "warnings": list(result.warnings),
                },
            )
            return result

        self._counts["rejections_total"] += 1
        # A refusal caused by an unusable contract spec is counted separately:
        # it is an operational fault (the spec was never synced), not a
        # request that was correctly declined.
        gap = result.gap or ""
        if "contract spec" in gap or "tick_value" in gap or "tick_size" in gap:
            self._counts["broker_metadata_failures"] += 1
        log.warning(
            "position sizing refused",
            extra={
                "event": "position_sizing_rejected",
                "context": context,
                "symbol": result.symbol,
                "side": result.side,
                "sizing_mode": str(result.method),
                "reason": gap[:300],
            },
        )
        return result

    def status(self) -> dict[str, object]:
        latencies = sorted(self._latencies)
        return {
            "position_sizing_requests_total": self._counts["requests_total"],
            "position_sizing_success_total": self._counts["success_total"],
            "position_sizing_rejections_total": self._counts["rejections_total"],
            # Reserved and reported as zero rather than omitted: the engine
            # converts every fault into a refusal, so an error here would mean
            # a bug above it, and a field that only appears when it is
            # non-zero is a field nobody has a dashboard for.
            "position_sizing_errors_total": self._counts["errors_total"],
            "broker_metadata_failures": self._counts["broker_metadata_failures"],
            "quantity_calculation_latency_ms": {
                "samples": len(latencies),
                "p50": round(_percentile(latencies, 50), 4) if latencies else None,
                "p95": round(_percentile(latencies, 95), 4) if latencies else None,
                "max": round(latencies[-1], 4) if latencies else None,
            },
            "authority": (
                "None. This service proposes a quantity. app.risk decides whether the "
                "order may exist, and only app.risk can produce an Approval."
            ),
            "determinism": (
                "The engine is pure: same request, same result. No clock, no randomness, "
                "no I/O, no model."
            ),
        }


def _percentile(ordered: list[float], pct: int) -> float:
    """Nearest-rank on an already-sorted list. Exact, and no dependency."""
    if not ordered:
        return 0.0
    rank = max(1, min(len(ordered), (pct * len(ordered) + 99) // 100))
    return ordered[rank - 1]


def money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
