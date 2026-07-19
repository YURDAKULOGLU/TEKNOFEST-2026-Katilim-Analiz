"""Alembic environment using asyncpg and a cluster-wide migration lock."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from katilim_analiz.storage import models as storage_models  # noqa: F401
from katilim_analiz.storage.base import Base
from katilim_analiz.storage.database import validated_asyncpg_url

_MIGRATION_LOCK_ID = 4_907_040_2026

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = (
    config.attributes.get("database_url")
    or os.environ.get("MIGRATION_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or config.get_main_option("sqlalchemy.url")
)
validated_asyncpg_url(database_url)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": _MIGRATION_LOCK_ID}
        )
        # pg_advisory_lock is session-scoped. Commit the implicit SQLAlchemy
        # transaction so Alembic can own and commit its DDL transaction.
        await connection.commit()
        try:
            await connection.run_sync(_run_migrations)
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            )
            await connection.commit()
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
