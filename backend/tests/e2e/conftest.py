"""Disposable PostgreSQL harness for backend production-composition E2E tests."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from katilim_analiz.config import AppEnvironment, ModelProfile, Settings
from katilim_analiz.runtime.composition import create_production_app

_TEST_DATABASE = re.compile(r"^katilim_e2e_[a-f0-9]{12}$")
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_POSTGRES_IMAGE = (
    "postgres:17-bookworm@sha256:67870dc097790edf2bd6726658db995dcc830f799d41bb2b78ef07c9a2d5f010"
)


async def _create_database(base_url: str, database_name: str) -> str:
    if not _TEST_DATABASE.fullmatch(database_name):
        raise ValueError("refusing to create a database outside the E2E naming contract")
    base = make_url(base_url)
    admin_url = base.set(database="postgres", drivername="postgresql+asyncpg")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        await engine.dispose()
    return base.set(
        database=database_name,
        drivername="postgresql+asyncpg",
    ).render_as_string(hide_password=False)


async def _drop_database(database_url: str) -> None:
    url = make_url(database_url)
    database_name = url.database or ""
    if not _TEST_DATABASE.fullmatch(database_name):
        raise ValueError("refusing to drop a database outside the E2E naming contract")
    engine = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
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


def _migrate(database_url: str) -> None:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def disposable_database_url() -> Iterator[str]:
    """Never consult a live DATABASE_URL; own an isolated container and database."""

    with PostgresContainer(_POSTGRES_IMAGE, driver="asyncpg") as postgres:
        database_name = f"katilim_e2e_{uuid4().hex[:12]}"
        database_url = asyncio.run(
            _create_database(postgres.get_connection_url(driver="asyncpg"), database_name)
        )
        try:
            _migrate(database_url)
            yield database_url
        finally:
            asyncio.run(_drop_database(database_url))


@pytest_asyncio.fixture
async def production_app(disposable_database_url: str) -> AsyncIterator[FastAPI]:
    settings = Settings(
        app_env=AppEnvironment.TEST,
        app_allowed_hosts=["testserver"],
        database_url=disposable_database_url,
        model_profile=ModelProfile.RULES_ONLY,
        ingest_network_enabled=False,
    )
    app = create_production_app(
        settings,
        frontend_dir=_BACKEND_ROOT / ".missing-e2e-frontend",
    )
    async with LifespanManager(app):
        yield app


@pytest_asyncio.fixture
async def production_client(production_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=production_app),
        base_url="http://testserver",
    ) as client:
        yield client
