"""Monitoring, observability and system health (L37).

    OBSERVE -> MEASURE -> DETECT -> REPORT

and never `OBSERVE -> MODIFY TRADING`. Section 81.

**What this package may not do**, and a test parses every module here to prove
it: import a risk engine, an order manager, a position manager, a sizing
service, an execution pipeline or a broker adapter class; place, modify or
cancel anything; restart a worker, reconnect an adapter or change a bot's
state. It reads what the owning systems report and writes rows.

A bug in this package can report the wrong colour or fail to report at all. It
cannot trade, and it cannot recover -- recovery is L38's, deliberately kept
apart, because a monitor that restarts the thing it is watching cannot tell you
it failed to restart it.

Submodules:

    contract     component states, layers, criticality, trading safety
    thresholds   every number the monitor compares against, in one place
    metrics      counters, gauges and histograms, with bounded labels
    collectors   what each component reports, read from the system that owns it
    incidents    hysteresis, `system_events` rows, and one SYSTEM_ALERT
    service      one collection pass, one answer, read by everything
    worker       the loop, on L02's worker base
"""

from __future__ import annotations
