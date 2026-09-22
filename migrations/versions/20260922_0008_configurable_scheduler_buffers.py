"""Add configurable scheduler buffer switches.

The server defaults remain in place so an older writer can insert rows during
the additive phase of a rolling deployment. Downgrade loses customized switch
values and restores the pre-issue-17 behavior.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260922_0008"
down_revision = "20260918_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduler_settings",
        sa.Column("buffers_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "scheduler_settings",
        sa.Column(
            "apply_distance_buffer_at_shift_start",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("scheduler_settings", "apply_distance_buffer_at_shift_start")
    op.drop_column("scheduler_settings", "buffers_enabled")
