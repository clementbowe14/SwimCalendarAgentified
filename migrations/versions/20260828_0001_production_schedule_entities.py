"""production schedule entities

Revision ID: 20260828_0001
Revises:
Create Date: 2026-08-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")
    inspector = sa.inspect(bind)

    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    if not inspector.has_table("instructors"):
        op.create_table(
            "instructors",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("main_address", sa.String(), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("work_start_time", sa.String(), nullable=True),
            sa.Column("work_end_time", sa.String(), nullable=True),
        )

    if inspector.has_table("appointments"):
        _add_missing_appointment_columns(inspector)
    else:
        op.create_table(
            "appointments",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("instructor_id", sa.String(), sa.ForeignKey("instructors.id"), nullable=False),
            sa.Column("client_id", sa.String(), nullable=False),
            sa.Column("request_id", sa.String(), nullable=True),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("start_time", sa.DateTime(), nullable=False),
            sa.Column("end_time", sa.DateTime(), nullable=False),
            sa.Column("location_address", sa.String(), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("is_break", sa.Boolean(), nullable=True),
            sa.Column("event_type", sa.String(), nullable=False, server_default="lesson"),
            sa.Column("status", sa.String(), nullable=False, server_default="scheduled"),
            sa.Column("idempotency_key", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("cancelled_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint("end_time > start_time", name="ck_appointments_end_after_start"),
            sa.CheckConstraint("event_type in ('lesson', 'block', 'hold')", name="ck_appointments_event_type"),
            sa.CheckConstraint("status in ('scheduled', 'hold', 'cancelled', 'completed')", name="ck_appointments_status"),
        )

    if not inspector.has_table("clients"):
        op.create_table(
            "clients",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("instructor_id", sa.String(), sa.ForeignKey("instructors.id"), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("email", sa.String(), nullable=True),
            sa.Column("phone", sa.String(), nullable=True),
            sa.Column("default_location_address", sa.String(), nullable=True),
            sa.Column("default_lat", sa.Float(), nullable=True),
            sa.Column("default_lng", sa.Float(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )

    if not inspector.has_table("invite_tokens"):
        op.create_table(
            "invite_tokens",
            sa.Column("token", sa.String(), primary_key=True),
            sa.Column("instructor_id", sa.String(), sa.ForeignKey("instructors.id"), nullable=False),
            sa.Column("client_id", sa.String(), sa.ForeignKey("clients.id"), nullable=True),
            sa.Column("business_name", sa.String(), nullable=False),
            sa.Column("location_label", sa.String(), nullable=False),
            sa.Column("preferred_duration_mins", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("allowed_durations_mins", sa.Text(), nullable=False, server_default="30,45,60"),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )

    if not inspector.has_table("lesson_requests"):
        op.create_table(
            "lesson_requests",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("request_token", sa.String(), nullable=False),
            sa.Column("invite_token", sa.String(), sa.ForeignKey("invite_tokens.token"), nullable=False),
            sa.Column("instructor_id", sa.String(), sa.ForeignKey("instructors.id"), nullable=False),
            sa.Column("client_id", sa.String(), sa.ForeignKey("clients.id"), nullable=True),
            sa.Column("client_name", sa.String(), nullable=False),
            sa.Column("client_email", sa.String(), nullable=True),
            sa.Column("client_phone", sa.String(), nullable=True),
            sa.Column("client_address", sa.String(), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("requested_date", sa.Date(), nullable=False),
            sa.Column("slot_start", sa.DateTime(), nullable=False),
            sa.Column("slot_end", sa.DateTime(), nullable=False),
            sa.Column("duration_mins", sa.Integer(), nullable=False),
            sa.Column("travel_time_mins", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("buffer_mins", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("distance_miles", sa.Float(), nullable=False, server_default="0"),
            sa.Column("location_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.CheckConstraint("slot_end > slot_start", name="ck_lesson_requests_end_after_start"),
            sa.CheckConstraint("status in ('pending', 'accepted', 'declined', 'cancelled')", name="ck_lesson_requests_status"),
            sa.CheckConstraint("location_type in ('home', 'facility')", name="ck_lesson_requests_location_type"),
        )

    _create_appointment_request_fk()
    _create_indexes()
    _install_overlap_constraint()
    _backfill_appointment_event_types()


def downgrade() -> None:
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS exclude_appointments_active_overlap")
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS fk_appointments_request_id_lesson_requests")
    op.drop_table("lesson_requests")
    op.drop_table("invite_tokens")
    op.drop_table("clients")


def _add_missing_appointment_columns(inspector: sa.Inspector) -> None:
    existing = {column["name"] for column in inspector.get_columns("appointments")}
    columns = {
        "request_id": sa.Column("request_id", sa.String(), nullable=True),
        "event_type": sa.Column("event_type", sa.String(), nullable=False, server_default="lesson"),
        "status": sa.Column("status", sa.String(), nullable=False, server_default="scheduled"),
        "idempotency_key": sa.Column("idempotency_key", sa.String(), nullable=True),
        "created_at": sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        "updated_at": sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        "cancelled_at": sa.Column("cancelled_at", sa.DateTime(), nullable=True),
    }
    for name, column in columns.items():
        if name not in existing:
            op.add_column("appointments", column)


def _create_appointment_request_fk() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'fk_appointments_request_id_lesson_requests'
            ) THEN
                ALTER TABLE appointments
                ADD CONSTRAINT fk_appointments_request_id_lesson_requests
                FOREIGN KEY (request_id) REFERENCES lesson_requests(id);
            END IF;
        END $$;
        """
    )


def _create_indexes() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_appointments_instructor_window ON appointments (instructor_id, start_time, end_time)")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_idempotency_key
        ON appointments (idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_clients_instructor_name ON clients (instructor_id, name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_invite_tokens_instructor ON invite_tokens (instructor_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_lesson_requests_instructor_status ON lesson_requests (instructor_id, status)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_requests_request_token ON lesson_requests (request_token)")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_requests_idempotency_key
        ON lesson_requests (idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )


def _install_overlap_constraint() -> None:
    op.execute(
        """
        ALTER TABLE appointments
        ADD CONSTRAINT exclude_appointments_active_overlap
        EXCLUDE USING gist (
            instructor_id WITH =,
            tsrange(start_time, end_time, '[)') WITH &&
        )
        WHERE (status IN ('scheduled', 'hold') AND cancelled_at IS NULL)
        """
    )


def _backfill_appointment_event_types() -> None:
    op.execute(
        """
        UPDATE appointments
        SET event_type = CASE WHEN is_break IS TRUE THEN 'block' ELSE 'lesson' END
        WHERE event_type IS NULL OR event_type = 'lesson'
        """
    )
