"""Add provider-managed schedule blockers.

Revision ID: 20261110_0012
Revises: 20261109_0011
Create Date: 2026-11-10
"""

from alembic import op
import sqlalchemy as sa


revision = "20261110_0012"
down_revision = "20261109_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.add_column("appointments", sa.Column("blocker_kind", sa.String(20), nullable=True))
    op.add_column("appointments", sa.Column("blocker_create_payload", sa.JSON(), nullable=True))
    op.add_column(
        "appointments",
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.alter_column("appointments", "client_id", existing_type=sa.String(), nullable=True)
    op.alter_column("appointments", "location_address", existing_type=sa.String(), nullable=True)
    op.alter_column("appointments", "lat", existing_type=sa.Float(), nullable=True)
    op.alter_column("appointments", "lng", existing_type=sa.Float(), nullable=True)

    # Normalize legacy break rows before installing the stronger shape checks.
    op.execute(
        """
        UPDATE appointments
        SET event_type = 'block'
        WHERE is_break IS TRUE
        """
    )
    op.execute(
        """
        UPDATE appointments
        SET blocker_kind = 'break',
            client_id = NULL,
            request_id = NULL,
            is_break = TRUE
        WHERE event_type = 'block'
        """
    )
    op.execute(
        """
        UPDATE appointments
        SET is_break = FALSE
        WHERE event_type <> 'block' OR is_break IS NULL
        """
    )

    op.alter_column(
        "appointments",
        "is_break",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )

    op.create_check_constraint(
        "ck_appointments_blocker_kind",
        "appointments",
        "blocker_kind IS NULL OR blocker_kind IN ('break', 'personal', 'unavailable')",
    )
    op.create_check_constraint(
        "ck_appointments_revision_positive",
        "appointments",
        "revision >= 1",
    )
    op.create_check_constraint(
        "ck_appointments_location_complete",
        "appointments",
        "(location_address IS NULL AND lat IS NULL AND lng IS NULL) OR "
        "(location_address IS NOT NULL AND btrim(location_address) <> '' "
        "AND lat IS NOT NULL AND lng IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_appointments_location_coordinates",
        "appointments",
        "(lat IS NULL OR lat BETWEEN -90 AND 90) AND "
        "(lng IS NULL OR lng BETWEEN -180 AND 180)",
    )
    op.create_check_constraint(
        "ck_appointments_blocker_shape",
        "appointments",
        "(event_type = 'block' AND blocker_kind IS NOT NULL "
        "AND client_id IS NULL AND request_id IS NULL) OR "
        "(event_type <> 'block' AND blocker_kind IS NULL "
        "AND (client_id IS NOT NULL OR status = 'cancelled') "
        "AND location_address IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_appointments_break_consistency",
        "appointments",
        "is_break = (event_type = 'block' AND blocker_kind = 'break')",
    )
    op.create_check_constraint(
        "ck_appointments_unavailable_locationless",
        "appointments",
        "blocker_kind IS DISTINCT FROM 'unavailable' OR location_address IS NULL",
    )
    op.create_check_constraint(
        "ck_appointments_blocker_create_payload",
        "appointments",
        "blocker_create_payload IS NULL OR event_type = 'block'",
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    unsupported = bind.execute(
        sa.text(
            """
            SELECT id
            FROM appointments
            WHERE event_type = 'block'
              AND (
                  blocker_kind IS DISTINCT FROM 'break'
                  OR location_address IS NULL
                  OR lat IS NULL
                  OR lng IS NULL
              )
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if unsupported is not None:
        raise RuntimeError(
            "Cannot downgrade provider blockers while personal, unavailable, "
            "or locationless blockers exist. Remove those blockers first."
        )

    clientless_history = bind.execute(
        sa.text(
            """
            SELECT id
            FROM appointments
            WHERE event_type <> 'block'
              AND client_id IS NULL
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if clientless_history is not None:
        raise RuntimeError(
            "Cannot downgrade provider blockers while clientless historical "
            "appointments exist. Restore their client references first."
        )

    op.drop_constraint("ck_appointments_blocker_create_payload", "appointments", type_="check")
    op.drop_constraint("ck_appointments_unavailable_locationless", "appointments", type_="check")
    op.drop_constraint("ck_appointments_break_consistency", "appointments", type_="check")
    op.drop_constraint("ck_appointments_blocker_shape", "appointments", type_="check")
    op.drop_constraint("ck_appointments_location_coordinates", "appointments", type_="check")
    op.drop_constraint("ck_appointments_location_complete", "appointments", type_="check")
    op.drop_constraint("ck_appointments_revision_positive", "appointments", type_="check")
    op.drop_constraint("ck_appointments_blocker_kind", "appointments", type_="check")

    op.execute(
        """
        UPDATE appointments
        SET client_id = 'instructor_break', is_break = TRUE
        WHERE event_type = 'block'
        """
    )
    op.alter_column("appointments", "lng", existing_type=sa.Float(), nullable=False)
    op.alter_column("appointments", "lat", existing_type=sa.Float(), nullable=False)
    op.alter_column("appointments", "location_address", existing_type=sa.String(), nullable=False)
    op.alter_column("appointments", "client_id", existing_type=sa.String(), nullable=False)
    op.alter_column(
        "appointments",
        "is_break",
        existing_type=sa.Boolean(),
        nullable=True,
        server_default=None,
    )
    op.drop_column("appointments", "revision")
    op.drop_column("appointments", "blocker_create_payload")
    op.drop_column("appointments", "blocker_kind")


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "This backend migration targets PostgreSQL. "
            "Set SWIM_DATABASE_URL to a PostgreSQL database."
        )
