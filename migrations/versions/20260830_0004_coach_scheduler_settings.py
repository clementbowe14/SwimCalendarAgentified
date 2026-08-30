"""coach scheduler settings

Revision ID: 20260830_0004
Revises: 20260828_0003
Create Date: 2026-08-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_0004"
down_revision: Union[str, None] = "20260828_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")

    op.create_table(
        "scheduler_settings",
        sa.Column("instructor_id", sa.String(), sa.ForeignKey("instructors.id"), primary_key=True),
        sa.Column("rounding_increment_mins", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("post_break_buffer_mins", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("distance_buffer_tiers", sa.Text(), nullable=False),
        sa.Column("payments_enabled_methods", sa.Text(), nullable=False, server_default=""),
        sa.Column("cancellation_window_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("require_payment_method_for_new_clients", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("scheduler_settings")
