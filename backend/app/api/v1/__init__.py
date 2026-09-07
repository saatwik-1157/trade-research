"""The v1 API surface.

**Where the version lives.** The router is mounted at `/v1` on the
application. Behind nginx the API is served under `/api/`, so the public URL
is `/api/v1/...` -- which is the versioned path the target architecture asks
for -- and a caller talking to the backend directly in development uses
`/v1/...`. That split is not a compromise, it is where the two names already
were: `NEXT_PUBLIC_API_URL` has always been the API root, and the frontend has
always sent paths relative to it. Putting `/api` in the application as well
would mean `/api/api/v1` behind the proxy or a proxy rewrite, and neither buys
anything.

**What stays outside the version prefix, and why:**

  * `/health`, `/health/live`, `/health/ready` -- an infrastructure contract.
    The Compose healthcheck, nginx and any orchestrator probe them there.
    `/v1/system/health` reads the same check functions, so there is one
    implementation with two doors rather than two answers.
  * `/auth/*` and `/admin/users` -- mounted at both their original paths and
    under `/v1`, the originals hidden from the schema and marked deprecated.
    The same router object is mounted twice: one implementation, two paths, no
    duplicated handler. The originals go when the frontend has moved, which
    L06 does for every call it makes.

**What the surface promises.** Every collection is paginated with a hard
ceiling, every sort and filter field is looked up in an allow-list, every
failure answers the one error envelope with a request id, and every route that
cannot yet do its job answers 501 naming the level that builds it, from behind
its real authorization gate.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.admin.router import router as admin_users_router
from app.api import pending
from app.api.v1 import (
    accounts,
    admin,
    ai,
    analytics,
    backtests,
    bots,
    brokers,
    datasets,
    execution,
    market,
    monitoring,
    notifications,
    orders,
    paper,
    portfolio,
    position_sizing,
    positions,
    realtime,
    recovery,
    replay,
    reviews,
    risk,
    security,
    strategies,
    strategy_builder,
    symbol_admin,
    symbols,
    system,
    trades,
    webhooks,
)
from app.auth.router import router as auth_router

API_PREFIX = "/v1"


def build_v1_router() -> APIRouter:
    """Assemble the whole versioned surface.

    Order matters in one place only: `pending` is included last so that a
    group which later grows a real router shadows nothing -- a real route
    registered earlier wins, and the 501 for that path simply stops being
    reachable rather than having to be deleted in the same commit.
    """
    router = APIRouter(prefix=API_PREFIX)

    # Identity, mounted under the version alongside its legacy path.
    router.include_router(auth_router)
    router.include_router(admin_users_router)

    # Built, serving real rows.
    router.include_router(system.router)
    router.include_router(symbols.router)
    router.include_router(orders.router)
    router.include_router(trades.router)
    router.include_router(positions.router)
    router.include_router(accounts.router)
    router.include_router(admin.router)
    router.include_router(realtime.router)
    router.include_router(market.router)
    router.include_router(webhooks.router)
    router.include_router(brokers.router)
    router.include_router(symbol_admin.router)
    router.include_router(strategies.router)
    router.include_router(strategy_builder.router)
    router.include_router(backtests.router)
    router.include_router(replay.router)
    router.include_router(paper.router)
    router.include_router(risk.router)
    router.include_router(position_sizing.router)
    router.include_router(execution.router)
    router.include_router(bots.router)
    router.include_router(datasets.router)
    router.include_router(ai.router)
    router.include_router(portfolio.router)
    router.include_router(analytics.router)
    router.include_router(reviews.router)
    router.include_router(notifications.router)
    router.include_router(monitoring.router)
    router.include_router(recovery.router)
    router.include_router(security.router)

    # Declared, gated, not built. Each answers 501 naming its level.
    router.include_router(pending.build_router())
    return router


__all__ = ["API_PREFIX", "build_v1_router"]
