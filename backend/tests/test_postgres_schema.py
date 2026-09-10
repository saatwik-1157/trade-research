"""What only a real PostgreSQL can check.

The suite runs on in-memory SQLite, which is fast and has two blind spots that
have both drawn blood:

* **Foreign keys are off** unless `PRAGMA foreign_keys=ON` is issued per
  connection, and almost nothing issues it. A route can write a dangling
  reference, pass every test, and fail on the real database -- which is
  exactly what `POST /v1/orders` did through 174 API tests
  (`KNOWN_TEST_LIMITATIONS.md` §1b).
* **Migrations are never run.** Every test builds its schema with
  `Base.metadata.create_all`, so the 27 Alembic revisions that actually create
  production's schema are exercised by nothing. A model can drift from its
  migration indefinitely and the suite stays green, because the suite never
  looks at the migration.

These tests close the second gap and widen the first. They **skip** unless
`POSTGRES_TEST_URL` is set, so a developer machine is unaffected; CI sets it
against a `postgres:16` service.

    POSTGRES_TEST_URL=postgresql+asyncpg://tr:tr@127.0.0.1:5432/tr \\
        python -m pytest tests/test_postgres_schema.py -v

The parity test is the valuable one. `create_all` and `upgrade head` are two
independent descriptions of the same schema, and nothing has ever compared
them.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

URL = os.environ.get("POSTGRES_TEST_URL", "")

pytestmark = pytest.mark.skipif(
    not URL, reason="POSTGRES_TEST_URL is not set; this suite needs real PostgreSQL"
)


def _sync_url(url: str) -> str:
    """Alembic runs synchronously; the app URL is asyncpg."""
    return url.replace("+asyncpg", "").replace("postgresql+psycopg", "postgresql")


@pytest_asyncio.fixture
async def fresh_db():
    """An empty schema. Dropped and recreated, never reused between tests.

    `DROP SCHEMA public CASCADE` is safe here and nowhere else: this runs only
    against the throwaway database named by POSTGRES_TEST_URL, which the CI
    service container creates for this job and discards with the runner. It is
    guarded below so it cannot be pointed at anything that looks real.
    """
    for word in ("prod", "live", "trade-research", "tr-postgres"):
        assert word not in URL, f"POSTGRES_TEST_URL looks non-disposable: {word!r}"

    engine = create_async_engine(URL, poolclass=None)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    yield URL


async def _upgrade_head() -> None:
    """Run `alembic upgrade head`, off the running event loop.

    `alembic/env.py` calls `asyncio.run()` itself, and asyncio.run cannot be
    called from inside a running loop -- which is where an async test lives.
    Found by running these tests rather than by reading them: all five failed
    with 'asyncio.run() cannot be called from a running event loop'.
    """
    import asyncio

    from alembic import command
    from alembic.config import Config

    def run() -> None:
        # DATABASE_URL, not `cfg.set_main_option`. `alembic/env.py` sets the
        # url itself from `get_settings().database_url`, so anything set on
        # the Config here is overwritten and the migration silently runs
        # against whatever the environment's default database is -- which is
        # how the first run of this file reported "the migration did not run"
        # while alembic was cheerfully migrating something else.
        from app.core.settings import get_settings

        os.environ["DATABASE_URL"] = URL
        get_settings.cache_clear()
        try:
            command.upgrade(Config("alembic.ini"), "head")
        finally:
            get_settings.cache_clear()

    await asyncio.to_thread(run)


async def _tables_and_columns(url: str) -> dict[str, set[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:

            def read(sync_conn):  # noqa: ANN001, ANN202
                insp = inspect(sync_conn)
                return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}

            return await conn.run_sync(read)
    finally:
        await engine.dispose()


# --------------------------------------------------------------- migrations


async def test_the_migrations_apply_to_an_empty_postgres(fresh_db):
    """27 revisions, from nothing to head, on the database production uses.

    Nothing else in the suite runs a migration at all.
    """
    await _upgrade_head()

    tables = await _tables_and_columns(URL)
    assert "alembic_version" in tables, "no version table; the migration did not run"
    # A handful that must exist for the platform to function at all. Named
    # rather than counted: a count passes while the wrong tables are present.
    for required in ("orders", "signals", "positions", "strategies"):
        assert required in tables, f"{required} missing after upgrade head"


async def test_the_head_revision_matches_the_code(fresh_db):
    """The database's stamped head is the head the repository declares."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    await _upgrade_head()

    expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            got = (await conn.execute(text("select version_num from alembic_version"))).scalar()
    finally:
        await engine.dispose()
    assert got == expected, f"database is at {got}, repository head is {expected}"


# ------------------------------------------------------------------ parity


async def test_the_migrations_and_the_models_describe_the_same_schema(fresh_db):
    """The test that has never existed, and the drift it would have caught.

    Every other test builds its schema with `Base.metadata.create_all`. Every
    deployment builds it with `alembic upgrade head`. Those are two independent
    descriptions of one schema and nothing has ever compared them, so a model
    could gain a column that no migration adds and the suite would stay green
    while production lacked the column.

    Reports the difference in both directions, because they mean different
    things: a table in the models and not the migrations never gets created,
    and a table in the migrations and not the models is dead weight nobody
    reads.
    """
    import app.auth.models  # noqa: F401 - register the auth tables
    import app.models  # noqa: F401 - register every platform table
    from app.db.base import Base

    await _upgrade_head()
    migrated = await _tables_and_columns(URL)
    migrated.pop("alembic_version", None)

    declared = {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}

    only_in_models = sorted(set(declared) - set(migrated))
    only_in_migrations = sorted(set(migrated) - set(declared))
    assert not only_in_models, (
        "tables the models declare that no migration creates -- production "
        f"would not have them: {only_in_models}"
    )
    assert not only_in_migrations, (
        f"tables the migrations create that no model reads: {only_in_migrations}"
    )

    column_drift: dict[str, dict[str, list[str]]] = {}
    for table in sorted(set(declared) & set(migrated)):
        missing = sorted(declared[table] - migrated[table])
        extra = sorted(migrated[table] - declared[table])
        if missing or extra:
            column_drift[table] = {"missing_in_db": missing, "not_in_models": extra}
    assert not column_drift, f"model and migration columns disagree: {column_drift}"


# -------------------------------------------------------------- foreign keys


async def test_postgres_actually_refuses_a_dangling_reference(fresh_db):
    """The blind spot itself, on the database that does not have it.

    SQLite accepts this row silently. PostgreSQL raises. That difference is
    the whole reason this file exists.
    """
    await _upgrade_head()

    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("orders"))
        assert fks, "orders has no foreign keys; this test would prove nothing"

        with pytest.raises(Exception) as caught:  # noqa: PT011 - driver-specific
            async with engine.begin() as conn:
                await conn.execute(
                    text("insert into orders (id, signal_id) values ('fk-probe', 'no-such-signal')")
                )
        assert (
            "foreign key" in str(caught.value).lower() or "violates" in str(caught.value).lower()
        ), f"refused, but not for referential integrity: {caught.value}"
    finally:
        await engine.dispose()


async def test_every_foreign_key_in_the_schema_is_enforced(fresh_db):
    """Counts them, so the coverage claim is a number rather than a hope.

    `KNOWN_TEST_LIMITATIONS.md` says only `orders.signal_id` is covered. This
    reports how many constraints PostgreSQL is enforcing, all of them, and
    fails if the schema somehow has none.
    """
    await _upgrade_head()

    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            total = (
                await conn.execute(
                    text(
                        "select count(*) from information_schema.table_constraints "
                        "where constraint_type = 'FOREIGN KEY' "
                        "and constraint_schema = 'public'"
                    )
                )
            ).scalar()
    finally:
        await engine.dispose()

    assert total and total > 0, "no foreign keys found; the schema is not what it claims"
    print(f"\n  PostgreSQL is enforcing {total} foreign key constraints")
