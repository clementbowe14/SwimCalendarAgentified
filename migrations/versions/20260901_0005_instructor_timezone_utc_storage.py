"""instructor timezone and UTC schedule storage

Revision ID: 20260901_0005
Revises: 20260830_0004
Create Date: 2026-09-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0005"
down_revision: Union[str, None] = "20260830_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TIMEZONE = "America/Chicago"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")

    inspector = sa.inspect(bind)
    instructor_columns = {column["name"] for column in inspector.get_columns("instructors")}
    if "timezone" not in instructor_columns:
        op.add_column(
            "instructors",
            sa.Column("timezone", sa.String(), nullable=True, server_default=DEFAULT_TIMEZONE),
        )
    op.execute(
        f"""
        UPDATE instructors
        SET timezone = '{DEFAULT_TIMEZONE}'
        WHERE timezone IS NULL OR timezone = ''
        """
    )
    op.alter_column("instructors", "timezone", nullable=False, server_default=DEFAULT_TIMEZONE)

    _drop_overlap_constraint()
    _convert_legacy_local_schedule_to_utc()
    op.execute("DELETE FROM route_estimates")
    _install_overlap_constraint()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("This backend migration targets PostgreSQL. Set SWIM_DATABASE_URL to a PostgreSQL database.")

    _drop_overlap_constraint()
    _convert_utc_schedule_to_legacy_local()
    _install_overlap_constraint()
    op.drop_column("instructors", "timezone")


def _drop_overlap_constraint() -> None:
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS exclude_appointments_active_overlap")


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


def _convert_legacy_local_schedule_to_utc() -> None:
    op.execute(
        """
        UPDATE appointments AS a
        SET
            start_time = (a.start_time AT TIME ZONE i.timezone) AT TIME ZONE 'UTC',
            end_time = (a.end_time AT TIME ZONE i.timezone) AT TIME ZONE 'UTC'
        FROM instructors AS i
        WHERE a.instructor_id = i.id
        """
    )
    op.execute(
        """
        UPDATE lesson_requests AS lr
        SET
            slot_start = (lr.slot_start AT TIME ZONE i.timezone) AT TIME ZONE 'UTC',
            slot_end = (lr.slot_end AT TIME ZONE i.timezone) AT TIME ZONE 'UTC'
        FROM instructors AS i
        WHERE lr.instructor_id = i.id
        """
    )
    op.execute(
        """
        UPDATE invite_slot_offers AS iso
        SET
            slot_start = (iso.slot_start AT TIME ZONE i.timezone) AT TIME ZONE 'UTC',
            slot_end = (iso.slot_end AT TIME ZONE i.timezone) AT TIME ZONE 'UTC'
        FROM instructors AS i
        WHERE iso.instructor_id = i.id
        """
    )


def _convert_utc_schedule_to_legacy_local() -> None:
    op.execute(
        """
        UPDATE appointments AS a
        SET
            start_time = (a.start_time AT TIME ZONE 'UTC') AT TIME ZONE i.timezone,
            end_time = (a.end_time AT TIME ZONE 'UTC') AT TIME ZONE i.timezone
        FROM instructors AS i
        WHERE a.instructor_id = i.id
        """
    )
    op.execute(
        """
        UPDATE lesson_requests AS lr
        SET
            slot_start = (lr.slot_start AT TIME ZONE 'UTC') AT TIME ZONE i.timezone,
            slot_end = (lr.slot_end AT TIME ZONE 'UTC') AT TIME ZONE i.timezone
        FROM instructors AS i
        WHERE lr.instructor_id = i.id
        """
    )
    op.execute(
        """
        UPDATE invite_slot_offers AS iso
        SET
            slot_start = (iso.slot_start AT TIME ZONE 'UTC') AT TIME ZONE i.timezone,
            slot_end = (iso.slot_end AT TIME ZONE 'UTC') AT TIME ZONE i.timezone
        FROM instructors AS i
        WHERE iso.instructor_id = i.id
        """
    )
