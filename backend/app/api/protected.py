"""Pre-v1 protected paths. As of L24 there are none left.

These were the L02/L04 stubs, kept as deprecated aliases while their groups
were unbuilt: 501 naming the level, from behind the same role gate.
They are hidden from the OpenAPI schema because `/v1` is the documented
surface, and they exist only so a caller written against the older paths keeps
working while it moves.

**`/orders` is deliberately not in this list.** It was here, answering "not
built until level 19", and that statement stopped being true at L06: the order
record is real and readable at `GET /v1/orders`. What is still unbuilt is
order *submission*, which is `POST /v1/orders` and says so there. Keeping a
root `/orders` that reports the whole group as missing would now be a false
statement about the record, and a false answer is worse than a moved one. The
only consumer was the test suite, which has moved with it; nothing in the
frontend called it.

Removal: once the frontend has no reference to an unprefixed path. Tracked in
MIGRATION_STATUS.md under L06.
"""

from __future__ import annotations

from app.api.pending import PENDING, build_router

# The pre-v1 paths, by the v1 path they moved from. Only groups that are still
# entirely unbuilt appear here.
LEGACY_PATHS: dict[str, str] = {
    # EMPTY as of L24, and that is the end state this module was written for.
    # Five entries have left it as their groups were built: "/orders" at L06,
    # "/brokers" at L10, "/strategies" at L12, "/bots" at L22 and "/ai/models"
    # at L24. In every case a root path reporting the whole group as missing
    # would have become a false statement about a surface that exists.
    #
    # The router is therefore empty and mounting it adds no route. The module
    # is kept rather than deleted because the rule it encodes -- a built group
    # drops its unversioned alias -- is one a later level will need, and a
    # deleted module takes its own reasoning with it. `tests/test_auth.py`
    # asserts the dict is empty, so a new entry has to be a deliberate act.
}

_legacy_groups = tuple(g for g in PENDING if g.path in LEGACY_PATHS)

# include_in_schema=False: /v1 is the documented surface. deprecated=True marks
# them for any client that reads the route objects directly.
router = build_router(_legacy_groups, include_in_schema=False, deprecated=True)

__all__ = ["LEGACY_PATHS", "router"]
