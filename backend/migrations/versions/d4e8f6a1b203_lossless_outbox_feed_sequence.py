"""lossless outbox feed sequence

Revision ID: d4e8f6a1b203
Revises: c752a0f68d91
Create Date: 2026-07-19 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8f6a1b203"
down_revision: str | Sequence[str] | None = "c752a0f68d91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE SEQUENCE public.outbox_feed_sequence "
        "AS bigint INCREMENT BY 1 MINVALUE 1 NO MAXVALUE START WITH 1 CACHE 1 NO CYCLE"
    )
    op.add_column(
        "outbox_events",
        sa.Column("feed_sequence", sa.BigInteger(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            WITH deterministic_positions AS (
                SELECT
                    id,
                    row_number() OVER (ORDER BY created_at ASC, id ASC)::bigint
                        AS feed_sequence
                FROM outbox_events
            )
            UPDATE outbox_events AS event
            SET feed_sequence = position.feed_sequence
            FROM deterministic_positions AS position
            WHERE event.id = position.id
            """
        )
    )
    # is_called=false distinguishes a pure deterministic backfill from any
    # subsequent allocation, including an allocation whose transaction rolls back.
    op.execute(
        sa.text(
            "SELECT setval("
            "'public.outbox_feed_sequence'::regclass, "
            "COALESCE((SELECT max(feed_sequence) + 1 FROM outbox_events), 1), false)"
        )
    )
    op.alter_column(
        "outbox_events",
        "feed_sequence",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_outbox_events_feed_sequence",
        "outbox_events",
        ["feed_sequence"],
    )
    op.create_index(
        "ix_outbox_events_notification_feed",
        "outbox_events",
        ["topic", "event_type", "feed_sequence"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION public.prevent_outbox_feed_sequence_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.feed_sequence IS DISTINCT FROM OLD.feed_sequence THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'outbox feed_sequence is immutable';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER outbox_feed_sequence_no_update "
        "BEFORE UPDATE OF feed_sequence ON outbox_events "
        "FOR EACH ROW EXECUTE FUNCTION public.prevent_outbox_feed_sequence_mutation()"
    )


def downgrade() -> None:
    # Sequence state is non-transactional. Once nextval has been called, even by
    # a rolled-back producer, restoring the timestamp cursor would be unsafe.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                feed_was_allocated boolean;
            BEGIN
                SELECT is_called
                INTO feed_was_allocated
                FROM public.outbox_feed_sequence;
                IF feed_was_allocated THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'outbox feed downgrade refused: '
                            'sequence positions were allocated';
                END IF;
            END
            $$
            """
        )
    )
    op.execute("DROP TRIGGER outbox_feed_sequence_no_update ON outbox_events")
    op.execute("DROP FUNCTION public.prevent_outbox_feed_sequence_mutation()")
    op.drop_index("ix_outbox_events_notification_feed", table_name="outbox_events")
    op.drop_constraint(
        "uq_outbox_events_feed_sequence",
        "outbox_events",
        type_="unique",
    )
    op.drop_column("outbox_events", "feed_sequence")
    op.execute("DROP SEQUENCE public.outbox_feed_sequence")
