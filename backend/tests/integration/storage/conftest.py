from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from katilim_analiz.storage.database import Database

_TEST_DATABASE = re.compile(r"^katilim_test_[a-f0-9]{12}$")
_BACKEND_ROOT = Path(__file__).resolve().parents[3]


async def _create_database(base_url: str, database_name: str) -> str:
    if not _TEST_DATABASE.fullmatch(database_name):
        raise ValueError("refusing to create a database outside the test naming contract")
    base = make_url(base_url)
    admin_url = base.set(database="postgres", drivername="postgresql+asyncpg")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        await engine.dispose()
    return base.set(database=database_name, drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


async def _drop_database(database_url: str) -> None:
    url = make_url(database_url)
    database_name = url.database or ""
    if not _TEST_DATABASE.fullmatch(database_name):
        raise ValueError("refusing to drop a database outside the test naming contract")
    admin_url = url.set(database="postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE "{database_name}"'))
    finally:
        await engine.dispose()


def _run_migration(database_url: str, revision: str) -> None:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, revision) if revision != "base" else command.downgrade(config, "base")


@pytest.fixture(scope="session")
def postgres_base_url() -> Iterator[str]:
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        url: URL = make_url(configured)
        yield url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        return

    with PostgresContainer(
        "postgres:17-bookworm@sha256:67870dc097790edf2bd6726658db995dcc830f799d41bb2b78ef07c9a2d5f010",
        driver="asyncpg",
    ) as postgres:
        yield postgres.get_connection_url(driver="asyncpg")


@pytest.fixture(scope="session")
def migrated_database_url(postgres_base_url: str) -> Iterator[str]:
    database_name = f"katilim_test_{uuid4().hex[:12]}"
    database_url = asyncio.run(_create_database(postgres_base_url, database_name))
    _run_migration(database_url, "head")
    yield database_url
    asyncio.run(_drop_database(database_url))


@pytest.fixture
def empty_database_url(postgres_base_url: str) -> Iterator[str]:
    database_name = f"katilim_test_{uuid4().hex[:12]}"
    database_url = asyncio.run(_create_database(postgres_base_url, database_name))
    yield database_url
    asyncio.run(_drop_database(database_url))


@pytest_asyncio.fixture
async def database(migrated_database_url: str) -> Iterator[Database]:
    engine = create_async_engine(migrated_database_url, pool_pre_ping=True)
    database = Database(engine)
    yield database
    await database.dispose()
