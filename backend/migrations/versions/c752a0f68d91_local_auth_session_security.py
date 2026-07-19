"""local auth session security

Revision ID: c752a0f68d91
Revises: b421f9d8c4a1
Create Date: 2026-07-19 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c752a0f68d91"
down_revision: str | Sequence[str] | None = "b421f9d8c4a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _preflight_existing_auth_rows()
    _upgrade_users()
    _upgrade_sessions()
    _create_rate_reservations()
    _create_append_only_audit()


def _preflight_existing_auth_rows() -> None:
    op.execute(
        sa.text(
            r"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM auth_users
                    WHERE username !~ '^[a-z][a-z0-9_-]{0,63}$'
                       OR password_hash NOT LIKE '$argon2id$v=19$%'
                       OR jsonb_typeof(roles) IS DISTINCT FROM 'array'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'WP-121 migration refused: existing auth user is not canonical';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM auth_sessions
                    WHERE token_hash !~ '^[0-9a-f]{64}$'
                       OR csrf_hash !~ '^[0-9a-f]{64}$'
                       OR token_hash = csrf_hash
                       OR COALESCE(last_seen_at, created_at) >= expires_at
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'WP-121 migration refused: existing auth session is unsafe';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM auth_sessions GROUP BY csrf_hash HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'WP-121 migration refused: duplicate CSRF digest exists';
                END IF;
            END
            $$
            """
        )
    )


def _upgrade_users() -> None:
    op.drop_constraint(op.f("ck_auth_users_username_length"), "auth_users", type_="check")
    op.drop_constraint(
        op.f("ck_auth_users_password_hash_not_plaintext"), "auth_users", type_="check"
    )
    op.alter_column(
        "auth_users",
        "username",
        existing_type=sa.String(length=191),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.add_column(
        "auth_users",
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "auth_users",
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_auth_users_username_canonical"),
        "auth_users",
        "username ~ '^[a-z][a-z0-9_-]{0,63}$'",
    )
    op.create_check_constraint(
        op.f("ck_auth_users_password_hash_argon2id_v19"),
        "auth_users",
        "password_hash LIKE '$argon2id$v=19$%'",
    )
    op.create_check_constraint(
        op.f("ck_auth_users_roles_array"),
        "auth_users",
        "jsonb_typeof(roles) = 'array'",
    )


def _upgrade_sessions() -> None:
    op.drop_index("ix_auth_sessions_user_expires", table_name="auth_sessions")
    op.drop_constraint(
        op.f("ck_auth_sessions_session_token_hash_sha256"), "auth_sessions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_session_csrf_hash_sha256"), "auth_sessions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_session_expiry_after_creation"), "auth_sessions", type_="check"
    )
    op.alter_column(
        "auth_sessions",
        "expires_at",
        new_column_name="absolute_expires_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
    op.add_column(
        "auth_sessions",
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE auth_sessions SET "
            "last_seen_at = COALESCE(last_seen_at, created_at), "
            "idle_expires_at = LEAST(absolute_expires_at, "
            "COALESCE(last_seen_at, created_at) + interval '30 minutes')"
        )
    )
    op.alter_column(
        "auth_sessions",
        "last_seen_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "auth_sessions",
        "idle_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_column("auth_sessions", "client_context")
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_token_hash_sha256"),
        "auth_sessions",
        "token_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_csrf_hash_sha256"),
        "auth_sessions",
        "csrf_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_hashes_distinct"),
        "auth_sessions",
        "token_hash <> csrf_hash",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_absolute_expiry_after_creation"),
        "auth_sessions",
        "absolute_expires_at > created_at",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_last_seen_after_creation"),
        "auth_sessions",
        "last_seen_at >= created_at",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_idle_after_last_seen"),
        "auth_sessions",
        "idle_expires_at > last_seen_at",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_idle_within_absolute"),
        "auth_sessions",
        "idle_expires_at <= absolute_expires_at",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_revoked_after_creation"),
        "auth_sessions",
        "revoked_at IS NULL OR revoked_at >= created_at",
    )
    op.create_unique_constraint(op.f("uq_auth_sessions_csrf_hash"), "auth_sessions", ["csrf_hash"])
    op.create_index(
        "ix_auth_sessions_user_active",
        "auth_sessions",
        ["user_id", "revoked_at", "idle_expires_at", "absolute_expires_at"],
        unique=False,
    )


def _create_rate_reservations() -> None:
    op.create_table(
        "auth_rate_reservations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dimension", sa.String(length=16), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('username','client')",
            name=op.f("ck_auth_rate_reservations_rate_dimension_known"),
        ),
        sa.CheckConstraint(
            "subject_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_auth_rate_reservations_rate_subject_hash_sha256"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_rate_reservations")),
    )
    op.create_index(
        "ix_auth_rate_reservations_window",
        "auth_rate_reservations",
        ["dimension", "subject_hash", "reserved_at"],
        unique=False,
    )


def _create_append_only_audit() -> None:
    op.create_table(
        "auth_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("username_hash", sa.String(length=64), nullable=True),
        sa.Column("client_hash", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=191), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type ~ '^[a-z][a-z0-9_.-]{0,63}$'",
            name=op.f("ck_auth_audit_events_audit_event_type_known"),
        ),
        sa.CheckConstraint(
            "outcome IN ('success','failure','denied')",
            name=op.f("ck_auth_audit_events_audit_outcome_known"),
        ),
        sa.CheckConstraint(
            "username_hash IS NULL OR username_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_auth_audit_events_audit_username_hash_sha256"),
        ),
        sa.CheckConstraint(
            "client_hash IS NULL OR client_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_auth_audit_events_audit_client_hash_sha256"),
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_.-]{0,63}$'",
            name=op.f("ck_auth_audit_events_audit_reason_code_known"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["auth_users.id"],
            name=op.f("fk_auth_audit_events_actor_user_id_auth_users"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_auth_audit_events_session_id_auth_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_audit_events")),
    )
    op.create_index(
        "ix_auth_audit_events_actor",
        "auth_audit_events",
        ["actor_user_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_auth_audit_events_occurred",
        "auth_audit_events",
        ["occurred_at", "id"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION public.prevent_auth_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'auth audit events are append-only';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER auth_audit_no_update_delete "
        "BEFORE UPDATE OR DELETE ON auth_audit_events "
        "FOR EACH ROW EXECUTE FUNCTION public.prevent_auth_audit_mutation()"
    )
    op.execute(
        "CREATE TRIGGER auth_audit_no_truncate "
        "BEFORE TRUNCATE ON auth_audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION public.prevent_auth_audit_mutation()"
    )


def downgrade() -> None:
    op.drop_table("auth_audit_events")
    op.execute("DROP FUNCTION public.prevent_auth_audit_mutation()")
    op.drop_index("ix_auth_rate_reservations_window", table_name="auth_rate_reservations")
    op.drop_table("auth_rate_reservations")
    _downgrade_sessions()
    _downgrade_users()


def _downgrade_sessions() -> None:
    op.drop_index("ix_auth_sessions_user_active", table_name="auth_sessions")
    op.drop_constraint(op.f("uq_auth_sessions_csrf_hash"), "auth_sessions", type_="unique")
    for name in (
        "session_revoked_after_creation",
        "session_idle_within_absolute",
        "session_idle_after_last_seen",
        "session_last_seen_after_creation",
        "session_absolute_expiry_after_creation",
        "session_hashes_distinct",
        "session_csrf_hash_sha256",
        "session_token_hash_sha256",
    ):
        op.drop_constraint(op.f(f"ck_auth_sessions_{name}"), "auth_sessions", type_="check")
    op.add_column(
        "auth_sessions",
        sa.Column(
            "client_context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column(
        "auth_sessions",
        "client_context",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=None,
        existing_nullable=False,
    )
    op.drop_column("auth_sessions", "idle_expires_at")
    op.alter_column(
        "auth_sessions",
        "last_seen_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.alter_column(
        "auth_sessions",
        "absolute_expires_at",
        new_column_name="expires_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_token_hash_sha256"),
        "auth_sessions",
        "char_length(token_hash) = 64",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_csrf_hash_sha256"),
        "auth_sessions",
        "char_length(csrf_hash) = 64",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_session_expiry_after_creation"),
        "auth_sessions",
        "expires_at > created_at",
    )
    op.create_index(
        "ix_auth_sessions_user_expires",
        "auth_sessions",
        ["user_id", "expires_at"],
        unique=False,
    )


def _downgrade_users() -> None:
    op.drop_constraint(op.f("ck_auth_users_roles_array"), "auth_users", type_="check")
    op.drop_constraint(
        op.f("ck_auth_users_password_hash_argon2id_v19"), "auth_users", type_="check"
    )
    op.drop_constraint(op.f("ck_auth_users_username_canonical"), "auth_users", type_="check")
    op.drop_column("auth_users", "last_authenticated_at")
    op.drop_column("auth_users", "password_changed_at")
    op.alter_column(
        "auth_users",
        "username",
        existing_type=sa.String(length=64),
        type_=sa.String(length=191),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_auth_users_username_length"),
        "auth_users",
        "char_length(username) BETWEEN 1 AND 191",
    )
    op.create_check_constraint(
        op.f("ck_auth_users_password_hash_not_plaintext"),
        "auth_users",
        "char_length(password_hash) >= 20",
    )
