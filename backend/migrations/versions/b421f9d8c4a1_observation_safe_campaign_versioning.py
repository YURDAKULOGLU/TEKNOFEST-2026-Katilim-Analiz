"""observation-safe campaign versioning and monitored sources

Revision ID: b421f9d8c4a1
Revises: a8575e796d75
Create Date: 2026-07-19 12:00:00.000000
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "b421f9d8c4a1"
down_revision: str | Sequence[str] | None = "a8575e796d75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitored_campaign_targets",
        sa.Column("id", sa.String(length=191), nullable=False),
        sa.Column("bank_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_key", sa.String(length=191), nullable=False),
        sa.Column("canonical_url", sa.String(length=2_048), nullable=False),
        sa.Column("discovered_from", sa.String(length=2_048), nullable=False),
        sa.Column("registry_version", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name=op.f("ck_monitored_campaign_targets_target_seen_times_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["bank_id"],
            ["sources.id"],
            name=op.f("fk_monitored_campaign_targets_bank_id_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitored_campaign_targets")),
        sa.UniqueConstraint(
            "bank_id",
            "canonical_url",
            name="uq_monitored_campaign_targets_bank_url",
        ),
        sa.UniqueConstraint(
            "campaign_key",
            name="uq_monitored_campaign_targets_campaign_key",
        ),
    )
    op.create_index(
        "ix_monitored_campaign_targets_active_bank_url",
        "monitored_campaign_targets",
        ["bank_id", "active", "canonical_url"],
        unique=False,
    )
    op.create_index(
        "ix_monitored_campaign_targets_discovered_from",
        "monitored_campaign_targets",
        ["discovered_from"],
        unique=False,
    )
    op.create_table(
        "monitored_source_states",
        sa.Column("bank_id", sa.String(length=64), nullable=False),
        sa.Column("index_url", sa.String(length=2_048), nullable=False),
        sa.Column("registry_version", sa.String(length=32), nullable=False),
        sa.Column("last_content_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_status IN ('success','not_modified','blocked','failed')",
            name=op.f("ck_monitored_source_states_monitored_source_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["bank_id"],
            ["sources.id"],
            name=op.f("fk_monitored_source_states_bank_id_sources"),
        ),
        sa.PrimaryKeyConstraint(
            "bank_id",
            "index_url",
            name=op.f("pk_monitored_source_states"),
        ),
    )
    op.create_index(
        "ix_monitored_source_states_observed",
        "monitored_source_states",
        ["last_observed_at", "bank_id"],
        unique=False,
    )

    op.drop_constraint(
        "uq_campaign_records_key_record_hash",
        "campaign_records",
        type_="unique",
    )
    op.drop_constraint(
        "uq_campaign_records_key_payload_hash",
        "campaign_records",
        type_="unique",
    )
    op.create_index(
        "ix_campaign_records_key_record_hash",
        "campaign_records",
        ["campaign_key", "record_sha256"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_records_key_payload_hash",
        "campaign_records",
        ["campaign_key", "payload_sha256"],
        unique=False,
    )
    op.create_table(
        "campaign_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("campaign_key", sa.String(length=191), nullable=False),
        sa.Column("observation_key", sa.String(length=64), nullable=False),
        sa.Column("scan_run_id", sa.String(length=191), nullable=False),
        sa.Column("record_id", sa.String(length=191), nullable=False),
        sa.Column("clean_sha256", sa.String(length=64), nullable=False),
        sa.Column("record_sha256", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["campaign_records.id"],
            name=op.f("fk_campaign_observations_record_id_campaign_records"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_observations")),
        sa.UniqueConstraint(
            "campaign_key",
            "observation_key",
            name="uq_campaign_observations_key_observation",
        ),
    )
    op.create_index(
        "ix_campaign_observations_campaign_observed",
        "campaign_observations",
        ["campaign_key", "observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_observations_record_id",
        "campaign_observations",
        ["record_id"],
        unique=False,
    )
    _backfill_campaign_observations()


def _backfill_campaign_observations() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT records.id AS record_id, records.campaign_key, records.record_sha256, "
            "records.observed_at, documents.clean_sha256 "
            "FROM campaign_records AS records "
            "JOIN clean_documents AS documents ON documents.id = records.source_document_id "
            "ORDER BY records.campaign_key, records.version, records.id"
        )
    ).mappings()
    parameters: list[dict[str, Any]] = []
    for row in rows:
        record_id = str(row["record_id"])
        campaign_key = str(row["campaign_key"])
        scan_run_id = f"legacy-record:{hashlib.sha256(record_id.encode()).hexdigest()}"
        material = f"{scan_run_id}\0{campaign_key}\0{record_id}"
        parameters.append(
            {
                "campaign_key": campaign_key,
                "observation_key": hashlib.sha256(material.encode()).hexdigest(),
                "scan_run_id": scan_run_id,
                "record_id": record_id,
                "clean_sha256": str(row["clean_sha256"]),
                "record_sha256": str(row["record_sha256"]),
                "observed_at": row["observed_at"],
            }
        )
    if parameters:
        bind.execute(
            sa.text(
                "INSERT INTO campaign_observations "
                "(campaign_key, observation_key, scan_run_id, record_id, clean_sha256, "
                "record_sha256, observed_at) VALUES "
                "(:campaign_key, :observation_key, :scan_run_id, :record_id, :clean_sha256, "
                ":record_sha256, :observed_at)"
            ),
            parameters,
        )


def downgrade() -> None:
    _preflight_legacy_hash_uniques()

    op.drop_index("ix_campaign_observations_record_id", table_name="campaign_observations")
    op.drop_index(
        "ix_campaign_observations_campaign_observed",
        table_name="campaign_observations",
    )
    op.drop_table("campaign_observations")
    op.drop_index("ix_campaign_records_key_payload_hash", table_name="campaign_records")
    op.drop_index("ix_campaign_records_key_record_hash", table_name="campaign_records")
    op.create_unique_constraint(
        "uq_campaign_records_key_payload_hash",
        "campaign_records",
        ["campaign_key", "payload_sha256"],
    )
    op.create_unique_constraint(
        "uq_campaign_records_key_record_hash",
        "campaign_records",
        ["campaign_key", "record_sha256"],
    )
    op.drop_index("ix_monitored_source_states_observed", table_name="monitored_source_states")
    op.drop_table("monitored_source_states")
    op.drop_index(
        "ix_monitored_campaign_targets_discovered_from",
        table_name="monitored_campaign_targets",
    )
    op.drop_index(
        "ix_monitored_campaign_targets_active_bank_url",
        table_name="monitored_campaign_targets",
    )
    op.drop_table("monitored_campaign_targets")


def _preflight_legacy_hash_uniques() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM campaign_records
                    GROUP BY campaign_key, record_sha256
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'WP-111 downgrade refused: duplicate campaign record hashes '
                                  'cannot satisfy the legacy unique constraint';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM campaign_records
                    GROUP BY campaign_key, payload_sha256
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'WP-111 downgrade refused: duplicate campaign payload hashes '
                                  'cannot satisfy the legacy unique constraint';
                END IF;
            END
            $$
            """
        )
    )
