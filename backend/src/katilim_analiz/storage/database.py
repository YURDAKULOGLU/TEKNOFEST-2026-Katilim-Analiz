"""Async PostgreSQL engine and explicit transaction boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from katilim_analiz.config import Settings


class DatabaseConfigurationError(ValueError):
    """The configured database cannot satisfy the PostgreSQL async contract."""


def validated_asyncpg_url(value: str) -> URL:
    """Validate that application persistence uses only SQLAlchemy asyncpg URLs."""

    try:
        url = make_url(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseConfigurationError("database URL is invalid") from exc
    if url.drivername != "postgresql+asyncpg":
        raise DatabaseConfigurationError("database URL must use the postgresql+asyncpg driver")
    if not url.database:
        raise DatabaseConfigurationError("database URL must name a database")
    return url


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the shared, health-checked async engine without opening a connection."""

    url = validated_asyncpg_url(settings.sqlalchemy_url())
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_recycle=1_800,
    )


class Database:
    """Own an engine and expose deliberately short session/transaction scopes."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(create_engine(settings))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a read-oriented session; pending work is rolled back on close."""

        async with self.session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield one explicit transaction and commit only on normal completion."""

        async with self.session_factory.begin() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
