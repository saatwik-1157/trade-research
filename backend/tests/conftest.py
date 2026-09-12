from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.settings import Settings  # noqa: E402

# Environment variables that would change what the tests are measuring. They
# are cleared so a developer's shell cannot make a "live" test pass or fail.
_MODE_VARS = ("ENVIRONMENT", "TRADING_MODE", "LIVE_TRADING", "DATABASE_URL", "REDIS_URL")


@pytest.fixture(autouse=True)
def _clean_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _MODE_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: the test never reads a developer's .env
    return Settings(_env_file=None)


# --------------------------------------------------------- referential integrity
#
# SQLite enforces no foreign key unless it is asked to, PER CONNECTION, and it
# was never asked. So the suite wrote rows pointing at parents that did not
# exist and called it a pass -- ~3,000 tests that could not fail the one way a
# relational bug usually shows up. The schema side is proven against a real
# PostgreSQL in `test_postgres_schema.py`; this is the behavioural side, where
# application code writes through a session.
#
# ONE listener rather than 121 edits. The suite builds 121 engines across 51
# files, each with its own `create_async_engine("sqlite+aiosqlite://")`, and a
# rule that has to be repeated 121 times is a rule that will be missed the
# 122nd. `Engine.connect` fires for the sync engine an async one wraps, so this
# reaches every session the tests open, including ones added later.
@event.listens_for(Engine, "connect")
def _sqlite_enforces_foreign_keys(dbapi_connection, connection_record):
    # Identified by the driver's own module, because asyncpg reaches this same
    # hook in the Postgres job and `PRAGMA` there is a syntax error.
    driver = type(dbapi_connection)
    if "sqlite" not in f"{driver.__module__}.{driver.__name__}".lower():
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


# ------------------------------------------------------------ the roles table
#
# `users.role` is a ForeignKey to `roles.name`, and migration 0002 seeds the
# three roles BEFORE adding that constraint -- its author wrote down why the
# order matters. The tests never run that migration: they build the schema with
# `Base.metadata.create_all()`, which creates tables and no rows. So `roles` was
# empty in every test, and with foreign keys off nobody could tell.
#
# Turning the pragma on made that visible immediately: every `INSERT INTO users`
# failed, because `role='user'` pointed at a row that did not exist. The
# production path is not affected and never was -- this is the fixture catching
# up with the migration.
#
# Seeded from `ROLE_SEED`, the same constant the migration's rows were written
# from, so the two cannot drift apart. Fired on `after_create` so it reaches all
# 121 engines the suite builds without a line in any of them.
def _seed_roles(target, connection, **kw):
    from app.models.accounts import ROLE_SEED

    connection.execute(target.insert(), ROLE_SEED)


def _install_role_seed() -> None:
    from app.models.accounts import RoleRow

    event.listen(RoleRow.__table__, "after_create", _seed_roles)


_install_role_seed()
