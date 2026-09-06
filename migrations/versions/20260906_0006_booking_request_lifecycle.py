"""booking request lifecycle

Revision ID: 20260906_0006
Revises: 20260901_0005
Create Date: 2026-09-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0006"
down_revision: Union[str, None] = "20260901_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")

    op.drop_constraint("ck_lesson_requests_status", "lesson_requests", type_="check")

    op.add_column("lesson_requests", sa.Column("recurrence", sa.String(), nullable=True, server_default="single"))
    op.add_column("lesson_requests", sa.Column("recurrence_count", sa.Integer(), nullable=True, server_default="1"))
    op.add_column("lesson_requests", sa.Column("original_slot_offer_id", sa.String(), nullable=True))
    op.add_column("lesson_requests", sa.Column("confirmed_slot_offer_id", sa.String(), nullable=True))
    op.add_column("lesson_requests", sa.Column("expires_at", sa.DateTime(), nullable=True))
    op.add_column("lesson_requests", sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.add_column("lesson_requests", sa.Column("declined_at", sa.DateTime(), nullable=True))
    op.add_column("lesson_requests", sa.Column("alternatives_sent_at", sa.DateTime(), nullable=True))
    op.add_column("lesson_requests", sa.Column("validation_failure_code", sa.String(), nullable=True))
    op.add_column("lesson_requests", sa.Column("validation_failure_details", sa.JSON(), nullable=True))
    op.add_column("lesson_requests", sa.Column("validation_failed_at", sa.DateTime(), nullable=True))
    op.add_column("lesson_requests", sa.Column("client_notification_status", sa.String(), nullable=True))
    op.add_column("lesson_requests", sa.Column("client_notification_message", sa.Text(), nullable=True))
    op.add_column("lesson_requests", sa.Column("client_notified_at", sa.DateTime(), nullable=True))

    op.add_column("invite_slot_offers", sa.Column("recurrence_count", sa.Integer(), nullable=True, server_default="1"))
    op.add_column("invite_slot_offers", sa.Column("lesson_request_id", sa.String(), nullable=True))
    op.add_column("invite_slot_offers", sa.Column("offer_kind", sa.String(), nullable=True, server_default="search"))
    op.add_column("invite_slot_offers", sa.Column("message", sa.Text(), nullable=True))
    op.add_column("invite_slot_offers", sa.Column("notified_at", sa.DateTime(), nullable=True))

    op.execute("UPDATE lesson_requests SET status = 'confirmed' WHERE status = 'accepted'")
    op.execute("UPDATE lesson_requests SET recurrence = 'single' WHERE recurrence IS NULL")
    op.execute("UPDATE lesson_requests SET recurrence_count = 1 WHERE recurrence_count IS NULL")
    op.execute(
        """
        UPDATE lesson_requests AS lr
        SET expires_at = LEAST(lr.created_at + interval '24 hours', it.expires_at)
        FROM invite_tokens AS it
        WHERE lr.invite_token = it.token AND lr.expires_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE lesson_requests
        SET expires_at = created_at + interval '24 hours'
        WHERE expires_at IS NULL
        """
    )
    op.execute("UPDATE invite_slot_offers SET recurrence_count = 1 WHERE recurrence_count IS NULL")
    op.execute("UPDATE invite_slot_offers SET offer_kind = 'search' WHERE offer_kind IS NULL")

    op.alter_column("lesson_requests", "recurrence", nullable=False, server_default="single")
    op.alter_column("lesson_requests", "recurrence_count", nullable=False, server_default="1")
    op.alter_column("lesson_requests", "expires_at", nullable=False)
    op.alter_column("invite_slot_offers", "recurrence_count", nullable=False, server_default="1")
    op.alter_column("invite_slot_offers", "offer_kind", nullable=False, server_default="search")

    op.create_check_constraint(
        "ck_lesson_requests_status",
        "lesson_requests",
        "status in ('pending', 'confirmed', 'alternatives_sent', 'declined', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_lesson_requests_recurrence",
        "lesson_requests",
        "recurrence in ('single', 'weekly')",
    )
    op.create_check_constraint(
        "ck_lesson_requests_recurrence_count",
        "lesson_requests",
        "((recurrence = 'single' AND recurrence_count = 1) OR (recurrence = 'weekly' AND recurrence_count BETWEEN 1 AND 12))",
    )
    op.create_check_constraint(
        "ck_invite_slot_offers_offer_kind",
        "invite_slot_offers",
        "offer_kind in ('search', 'alternative')",
    )
    op.create_check_constraint(
        "ck_invite_slot_offers_recurrence_count",
        "invite_slot_offers",
        "((recurrence = 'single' AND recurrence_count = 1) OR (recurrence = 'weekly' AND recurrence_count BETWEEN 1 AND 12))",
    )
    op.create_foreign_key(
        "fk_lesson_requests_original_slot_offer_id",
        "lesson_requests",
        "invite_slot_offers",
        ["original_slot_offer_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_lesson_requests_confirmed_slot_offer_id",
        "lesson_requests",
        "invite_slot_offers",
        ["confirmed_slot_offer_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_invite_slot_offers_lesson_request_id",
        "invite_slot_offers",
        "lesson_requests",
        ["lesson_request_id"],
        ["id"],
    )
    op.create_index(
        "ix_invite_slot_offers_request_kind",
        "invite_slot_offers",
        ["lesson_request_id", "offer_kind"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")

    op.drop_index("ix_invite_slot_offers_request_kind", table_name="invite_slot_offers")
    op.drop_constraint("fk_invite_slot_offers_lesson_request_id", "invite_slot_offers", type_="foreignkey")
    op.drop_constraint("fk_lesson_requests_confirmed_slot_offer_id", "lesson_requests", type_="foreignkey")
    op.drop_constraint("fk_lesson_requests_original_slot_offer_id", "lesson_requests", type_="foreignkey")
    op.drop_constraint("ck_invite_slot_offers_recurrence_count", "invite_slot_offers", type_="check")
    op.drop_constraint("ck_invite_slot_offers_offer_kind", "invite_slot_offers", type_="check")
    op.drop_constraint("ck_lesson_requests_recurrence_count", "lesson_requests", type_="check")
    op.drop_constraint("ck_lesson_requests_recurrence", "lesson_requests", type_="check")
    op.drop_constraint("ck_lesson_requests_status", "lesson_requests", type_="check")
    op.execute("UPDATE lesson_requests SET status = 'accepted' WHERE status = 'confirmed'")
    op.execute("UPDATE lesson_requests SET status = 'declined' WHERE status = 'alternatives_sent'")
    op.create_check_constraint(
        "ck_lesson_requests_status",
        "lesson_requests",
        "status in ('pending', 'accepted', 'declined', 'cancelled')",
    )
    for column in (
        "notified_at",
        "message",
        "offer_kind",
        "lesson_request_id",
        "recurrence_count",
    ):
        op.drop_column("invite_slot_offers", column)
    for column in (
        "client_notified_at",
        "client_notification_message",
        "client_notification_status",
        "validation_failed_at",
        "validation_failure_details",
        "validation_failure_code",
        "alternatives_sent_at",
        "declined_at",
        "confirmed_at",
        "expires_at",
        "confirmed_slot_offer_id",
        "original_slot_offer_id",
        "recurrence_count",
        "recurrence",
    ):
        op.drop_column("lesson_requests", column)
