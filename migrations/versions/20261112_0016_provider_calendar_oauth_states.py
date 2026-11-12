"""Add single-use provider calendar OAuth-state storage.

Revision ID: 20261112_0016
Revises: 20261112_0015
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20261112_0016"
down_revision = "20261112_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute("LOCK TABLE provider_sessions IN ACCESS EXCLUSIVE MODE NOWAIT")
    op.create_unique_constraint(
        "uq_provider_sessions_account_id_id",
        "provider_sessions",
        ["account_id", "id"],
    )
    op.create_table(
        "provider_calendar_oauth_states",
        sa.Column("state_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=16),
            server_default=sa.text("'google'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(state_digest) = 32",
            name="ck_provider_calendar_oauth_states_digest_length",
        ),
        sa.CheckConstraint(
            "provider = 'google'",
            name="ck_provider_calendar_oauth_states_provider",
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND "
            "expires_at <= created_at + INTERVAL '10 minutes'",
            name="ck_provider_calendar_oauth_states_lifetime",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "session_id"],
            ["provider_sessions.account_id", "provider_sessions.id"],
            name="fk_provider_calendar_oauth_states_account_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "state_digest",
            name="pk_provider_calendar_oauth_states",
        ),
        sa.UniqueConstraint(
            "account_id",
            "session_id",
            name="uq_provider_calendar_oauth_states_account_session",
        ),
    )
    op.create_index(
        "ix_provider_calendar_oauth_states_expires_at",
        "provider_calendar_oauth_states",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute(
        "LOCK TABLE provider_sessions, provider_calendar_oauth_states "
        "IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    if bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM provider_calendar_oauth_states "
            "WHERE expires_at > CURRENT_TIMESTAMP"
            ")"
        )
    ).scalar_one():
        raise RuntimeError(
            "Cannot downgrade calendar OAuth-state storage while an "
            "authorization attempt is active. Retry after it expires or is consumed."
        )

    op.drop_index(
        "ix_provider_calendar_oauth_states_expires_at",
        table_name="provider_calendar_oauth_states",
    )
    op.drop_table("provider_calendar_oauth_states")
    op.drop_constraint(
        "uq_provider_sessions_account_id_id",
        "provider_sessions",
        type_="unique",
    )


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Calendar OAuth-state storage migration requires PostgreSQL.")
