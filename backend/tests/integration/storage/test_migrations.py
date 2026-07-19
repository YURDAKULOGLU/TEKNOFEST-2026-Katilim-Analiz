from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_CANDIDATE_HASH_REVISION = "f6a91c2d8e47"


def _config(database_url: str) -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


async def _schema_state(
    database_url: str,
) -> tuple[set[str], dict[str, str], str | None, int]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
                    )
                ).scalars()
            )
            extensions = dict(
                (
                    await connection.execute(text("SELECT extname, extversion FROM pg_extension"))
                ).all()
            )
            server_major = int(
                (
                    await connection.execute(
                        text("SELECT current_setting('server_version_num')::integer / 10000")
                    )
                ).scalar_one()
            )
            revision = None
            if "alembic_version" in tables:
                revision = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
            return tables, extensions, revision, server_major
    finally:
        await engine.dispose()


def test_upgrade_downgrade_upgrade_is_reversible(empty_database_url: str) -> None:
    config = _config(empty_database_url)

    command.upgrade(config, "head")
    tables, extensions, revision, server_major = asyncio.run(_schema_state(empty_database_url))
    assert revision == _CANDIDATE_HASH_REVISION
    assert server_major == 17
    assert {
        "sources",
        "campaign_records",
        "campaign_observations",
        "monitored_campaign_targets",
        "monitored_source_states",
        "durable_jobs",
        "auth_sessions",
    } <= tables
    assert extensions["pg_trgm"] == "1.6"
    assert extensions["unaccent"] == "1.1"

    command.downgrade(config, "base")
    tables, extensions, revision, _ = asyncio.run(_schema_state(empty_database_url))
    assert revision is None
    assert "campaign_records" not in tables
    assert {"pg_trgm", "unaccent"} <= extensions.keys()

    command.upgrade(config, "head")
    tables, _, revision, _ = asyncio.run(_schema_state(empty_database_url))
    assert revision == _CANDIDATE_HASH_REVISION
    assert "campaign_records" in tables


async def _insert_duplicate_campaign_hashes(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sources "
                    "(id, registry_version, listing_order, legal_name, homepage_url, "
                    "allowed_hosts, digital_bank, active) VALUES "
                    "('migration-bank', 'migration-test', 1, 'Migration Bank', "
                    "'https://migration.example', '[\"migration.example\"]'::jsonb, false, true)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO clean_documents "
                    "(id, source_id, canonical_url, title, clean_sha256, language) VALUES "
                    "('clean:migration', 'migration-bank', 'https://migration.example/campaign', "
                    "'Migration Campaign', :clean_sha256, 'tr')"
                ),
                {"clean_sha256": "c" * 64},
            )
            for version in (1, 2):
                await connection.execute(
                    text(
                        "INSERT INTO campaign_records "
                        "(id, campaign_key, version, source_document_id, bank_id, observed_at, "
                        "title, product_family, campaign_type, data, extraction, status, "
                        "validation_issues, record_sha256, payload_sha256) VALUES "
                        "(:id, 'migration-bank:campaign', :version, 'clean:migration', "
                        "'migration-bank', now(), 'Migration Campaign', 'unknown', 'unknown', "
                        "'{}'::jsonb, '{}'::jsonb, 'validated', '[]'::jsonb, "
                        ":record_sha256, :payload_sha256)"
                    ),
                    {
                        "id": f"record:migration:{version}",
                        "version": version,
                        "record_sha256": "a" * 64,
                        "payload_sha256": "b" * 64,
                    },
                )
    finally:
        await engine.dispose()


async def _migration_revision_and_duplicate_count(database_url: str) -> tuple[str, int]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM campaign_records "
                        "WHERE campaign_key = 'migration-bank:campaign'"
                    )
                )
            ).scalar_one()
            return revision, int(count)
    finally:
        await engine.dispose()


async def _insert_one_legacy_campaign(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sources "
                    "(id, registry_version, listing_order, legal_name, homepage_url, "
                    "allowed_hosts, digital_bank, active) VALUES "
                    "('legacy-bank', 'legacy-test', 1, 'Legacy Bank', "
                    "'https://legacy.example', '[\"legacy.example\"]'::jsonb, false, true)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO clean_documents "
                    "(id, source_id, canonical_url, title, clean_sha256, language) VALUES "
                    "('clean:legacy', 'legacy-bank', 'https://legacy.example/campaign', "
                    "'Legacy Campaign', :clean_sha256, 'tr')"
                ),
                {"clean_sha256": "c" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO campaign_records "
                    "(id, campaign_key, version, source_document_id, bank_id, observed_at, "
                    "title, product_family, campaign_type, data, extraction, status, "
                    "validation_issues, record_sha256, payload_sha256) VALUES "
                    "('record:legacy', 'legacy-bank:campaign', 1, 'clean:legacy', "
                    "'legacy-bank', now(), 'Legacy Campaign', 'unknown', 'unknown', "
                    "'{}'::jsonb, '{}'::jsonb, 'validated', '[]'::jsonb, "
                    ":record_sha256, :payload_sha256)"
                ),
                {"record_sha256": "a" * 64, "payload_sha256": "b" * 64},
            )
    finally:
        await engine.dispose()


async def _legacy_observation(database_url: str) -> tuple[str, str, str, str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT scan_run_id, record_id, clean_sha256, record_sha256 "
                        "FROM campaign_observations "
                        "WHERE campaign_key = 'legacy-bank:campaign'"
                    )
                )
            ).one()
            return str(row[0]), str(row[1]), str(row[2]), str(row[3])
    finally:
        await engine.dispose()


def test_downgrade_fails_closed_when_legacy_hash_uniques_are_impossible(
    empty_database_url: str,
) -> None:
    config = _config(empty_database_url)
    command.upgrade(config, "head")
    asyncio.run(_insert_duplicate_campaign_hashes(empty_database_url))

    with pytest.raises(IntegrityError, match="WP-111 downgrade refused"):
        command.downgrade(config, "a8575e796d75")

    revision, duplicate_count = asyncio.run(
        _migration_revision_and_duplicate_count(empty_database_url)
    )
    assert revision == _CANDIDATE_HASH_REVISION
    assert duplicate_count == 2


def test_existing_records_are_backfilled_deterministically_across_reupgrade(
    empty_database_url: str,
) -> None:
    config = _config(empty_database_url)
    command.upgrade(config, "a8575e796d75")
    asyncio.run(_insert_one_legacy_campaign(empty_database_url))

    command.upgrade(config, "head")
    first = asyncio.run(_legacy_observation(empty_database_url))
    command.downgrade(config, "a8575e796d75")
    command.upgrade(config, "head")
    second = asyncio.run(_legacy_observation(empty_database_url))

    assert first == second
    assert first[0].startswith("legacy-record:")
    assert first[1:] == ("record:legacy", "c" * 64, "a" * 64)


def test_alembic_metadata_has_no_pending_schema_changes(migrated_database_url: str) -> None:
    command.check(_config(migrated_database_url))


async def test_postgresql_specific_types_and_indexes_are_installed(database) -> None:  # type: ignore[no-untyped-def]
    async with database.session() as session:
        indexes = set(
            (
                await session.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
                )
            ).scalars()
        )
        types = dict(
            (
                await session.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'campaign_records'"
                    )
                )
            ).all()
        )
        observation_columns = {
            name: (data_type, maximum_length)
            for name, data_type, maximum_length in (
                await session.execute(
                    text(
                        "SELECT column_name, data_type, character_maximum_length "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'campaign_observations'"
                    )
                )
            ).all()
        }
        observation_constraints = set(
            (
                await session.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'campaign_observations'"
                    )
                )
            ).scalars()
        )
        campaign_constraints = set(
            (
                await session.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'campaign_records'"
                    )
                )
            ).scalars()
        )
        index_definitions = dict(
            (
                await session.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = current_schema()"
                    )
                )
            ).all()
        )

    assert types["data"] == "jsonb"
    assert types["rate_min"] == "numeric"
    assert types["search_vector"] == "tsvector"
    assert {
        "ix_campaign_records_search_vector",
        "ix_campaign_records_title_unaccent_trgm",
        "ix_campaign_records_data_jsonb",
        "ix_campaign_records_logical_latest",
        "ix_campaign_records_key_record_hash",
        "ix_campaign_records_key_payload_hash",
        "ix_campaign_observations_campaign_observed",
        "ix_campaign_observations_record_id",
        "ix_durable_jobs_claim",
    } <= indexes
    assert observation_columns == {
        "id": ("bigint", None),
        "campaign_key": ("character varying", 191),
        "observation_key": ("character varying", 64),
        "scan_run_id": ("character varying", 191),
        "record_id": ("character varying", 191),
        "clean_sha256": ("character varying", 64),
        "record_sha256": ("character varying", 64),
        "observed_at": ("timestamp with time zone", None),
        "created_at": ("timestamp with time zone", None),
    }
    assert {
        "uq_campaign_observations_key_observation",
        "fk_campaign_observations_record_id_campaign_records",
    } <= observation_constraints
    assert "uq_campaign_records_key_version" in campaign_constraints
    assert "uq_campaign_records_key_record_hash" not in campaign_constraints
    assert "uq_campaign_records_key_payload_hash" not in campaign_constraints
    assert " UNIQUE " not in index_definitions["ix_campaign_records_key_record_hash"]
    assert " UNIQUE " not in index_definitions["ix_campaign_records_key_payload_hash"]


async def _insert_legacy_outbox_rows(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for identifier, created_at_text in (
                ("00000000-0000-0000-0000-000000000002", "2026-07-19T10:00:00+00:00"),
                ("00000000-0000-0000-0000-000000000001", "2026-07-19T10:00:00+00:00"),
                ("00000000-0000-0000-0000-000000000003", "2026-07-19T11:00:00+00:00"),
            ):
                created_at = datetime.fromisoformat(created_at_text)
                await connection.execute(
                    text(
                        "INSERT INTO outbox_events "
                        "(id, topic, event_type, aggregate_type, aggregate_id, payload, "
                        "dedupe_key, occurred_at, available_at, attempts, max_attempts, "
                        "created_at) "
                        "VALUES (:id, 'legacy-topic', 'legacy-event', 'legacy', :aggregate_id, "
                        "'{}'::jsonb, :dedupe_key, :created_at, :created_at, 0, 10, :created_at)"
                    ),
                    {
                        "id": identifier,
                        "aggregate_id": f"legacy:{identifier}",
                        "dedupe_key": f"legacy:{identifier}",
                        "created_at": created_at,
                    },
                )
    finally:
        await engine.dispose()


async def _outbox_feed_schema_state(
    database_url: str,
) -> tuple[list[tuple[str, int]], tuple[int, bool], tuple[str, str], set[str], set[str]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = [
                (str(identifier), int(sequence))
                for identifier, sequence in (
                    await connection.execute(
                        text("SELECT id, feed_sequence FROM outbox_events ORDER BY feed_sequence")
                    )
                ).all()
            ]
            sequence = (
                await connection.execute(
                    text(
                        "SELECT cache_size, cycle FROM pg_sequences "
                        "WHERE schemaname = current_schema() "
                        "AND sequencename = 'outbox_feed_sequence'"
                    )
                )
            ).one()
            column = (
                await connection.execute(
                    text(
                        "SELECT data_type, is_nullable FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'outbox_events' AND column_name = 'feed_sequence'"
                    )
                )
            ).one()
            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT constraint_name FROM information_schema.table_constraints "
                            "WHERE table_schema = current_schema() "
                            "AND table_name = 'outbox_events' "
                            "AND constraint_name NOT LIKE '%\\_not\\_null' ESCAPE '\\'"
                        )
                    )
                ).scalars()
            )
            indexes = set(
                (
                    await connection.execute(
                        text(
                            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                            "AND tablename = 'outbox_events'"
                        )
                    )
                ).scalars()
            )
            return (
                rows,
                (int(sequence[0]), bool(sequence[1])),
                (str(column[0]), str(column[1])),
                constraints,
                indexes,
            )
    finally:
        await engine.dispose()


def test_outbox_feed_backfill_is_deterministic_and_sequence_is_strict(
    empty_database_url: str,
) -> None:
    config = _config(empty_database_url)
    command.upgrade(config, "c752a0f68d91")
    asyncio.run(_insert_legacy_outbox_rows(empty_database_url))

    command.upgrade(config, "head")
    first = asyncio.run(_outbox_feed_schema_state(empty_database_url))
    command.downgrade(config, "c752a0f68d91")
    command.upgrade(config, "head")
    second = asyncio.run(_outbox_feed_schema_state(empty_database_url))

    expected_rows = [
        ("00000000-0000-0000-0000-000000000001", 1),
        ("00000000-0000-0000-0000-000000000002", 2),
        ("00000000-0000-0000-0000-000000000003", 3),
    ]
    assert first == second
    assert first[0] == expected_rows
    assert first[1] == (1, False)
    assert first[2] == ("bigint", "NO")
    assert "uq_outbox_events_feed_sequence" in first[3]
    assert "ix_outbox_events_notification_feed" in first[4]


async def _insert_positioned_outbox_row(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(id, feed_sequence, topic, event_type, aggregate_type, aggregate_id, payload, "
                    "dedupe_key, occurred_at, available_at, attempts, max_attempts) VALUES "
                    "('00000000-0000-0000-0000-000000000099', "
                    "nextval('outbox_feed_sequence'::regclass), 'test', 'test', 'test', "
                    "'test', '{}'::jsonb, "
                    "'positioned:99', now(), now(), 0, 10)"
                )
            )
    finally:
        await engine.dispose()


async def _migration_revision(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return str(
                (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
            )
    finally:
        await engine.dispose()


def test_outbox_feed_downgrade_fails_closed_after_sequence_allocation(
    empty_database_url: str,
) -> None:
    config = _config(empty_database_url)
    command.upgrade(config, "head")
    asyncio.run(_insert_positioned_outbox_row(empty_database_url))

    with pytest.raises(IntegrityError, match="outbox feed downgrade refused"):
        command.downgrade(config, "c752a0f68d91")

    assert asyncio.run(_migration_revision(empty_database_url)) == _CANDIDATE_HASH_REVISION


async def _insert_candidate_hash_rows(
    database_url: str,
    rows: list[tuple[str, str]],
) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sources "
                    "(id, registry_version, listing_order, legal_name, homepage_url, "
                    "allowed_hosts, digital_bank, active) VALUES "
                    "('candidate-migration-bank', 'candidate-migration-test', 1, "
                    "'Candidate Migration Bank', 'https://candidate-migration.example', "
                    "'[\"candidate-migration.example\"]'::jsonb, false, true)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO clean_documents "
                    "(id, source_id, canonical_url, title, clean_sha256, language) VALUES "
                    "('clean:candidate-migration', 'candidate-migration-bank', "
                    "'https://candidate-migration.example/campaign', "
                    "'Candidate Migration Campaign', :clean_sha256, 'tr')"
                ),
                {"clean_sha256": "c" * 64},
            )
            for identifier, candidate_hash in rows:
                await connection.execute(
                    text(
                        "INSERT INTO extraction_candidates "
                        "(id, source_document_id, bank_id, data, method, extractor_version, "
                        "schema_version, started_at, completed_at, issues, candidate_sha256) "
                        "VALUES (:id, 'clean:candidate-migration', 'candidate-migration-bank', "
                        "'{}'::jsonb, 'rule', 'legacy-rules/1', 'campaign-data/1.0', "
                        "now(), now(), '[]'::jsonb, :candidate_sha256)"
                    ),
                    {"id": identifier, "candidate_sha256": candidate_hash},
                )
    finally:
        await engine.dispose()


async def _candidate_hash_rows(database_url: str) -> list[tuple[str, str]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return [
                (str(identifier), str(candidate_hash))
                for identifier, candidate_hash in (
                    await connection.execute(
                        text("SELECT id, candidate_sha256 FROM extraction_candidates ORDER BY id")
                    )
                ).all()
            ]
    finally:
        await engine.dispose()


def test_candidate_identity_hash_backfill_preserves_arbitrary_legacy_ids(
    empty_database_url: str,
) -> None:
    config = _config(empty_database_url)
    command.upgrade(config, "d4e8f6a1b203")
    deterministic_a = f"candidate:{'a' * 64}"
    deterministic_b = f"candidate:{'b' * 64}"
    arbitrary_legacy = "candidate:legacy-fixture"
    asyncio.run(
        _insert_candidate_hash_rows(
            empty_database_url,
            [
                (deterministic_a, "1" * 64),
                (deterministic_b, "b" * 64),
                (arbitrary_legacy, "2" * 64),
            ],
        )
    )

    command.upgrade(config, "head")

    assert asyncio.run(_candidate_hash_rows(empty_database_url)) == sorted(
        [
            (arbitrary_legacy, "2" * 64),
            (deterministic_a, "a" * 64),
            (deterministic_b, "b" * 64),
        ]
    )
    assert asyncio.run(_migration_revision(empty_database_url)) == _CANDIDATE_HASH_REVISION


def test_candidate_identity_hash_backfill_fails_closed_on_target_conflict(
    empty_database_url: str,
) -> None:
    config = _config(empty_database_url)
    command.upgrade(config, "d4e8f6a1b203")
    deterministic = f"candidate:{'a' * 64}"
    conflicting_legacy = "candidate:legacy-conflict"
    original_rows = [
        (deterministic, "1" * 64),
        (conflicting_legacy, "a" * 64),
    ]
    asyncio.run(_insert_candidate_hash_rows(empty_database_url, original_rows))

    with pytest.raises(IntegrityError, match="candidate hash target is already in use"):
        command.upgrade(config, "head")

    assert asyncio.run(_candidate_hash_rows(empty_database_url)) == sorted(original_rows)
    assert asyncio.run(_migration_revision(empty_database_url)) == "d4e8f6a1b203"


def test_candidate_identity_hash_backfill_fails_closed_on_malformed_digest_id(
    empty_database_url: str,
) -> None:
    config = _config(empty_database_url)
    command.upgrade(config, "d4e8f6a1b203")
    malformed = f"candidate:{'A' * 64}"
    asyncio.run(_insert_candidate_hash_rows(empty_database_url, [(malformed, "1" * 64)]))

    with pytest.raises(IntegrityError, match="malformed deterministic candidate identity"):
        command.upgrade(config, "head")

    assert asyncio.run(_candidate_hash_rows(empty_database_url)) == [(malformed, "1" * 64)]
    assert asyncio.run(_migration_revision(empty_database_url)) == "d4e8f6a1b203"
