"""Add provider calendar connection revision fence.

Revision ID: 20261114_0019
Revises: 20261113_0018
Create Date: 2026-11-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20261114_0019"
down_revision = "20261113_0018"
branch_labels = None
depends_on = None


TABLE_NAME = "provider_accounts"
COLUMN_NAME = "calendar_connection_revision"
CHECK_NAME = "ck_provider_accounts_calendar_connection_revision_nonnegative"


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)
    _lock_provider_accounts()

    op.add_column(
        TABLE_NAME,
        sa.Column(
            COLUMN_NAME,
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        CHECK_NAME,
        TABLE_NAME,
        "calendar_connection_revision >= 0",
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)
    _lock_provider_accounts()

    if bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM provider_accounts "
            "WHERE calendar_connection_revision <> 0"
            ")"
        )
    ).scalar_one():
        raise RuntimeError(
            "Cannot downgrade the calendar connection revision fence after "
            "calendar connection activity."
        )

    op.drop_constraint(CHECK_NAME, TABLE_NAME, type_="check")
    op.drop_column(TABLE_NAME, COLUMN_NAME)


def _lock_provider_accounts() -> None:
    op.execute("LOCK TABLE provider_accounts IN ACCESS EXCLUSIVE MODE NOWAIT")


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "Calendar connection revision migration requires PostgreSQL."
        )
