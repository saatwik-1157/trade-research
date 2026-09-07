"""Create or promote the first admin.

    BOOTSTRAP_ADMIN_PASSWORD=... python -m app.auth.bootstrap --email admin@example.org

Registration never yields an admin; this is the only way to make one until an
existing admin promotes another. The password comes from the environment, not
the command line, so it does not land in shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from app.auth import service
from app.auth.passwords import MIN_PASSWORD_LENGTH
from app.core.settings import get_settings
from app.db.session import make_engine, make_session_factory


async def _run(email: str, password: str) -> int:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as db:
            user, action = await service.ensure_admin(db, email, password)
        print(f"admin {user.email}: {action}")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True)
    args = ap.parse_args(argv)
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"BOOTSTRAP_ADMIN_PASSWORD must be set and at least {MIN_PASSWORD_LENGTH} characters",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(args.email, password))


if __name__ == "__main__":
    raise SystemExit(main())
