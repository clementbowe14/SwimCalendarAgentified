"""Notification ledger and stable calendar UIDs.

Downgrade destructively removes notification history and all calendar UIDs.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260918_0007"
down_revision = "20260906_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("appointments", sa.Column("calendar_uid", sa.String(), nullable=False, server_default=sa.text("gen_random_uuid()::text")))
    op.create_unique_constraint("uq_appointments_calendar_uid", "appointments", ["calendar_uid"])
    op.create_unique_constraint("uq_invite_tokens_tenant", "invite_tokens", ["token", "instructor_id"])
    op.create_unique_constraint("uq_lesson_requests_tenant_invite", "lesson_requests", ["id", "invite_token", "instructor_id"])
    op.create_table(
        "notification_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("instructor_id", sa.String(), sa.ForeignKey("instructors.id"), nullable=False),
        sa.Column("invite_token", sa.String(), nullable=False),
        sa.Column("request_id", sa.String()),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("logical_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("recipient", sa.String()),
        sa.Column("template_version", sa.String(), nullable=False),
        sa.Column("template_context", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_token", sa.String()),
        sa.Column("failure_code", sa.String()),
        sa.Column("provider_message_id", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invite_token", "instructor_id"], ["invite_tokens.token", "invite_tokens.instructor_id"], name="fk_notifications_invite_tenant"),
        sa.ForeignKeyConstraint(["request_id", "invite_token", "instructor_id"], ["lesson_requests.id", "lesson_requests.invite_token", "lesson_requests.instructor_id"], name="fk_notifications_request_tenant"),
        sa.UniqueConstraint("instructor_id", "logical_key", "channel", name="uq_notifications_channel"),
        sa.CheckConstraint("kind in ('invite', 'confirmation')", name="ck_notifications_kind"),
        sa.CheckConstraint("channel in ('sms', 'email')", name="ck_notifications_channel"),
        sa.CheckConstraint("status in ('queued', 'processing', 'accepted', 'delivered', 'failed', 'skipped')", name="ck_notifications_status"),
        sa.CheckConstraint("attempt_count between 0 and 5", name="ck_notifications_attempts"),
        sa.CheckConstraint("(kind = 'confirmation' AND request_id IS NOT NULL) OR (kind = 'invite' AND request_id IS NULL)", name="ck_notifications_request_kind"),
    )
    op.create_index("ix_notifications_due", "notification_events", ["status", "next_attempt_at", "lease_expires_at"])
    op.create_index("ix_notifications_request", "notification_events", ["request_id"])


def downgrade():
    op.drop_table("notification_events")
    op.drop_constraint("uq_lesson_requests_tenant_invite", "lesson_requests", type_="unique")
    op.drop_constraint("uq_invite_tokens_tenant", "invite_tokens", type_="unique")
    op.drop_constraint("uq_appointments_calendar_uid", "appointments", type_="unique")
    op.drop_column("appointments", "calendar_uid")
