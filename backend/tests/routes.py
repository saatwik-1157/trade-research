"""Reading the API surface, on either shape `app.routes` has had.

Up to FastAPI 0.140 `include_router` COPIED every route into the parent with
the prefixes already combined, so `app.routes` was a flat list of `APIRoute`
and a test could read `route.path` straight off it. 0.141 stopped flattening:
it appends one private `_IncludedRouter` wrapper per `include_router` call and
resolves the real routes through it at request time.

The application is unaffected -- requests route correctly either way -- but
every test that asked `app.routes` what the surface contains started seeing 33
pathless wrappers instead of several hundred routes. That turns each
"no route under /v1/ai promotes a model" assertion into a statement about an
empty set, which is true and worthless. Three such tests had no emptiness guard
and passed while checking nothing; the ones that did guard are how this was
found, and are why walking a private structure is safe here. A shape this does
not understand yields nothing, and those tests fail loudly rather than going
quiet again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouteView:
    """One servable route: the full path, and the methods it answers."""

    path: str
    methods: frozenset[str]


def api_routes(app: Any) -> list[RouteView]:
    """Every route the app will serve, with its full path.

    Written against both shapes on purpose: the suite has to be honest about
    the surface on the FastAPI a developer has installed and on the one CI
    resolves, and those were 0.136 and 0.141 when this was written.
    """
    found: list[RouteView] = []

    def walk(routes: Iterable[Any], prefix: str) -> None:
        for route in routes:
            # FastAPI >= 0.141: a nested router, not a route. The prefix it was
            # mounted under lives on the include, not on the routes below it.
            context = getattr(route, "include_context", None)
            if context is not None:
                walk(context.included_router.routes, prefix + (context.prefix or ""))
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            found.append(RouteView(prefix + path, frozenset(getattr(route, "methods", None) or ())))

    walk(app.routes, "")
    return found
