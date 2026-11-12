"""Add encrypted provider calendar connection storage.

Revision ID: 20261112_0015
Revises: 20261111_0014
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20261112_0015"
down_revision = "20261111_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.create_table(
        "provider_calendar_connections",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=16),
            server_default=sa.text("'google'"),
            nullable=False,
        ),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("credential_key_id", sa.String(length=64), nullable=False),
        sa.Column("credential_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "authorization_required_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
            "id ~ '^calconn_[0-9a-f]{32}$'",
            name="ck_provider_calendar_connections_id",
        ),
        sa.CheckConstraint(
            "provider = 'google'",
            name="ck_provider_calendar_connections_provider",
        ),
        sa.CheckConstraint(
            "credential_version = 1",
            name="ck_provider_calendar_connections_credential_version",
        ),
        sa.CheckConstraint(
            "credential_key_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name="ck_provider_calendar_connections_credential_key_id",
        ),
        sa.CheckConstraint(
            "octet_length(credential_nonce) = 12",
            name="ck_provider_calendar_connections_credential_nonce_length",
        ),
        sa.CheckConstraint(
            "octet_length(credential_ciphertext) BETWEEN 17 AND 16400",
            name="ck_provider_calendar_connections_credential_ciphertext_length",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_provider_calendar_connections_timestamp_order",
        ),
        sa.CheckConstraint(
            "authorization_required_at IS NULL OR "
            "(authorization_required_at >= created_at "
            "AND authorization_required_at <= updated_at)",
            name="ck_provider_calendar_connections_authorization_timing",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["provider_accounts.id"],
            name="fk_provider_calendar_connections_account_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_provider_calendar_connections",
        ),
        sa.UniqueConstraint(
            "account_id",
            name="uq_provider_calendar_connections_account_id",
        ),
    )
    op.create_index(
        "uq_provider_calendar_connections_key_nonce",
        "provider_calendar_connections",
        ["credential_key_id", "credential_nonce"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute(
        "LOCK TABLE provider_calendar_connections "
        "IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    if bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM provider_calendar_connections)")
    ).scalar_one():
        raise RuntimeError(
            "Cannot downgrade calendar connection storage while connections exist. "
            "Disconnect every provider calendar before retrying."
        )

    op.drop_index(
        "uq_provider_calendar_connections_key_nonce",
        table_name="provider_calendar_connections",
    )
    op.drop_table("provider_calendar_connections")


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "Calendar connection storage migration requires PostgreSQL."
        )
