"""align stored candidate hashes with deterministic candidate identities

Revision ID: f6a91c2d8e47
Revises: d4e8f6a1b203
Create Date: 2026-07-19 20:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a91c2d8e47"
down_revision: str | Sequence[str] | None = "d4e8f6a1b203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _preflight_candidate_hash_backfill()
    op.execute(
        sa.text(
            r"""
            UPDATE extraction_candidates
            SET candidate_sha256 = right(id, 64)
            WHERE id ~ '^candidate:[0-9a-f]{64}$'
              AND candidate_sha256 IS DISTINCT FROM right(id, 64)
            """
        )
    )
    _verify_candidate_hash_backfill()


def _preflight_candidate_hash_backfill() -> None:
    op.execute(
        sa.text(
            r"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM extraction_candidates
                    WHERE id LIKE 'candidate:%'
                      AND char_length(id) = 74
                      AND id !~ '^candidate:[0-9a-f]{64}$'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'WP-040 migration refused: malformed deterministic '
                                  'candidate identity';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM extraction_candidates
                    WHERE id ~ '^candidate:[0-9a-f]{64}$'
                      AND candidate_sha256 !~ '^[0-9a-f]{64}$'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'WP-040 migration refused: malformed stored candidate hash';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM extraction_candidates AS target
                    JOIN extraction_candidates AS conflict
                      ON conflict.candidate_sha256 = right(target.id, 64)
                     AND conflict.id <> target.id
                    WHERE target.id ~ '^candidate:[0-9a-f]{64}$'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'WP-040 migration refused: candidate hash target '
                                  'is already in use';
                END IF;
            END
            $$
            """
        )
    )


def _verify_candidate_hash_backfill() -> None:
    op.execute(
        sa.text(
            r"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM extraction_candidates
                    WHERE id ~ '^candidate:[0-9a-f]{64}$'
                      AND candidate_sha256 IS DISTINCT FROM right(id, 64)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'WP-040 migration failed: candidate hash backfill is incomplete';
                END IF;
            END
            $$
            """
        )
    )


def downgrade() -> None:
    # The prior hash was derived from a superseded serialization contract and
    # cannot be reconstructed losslessly from stored columns. An empty database
    # may downgrade; populated deterministic identities must not silently become
    # incompatible with the older repository implementation.
    op.execute(
        sa.text(
            r"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM extraction_candidates
                    WHERE id ~ '^candidate:[0-9a-f]{64}$'
                      AND candidate_sha256 = right(id, 64)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'WP-040 downgrade refused: prior candidate hashes '
                                  'cannot be reconstructed';
                END IF;
            END
            $$
            """
        )
    )
