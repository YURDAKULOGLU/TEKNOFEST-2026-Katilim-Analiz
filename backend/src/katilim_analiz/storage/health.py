"""PostgreSQL and Alembic health adapter for Kubernetes readiness."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from katilim_analiz.application.health import MigrationRevisions
from katilim_analiz.application.ports import DatabaseHealthPort

_UNACCENT_PROBE = "ÇĞİÖŞÜÂÎÛ çğıöşüâîû Hôtel Æ"
_UNACCENT_EXPECTED = "CGIOSUAIU cgiosuaiu Hotel AE"
_DATABASE_CONTRACT = text(
    """
    SELECT
        current_setting('server_version_num')::integer / 10000 = 17
        AND (SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm') = '1.6'
        AND (SELECT extversion FROM pg_extension WHERE extname = 'unaccent') = '1.1'
        AND public.immutable_unaccent(:probe) = :expected
    """
)


class MigrationHeadError(RuntimeError):
    """The migration directory has no single deterministic head."""


class PostgresDatabaseHealth(DatabaseHealthPort):
    def __init__(self, engine: AsyncEngine, *, alembic_config: str | Path) -> None:
        self._engine = engine
        self._alembic_config = Path(alembic_config)

    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                return bool(
                    (
                        await connection.execute(
                            _DATABASE_CONTRACT,
                            {"probe": _UNACCENT_PROBE, "expected": _UNACCENT_EXPECTED},
                        )
                    ).scalar_one()
                )
        except (SQLAlchemyError, OSError):
            return False

    async def migration_revisions(self) -> MigrationRevisions:
        head = self._head_revision()
        async with self._engine.connect() as connection:
            version_table_exists = (
                await connection.execute(
                    text("SELECT to_regclass(current_schema() || '.alembic_version')")
                )
            ).scalar_one_or_none()
            current = None
            if version_table_exists is not None:
                current = (
                    await connection.execute(
                        text("SELECT version_num FROM alembic_version LIMIT 1")
                    )
                ).scalar_one_or_none()
        return MigrationRevisions(current=current, head=head)

    def _head_revision(self) -> str:
        config = Config(str(self._alembic_config))
        heads = ScriptDirectory.from_config(config).get_heads()
        if len(heads) != 1:
            raise MigrationHeadError("Alembic must have exactly one migration head")
        return heads[0]
