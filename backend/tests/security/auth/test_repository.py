from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from katilim_analiz.auth.crypto import DUMMY_PASSWORD_PHC, sha256_text
from katilim_analiz.auth.models import AuditOutcome
from katilim_analiz.auth.repository import (
    AuthAuditRepository,
    AuthRepository,
    BootstrapConflictError,
    LoginRateRepository,
)
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.models import AuthAuditEvent, AuthSession

_TEST_DATABASE = re.compile(r"^katilim_auth_[a-f0-9]{12}$")
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_HEAD_REVISION = "f6a91c2d8e47"


async def _create_database(base_url: str) -> str:
    database_name = f"katilim_auth_{uuid4().hex[:12]}"
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
        raise ValueError("refusing to drop a database outside the auth-test contract")
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


def _config(database_url: str) -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture(scope="module")
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


@pytest.fixture(scope="module")
def migrated_database_url(postgres_base_url: str) -> Iterator[str]:
    database_url = asyncio.run(_create_database(postgres_base_url))
    command.upgrade(_config(database_url), "head")
    yield database_url
    asyncio.run(_drop_database(database_url))


@pytest_asyncio.fixture
async def database(migrated_database_url: str) -> Iterator[Database]:
    engine = create_async_engine(migrated_database_url, pool_pre_ping=True)
    value = Database(engine)
    yield value
    await value.dispose()


def test_auth_migration_is_exact_reversible_and_matches_metadata(
    postgres_base_url: str,
) -> None:
    database_url = asyncio.run(_create_database(postgres_base_url))
    config = _config(database_url)
    try:
        command.upgrade(config, "head")
        command.check(config)

        async def head_state() -> tuple[str, set[str], set[str], set[str], set[str]]:
            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    revision = str(
                        (
                            await connection.execute(
                                text("SELECT version_num FROM alembic_version")
                            )
                        ).scalar_one()
                    )
                    tables = set(
                        (
                            await connection.execute(
                                text(
                                    "SELECT tablename FROM pg_tables "
                                    "WHERE schemaname = current_schema()"
                                )
                            )
                        ).scalars()
                    )
                    columns = set(
                        (
                            await connection.execute(
                                text(
                                    "SELECT column_name FROM information_schema.columns "
                                    "WHERE table_schema = current_schema() "
                                    "AND table_name = 'auth_sessions'"
                                )
                            )
                        ).scalars()
                    )
                    constraints = set(
                        (
                            await connection.execute(
                                text(
                                    "SELECT constraint_name FROM "
                                    "information_schema.table_constraints "
                                    "WHERE table_schema = current_schema() "
                                    "AND table_name IN ('auth_sessions','auth_audit_events')"
                                )
                            )
                        ).scalars()
                    )
                    triggers = set(
                        (
                            await connection.execute(
                                text(
                                    "SELECT triggers.tgname FROM pg_trigger AS triggers "
                                    "JOIN pg_class AS tables ON tables.oid = triggers.tgrelid "
                                    "JOIN pg_namespace AS schemas "
                                    "ON schemas.oid = tables.relnamespace "
                                    "WHERE schemas.nspname = current_schema() "
                                    "AND tables.relname = 'auth_audit_events' "
                                    "AND NOT triggers.tgisinternal"
                                )
                            )
                        ).scalars()
                    )
                    return revision, tables, columns, constraints, triggers
            finally:
                await engine.dispose()

        revision, tables, columns, constraints, triggers = asyncio.run(head_state())
        assert revision == _HEAD_REVISION
        assert {
            "auth_users",
            "auth_sessions",
            "auth_rate_reservations",
            "auth_audit_events",
        } <= tables
        assert {
            "absolute_expires_at",
            "idle_expires_at",
            "last_seen_at",
            "token_hash",
            "csrf_hash",
        } <= columns
        assert "expires_at" not in columns
        assert "client_context" not in columns
        assert {
            "uq_auth_sessions_csrf_hash",
            "ck_auth_sessions_session_hashes_distinct",
            "ck_auth_sessions_session_idle_within_absolute",
            "ck_auth_audit_events_audit_username_hash_sha256",
        } <= constraints
        assert triggers == {"auth_audit_no_update_delete", "auth_audit_no_truncate"}

        command.downgrade(config, "b421f9d8c4a1")

        async def legacy_state() -> tuple[str, set[str], set[str]]:
            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    revision = str(
                        (
                            await connection.execute(
                                text("SELECT version_num FROM alembic_version")
                            )
                        ).scalar_one()
                    )
                    tables = set(
                        (
                            await connection.execute(
                                text(
                                    "SELECT tablename FROM pg_tables "
                                    "WHERE schemaname = current_schema()"
                                )
                            )
                        ).scalars()
                    )
                    columns = set(
                        (
                            await connection.execute(
                                text(
                                    "SELECT column_name FROM information_schema.columns "
                                    "WHERE table_schema = current_schema() "
                                    "AND table_name = 'auth_sessions'"
                                )
                            )
                        ).scalars()
                    )
                    return revision, tables, columns
            finally:
                await engine.dispose()

        legacy_revision, legacy_tables, legacy_columns = asyncio.run(legacy_state())
        assert legacy_revision == "b421f9d8c4a1"
        assert "auth_rate_reservations" not in legacy_tables
        assert "auth_audit_events" not in legacy_tables
        assert {"expires_at", "last_seen_at", "client_context"} <= legacy_columns
        assert "absolute_expires_at" not in legacy_columns

        command.upgrade(config, "head")
        assert asyncio.run(head_state())[0] == _HEAD_REVISION
    finally:
        asyncio.run(_drop_database(database_url))


@pytest.mark.asyncio
async def test_create_once_user_failure_state_and_success_reset(database: Database) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    async with database.transaction() as session:
        repository = AuthRepository(session)
        user_id = await repository.create_initial_user("Admin", DUMMY_PASSWORD_PHC, now=now)
        with pytest.raises(BootstrapConflictError, match="already completed"):
            await repository.create_initial_user("admin", DUMMY_PASSWORD_PHC, now=now)

    async with database.transaction() as session:
        repository = AuthRepository(session)
        state = None
        for offset in range(5):
            state = await repository.record_login_failure(
                user_id, now=now + timedelta(seconds=offset)
            )
        assert state is not None
        assert state.failed_attempts == 5
        assert state.locked_until == now + timedelta(seconds=34)

        state = await repository.record_login_failure(user_id, now=now + timedelta(seconds=5))
        assert state.failed_attempts == 6
        assert state.locked_until == now + timedelta(seconds=65)

        state = await repository.record_login_success(user_id, now=now + timedelta(minutes=2))
        assert state.failed_attempts == 0
        assert state.locked_until is None
        assert state.last_authenticated_at == now + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_session_rotation_touch_expiry_revocation_and_restart_persistence(
    database: Database,
) -> None:
    now = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
    async with database.transaction() as session:
        repository = AuthRepository(session)
        user_id = await repository.create_user(
            f"admin_{uuid4().hex[:8]}", DUMMY_PASSWORD_PHC, now=now
        )
        first = await repository.rotate_session(
            user_id=user_id,
            token_hash=sha256_text("first-session-token"),
            csrf_hash=sha256_text("first-csrf-token"),
            now=now,
        )

    async with database.transaction() as session:
        repository = AuthRepository(session)
        unchanged = await repository.touch_active_session(
            first.token_hash, now=now + timedelta(seconds=30)
        )
        assert unchanged is not None and unchanged.last_seen_at == now
        touched = await repository.touch_active_session(
            first.token_hash, now=now + timedelta(seconds=61)
        )
        assert touched is not None
        assert touched.last_seen_at == now + timedelta(seconds=61)
        assert touched.idle_expires_at == now + timedelta(minutes=30, seconds=61)

        second = await repository.rotate_session(
            user_id=user_id,
            token_hash=sha256_text("second-session-token"),
            csrf_hash=sha256_text("second-csrf-token"),
            now=now + timedelta(minutes=2),
        )

    async with database.session() as session:
        stored_first = await session.get(AuthSession, first.id)
        stored_second = await session.get(AuthSession, second.id)
        assert stored_first is not None and stored_first.revoked_at == now + timedelta(minutes=2)
        assert stored_second is not None and stored_second.client_context == {}
        assert stored_second.token_hash != "second-session-token"
        assert stored_second.csrf_hash != "second-csrf-token"

    async with database.transaction() as session:
        repository = AuthRepository(session)
        active = await repository.get_active_session(
            second.token_hash, now=now + timedelta(minutes=3)
        )
        assert active is not None and active[0].id == second.id
        assert await repository.revoke_session(second.id, now=now + timedelta(minutes=4))
        assert not await repository.revoke_session(second.id, now=now + timedelta(minutes=5))
        assert (
            await repository.get_active_session(second.token_hash, now=now + timedelta(minutes=5))
            is None
        )


@pytest.mark.asyncio
async def test_login_rate_reservations_are_durable_and_concurrency_safe(
    database: Database,
) -> None:
    now = datetime(2026, 7, 19, 14, 0, tzinfo=UTC)
    username_hash = sha256_text(f"user:{uuid4()}")
    client_hash = sha256_text(f"client:{uuid4()}")

    async def reserve() -> bool:
        async with database.session_factory.begin() as session:
            decision = await LoginRateRepository(session).reserve_login_attempt(
                username_hash=username_hash,
                client_hash=client_hash,
                now=now,
                username_limit=1,
                client_limit=1,
            )
            return decision.allowed

    first, second = await asyncio.gather(reserve(), reserve())
    assert sorted((first, second)) == [False, True]

    async with database.transaction() as session:
        persisted = await LoginRateRepository(session).reserve_login_attempt(
            username_hash=username_hash,
            client_hash=client_hash,
            now=now + timedelta(seconds=1),
            username_limit=1,
            client_limit=1,
        )
        assert not persisted.allowed
        assert persisted.username_count == persisted.client_count == 1
        assert persisted.retry_at == now + timedelta(minutes=15)

    async with database.transaction() as session:
        boundary = await LoginRateRepository(session).reserve_login_attempt(
            username_hash=username_hash,
            client_hash=client_hash,
            now=now + timedelta(minutes=15),
            username_limit=1,
            client_limit=1,
        )
        assert boundary.allowed


@pytest.mark.asyncio
async def test_audit_shape_is_redacted_persistent_and_database_append_only(
    database: Database,
) -> None:
    now = datetime(2026, 7, 19, 15, 0, tzinfo=UTC)
    username_hash = sha256_text(f"audit-user:{uuid4()}")
    client_hash = sha256_text(f"audit-client:{uuid4()}")
    async with database.transaction() as session:
        event = await AuthAuditRepository(session).append(
            occurred_at=now,
            event_type="auth.login_failed",
            outcome=AuditOutcome.FAILURE,
            username_hash=username_hash,
            client_hash=client_hash,
            reason_code="invalid_credentials",
            correlation_id=f"corr-{uuid4()}",
        )

    with pytest.raises(DBAPIError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(
                update(AuthAuditEvent)
                .where(AuthAuditEvent.id == event.id)
                .values(reason_code="locked_account")
            )

    with pytest.raises(DBAPIError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(
                text("DELETE FROM auth_audit_events WHERE id = :id"), {"id": event.id}
            )

    async with database.session() as session:
        stored = await session.get(AuthAuditEvent, event.id)
        columns = set(
            (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'auth_audit_events'"
                    )
                )
            ).scalars()
        )
    assert stored is not None
    assert stored.reason_code == "invalid_credentials"
    assert not {"password", "token", "csrf", "raw_ip", "user_agent", "payload", "details"} & columns

    async with database.transaction() as session:
        with pytest.raises(ValueError, match="approved redacted"):
            await AuthAuditRepository(session).append(
                occurred_at=now,
                event_type="auth.arbitrary_detail",
                outcome=AuditOutcome.FAILURE,
            )
