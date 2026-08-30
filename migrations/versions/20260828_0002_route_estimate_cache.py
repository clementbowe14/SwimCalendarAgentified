"""route estimate cache

Revision ID: 20260828_0002
Revises: 20260828_0001
Create Date: 2026-08-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0002"
down_revision: Union[str, None] = "20260828_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")

    op.create_table(
        "route_estimates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("origin_lat", sa.Float(), nullable=False),
        sa.Column("origin_lng", sa.Float(), nullable=False),
        sa.Column("destination_lat", sa.Float(), nullable=False),
        sa.Column("destination_lng", sa.Float(), nullable=False),
        sa.Column("origin_key", sa.String(), nullable=False),
        sa.Column("destination_key", sa.String(), nullable=False),
        sa.Column("departure_bucket_start", sa.DateTime(), nullable=False),
        sa.Column("traffic_mode", sa.String(), nullable=False, server_default="none"),
        sa.Column("distance_miles", sa.Float(), nullable=False),
        sa.Column("travel_time_mins", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint(
            "provider",
            "origin_key",
            "destination_key",
            "departure_bucket_start",
            "traffic_mode",
            name="uq_route_estimates_cache_key",
        ),
    )
    op.create_index("ix_route_estimates_expires_at", "route_estimates", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_route_estimates_expires_at", table_name="route_estimates")
    op.drop_table("route_estimates")
