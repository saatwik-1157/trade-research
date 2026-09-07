"""Alembic migrations against a real PostgreSQL, when one is available.

Set TEST_DATABASE_URL (asyncpg URL to an empty, disposable database). Skipped
otherwise, so the default unit run needs no service; CI provides one.

Checks: upgrade to head creates every promised table; the models and the
migrations agree (no autogenerate drift); downgrade to base removes what it
created; upgrade again succeeds; and the users.role foreign key added to an
existing table finds its roles already seeded, which is the one ordering
mistake this migration could have made.

Alembic's command API is synchronous and drives its own event loop, so every
call goes through asyncio.to_thread rather than being awaited directly.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from app.db.base import Base
from app.models import EXPECTED_TABLES
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

#: This module runs `DROP SCHEMA public CASCADE`. That is correct for a scratch
#: database and catastrophic for a real one, and the only thing standing between
#: the two was that the variable is spelled `TEST_DATABASE_URL` rather than
#: `DATABASE_URL`. A tired operator exporting the wrong value loses the trade
#: history.
#:
#: So the database NAME must look like a test database. It is a name check
#: rather than a value check on purpose: the host, port and credentials of a
#: test database are legitimately identical to production's, and the name is the
#: one part that distinguishes them. Added at L42.
_TEST_DB_MARKERS = ("test", "scratch", "ci", "tmp")


def _refuse_a_real_database(url: str) -> None:
    from urllib.parse import urlsplit

    name = urlsplit(url).path.lstrip("/").lower()
    if not name:
        raise RuntimeError("TEST_DATABASE_URL names no database")
    if not any(marker in name for marker in _TEST_DB_MARKERS):
        raise RuntimeError(
            f"refusing to run migration tests against a database named {name!r}: "
            f"this module DROPS THE SCHEMA, and the name does not contain any of "
            f"{_TEST_DB_MARKERS}. Point TEST_DATABASE_URL at a scratch database "
            f"(e.g. {name}_test) -- never at one holding trade history."
        )


if URL:
    _refuse_a_real_database(URL)

BACKEND = Path(__file__).resolve().parent.parent


def _cfg() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


async def _upgrade(cfg: Config, rev: str = "head") -> None:
    await asyncio.to_thread(command.upgrade, cfg, rev)


async def _downgrade(cfg: Config, rev: str = "base") -> None:
    await asyncio.to_thread(command.downgrade, cfg, rev)


@pytest.fixture
async def cfg(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Config]:
    assert URL
    monkeypatch.setenv("DATABASE_URL", URL)
    from app.core.settings import get_settings

    get_settings.cache_clear()
    config = _cfg()
    # Start from a known-empty schema: a previous failed run can leave tables
    # behind, and a migration test that depends on leftovers proves nothing.
    engine = create_async_engine(URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    yield config
    get_settings.cache_clear()


async def _tables() -> set[str]:
    assert URL
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


async def _drift() -> list:
    assert URL
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:

            def diff(sync_conn):  # noqa: ANN001, ANN202
                ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
                return compare_metadata(ctx, Base.metadata)

            return await conn.run_sync(diff)
    finally:
        await engine.dispose()


async def test_upgrade_creates_every_table_without_drift(cfg: Config) -> None:
    await _upgrade(cfg)
    names = await _tables()
    missing = EXPECTED_TABLES - names
    assert not missing, sorted(missing)
    drift = await _drift()
    assert drift == [], [str(d)[:200] for d in drift]


async def test_roles_are_seeded_before_the_users_foreign_key(cfg: Config) -> None:
    assert URL
    await _upgrade(cfg)
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            roles = (await conn.execute(text("SELECT name, rank FROM roles ORDER BY rank"))).all()
            fks = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'users'::regclass AND contype = 'f'"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    assert [(r[0], r[1]) for r in roles] == [("user", 0), ("trader", 1), ("admin", 2)]
    assert "fk_users_role_roles" in set(fks)


async def test_round_trip_downgrade_then_upgrade(cfg: Config) -> None:
    await _upgrade(cfg)
    await _downgrade(cfg)
    leftover = (await _tables()) & EXPECTED_TABLES
    assert leftover == set(), sorted(leftover)
    await _upgrade(cfg)
    assert EXPECTED_TABLES <= await _tables()
