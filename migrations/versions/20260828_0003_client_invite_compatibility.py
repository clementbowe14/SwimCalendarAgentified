"""client invite compatibility

Revision ID: 20260828_0003
Revises: 20260828_0002
Create Date: 2026-08-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0003"
down_revision: Union[str, None] = "20260828_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")

    op.create_table(
        "invite_intakes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("invite_token", sa.String(), sa.ForeignKey("invite_tokens.token"), nullable=False),
        sa.Column("client_name", sa.String(), nullable=False),
        sa.Column("guardian_phone", sa.String(), nullable=True),
        sa.Column("guardian_email", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("location_type", sa.String(), nullable=False, server_default="home"),
        sa.Column("location_address", sa.String(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("is_instructor_facility", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("location_type in ('home', 'facility')", name="ck_invite_intakes_location_type"),
        sa.UniqueConstraint("invite_token", name="uq_invite_intakes_invite_token"),
    )
    op.create_table(
        "invite_slot_offers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("invite_token", sa.String(), sa.ForeignKey("invite_tokens.token"), nullable=False),
        sa.Column("instructor_id", sa.String(), sa.ForeignKey("instructors.id"), nullable=False),
        sa.Column("client_name", sa.String(), nullable=False),
        sa.Column("client_address", sa.String(), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=False),
        sa.Column("slot_start", sa.DateTime(), nullable=False),
        sa.Column("slot_end", sa.DateTime(), nullable=False),
        sa.Column("duration_mins", sa.Integer(), nullable=False),
        sa.Column("location_type", sa.String(), nullable=False),
        sa.Column("location_address", sa.String(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("travel_time_mins", sa.Integer(), nullable=False),
        sa.Column("buffer_mins", sa.Integer(), nullable=False),
        sa.Column("distance_miles", sa.Float(), nullable=False),
        sa.Column("recurrence", sa.String(), nullable=False, server_default="single"),
        sa.Column("recurrence_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("slot_end > slot_start", name="ck_invite_slot_offers_end_after_start"),
        sa.CheckConstraint("location_type in ('home', 'facility')", name="ck_invite_slot_offers_location_type"),
        sa.CheckConstraint("recurrence in ('single', 'weekly')", name="ck_invite_slot_offers_recurrence"),
    )
    op.create_index(
        "ix_invite_slot_offers_invite_expires",
        "invite_slot_offers",
        ["invite_token", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_invite_slot_offers_invite_expires", table_name="invite_slot_offers")
    op.drop_table("invite_slot_offers")
    op.drop_table("invite_intakes")
