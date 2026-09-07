"""Realtime: the event catalogue, channels, the hub and the WebSocket endpoint.

Delivery only. Nothing in this package places an order, changes a position or
calls a domain service, and the WebSocket protocol has no publish verb -- a
browser that could publish onto the bus could publish ORDER_FILLED, and
something downstream would eventually believe it.

The database and the broker remain authoritative. An event says something
changed; a consumer that needs the value reads it back.
"""

from app.realtime.hub import Hub, SeenEvents

__all__ = ["Hub", "SeenEvents"]
