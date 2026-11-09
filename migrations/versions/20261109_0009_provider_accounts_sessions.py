"""Add revocable provider accounts and sessions.

Revision ID: 20261109_0009
Revises: 20260922_0008
Create Date: 2026-11-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20261109_0009"
down_revision = "20260922_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Provider authentication storage requires PostgreSQL.")

    op.add_column(
        "invite_tokens",
        sa.Column(
            "is_demo",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    bind.execute(
        sa.text(
            """
            UPDATE invite_tokens
            SET is_demo = true
            WHERE token = :demo_token OR token = :sofia_token
            """
        ),
        {
            "demo_token": "demo-invite-token",
            "sofia_token": "invite_sofia_0826",
        },
    )

    op.create_table(
        "provider_accounts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("instructor_id", sa.String(), nullable=False),
        sa.Column("email_normalized", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "is_demo",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("session_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "session_version >= 1",
            name="ck_provider_accounts_session_version",
        ),
        sa.ForeignKeyConstraint(
            ["instructor_id"],
            ["instructors.id"],
            name="fk_provider_accounts_instructor_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_accounts"),
        sa.UniqueConstraint(
            "email_normalized",
            name="uq_provider_accounts_email_normalized",
        ),
        sa.UniqueConstraint(
            "instructor_id",
            name="uq_provider_accounts_instructor_id",
        ),
    )

    op.create_table(
        "provider_sessions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("token_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=80), nullable=True),
        sa.CheckConstraint(
            "session_version >= 1",
            name="ck_provider_sessions_session_version",
        ),
        sa.CheckConstraint(
            "octet_length(token_digest) = 32",
            name="ck_provider_sessions_digest_length",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["provider_accounts.id"],
            name="fk_provider_sessions_account_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_sessions"),
    )
    op.create_index(
        "uq_provider_sessions_token_digest",
        "provider_sessions",
        ["token_digest"],
        unique=True,
    )
    op.create_index(
        "ix_provider_sessions_account_revoked",
        "provider_sessions",
        ["account_id", "revoked_at"],
        unique=False,
    )
    op.create_index(
        "ix_provider_sessions_expires_at",
        "provider_sessions",
        ["expires_at"],
        unique=False,
    )

    op.drop_index("uq_appointments_idempotency_key", table_name="appointments")
    op.create_index(
        "uq_appointments_instructor_idempotency_key",
        "appointments",
        ["instructor_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            """
            SELECT idempotency_key, count(*) AS duplicate_count
            FROM appointments
            WHERE idempotency_key IS NOT NULL
            GROUP BY idempotency_key
            HAVING count(*) > 1
            ORDER BY idempotency_key
            LIMIT 1
            """
        )
    ).mappings().first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade provider-scoped appointment idempotency: "
            f"key {duplicate['idempotency_key']!r} exists {duplicate['duplicate_count']} times. "
            "Resolve cross-provider duplicate keys before retrying the downgrade."
        )

    op.drop_index(
        "uq_appointments_instructor_idempotency_key",
        table_name="appointments",
    )
    op.create_index(
        "uq_appointments_idempotency_key",
        "appointments",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.drop_index("ix_provider_sessions_expires_at", table_name="provider_sessions")
    op.drop_index("ix_provider_sessions_account_revoked", table_name="provider_sessions")
    op.drop_index("uq_provider_sessions_token_digest", table_name="provider_sessions")
    op.drop_table("provider_sessions")
    op.drop_table("provider_accounts")
    op.drop_column("invite_tokens", "is_demo")
