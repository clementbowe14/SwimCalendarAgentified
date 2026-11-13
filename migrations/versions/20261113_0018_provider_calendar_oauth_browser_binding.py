"""Add browser binding to provider calendar OAuth state.

Revision ID: 20261113_0018
Revises: 20261112_0017
Create Date: 2026-11-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20261113_0018"
down_revision = "20261112_0017"
branch_labels = None
depends_on = None


TABLE_NAME = "provider_calendar_oauth_states"
COLUMN_NAME = "browser_binding_digest"
CHECK_NAME = "ck_provider_calendar_oauth_states_browser_binding_digest"
INDEX_NAME = "uq_provider_calendar_oauth_states_browser_binding_digest"


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute(
        "LOCK TABLE provider_calendar_oauth_states "
        "IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    op.add_column(
        TABLE_NAME,
        sa.Column(
            COLUMN_NAME,
            sa.LargeBinary(length=32),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        CHECK_NAME,
        TABLE_NAME,
        "browser_binding_digest IS NULL OR "
        "(octet_length(browser_binding_digest) = 32 "
        "AND browser_binding_digest <> state_digest)",
    )
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        [COLUMN_NAME],
        unique=True,
        postgresql_where=sa.text("browser_binding_digest IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute(
        "LOCK TABLE provider_calendar_oauth_states "
        "IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    if bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM provider_calendar_oauth_states "
            "WHERE browser_binding_digest IS NOT NULL "
            "AND expires_at > CURRENT_TIMESTAMP"
            ")"
        )
    ).scalar_one():
        raise RuntimeError(
            "Cannot downgrade calendar OAuth browser binding while an "
            "authorization attempt is active. Retry after it expires or is consumed."
        )

    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.drop_constraint(CHECK_NAME, TABLE_NAME, type_="check")
    op.drop_column(TABLE_NAME, COLUMN_NAME)


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "Calendar OAuth browser-binding migration requires PostgreSQL."
        )
