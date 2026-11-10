"""Add canonical client profile persistence.

Revision ID: 20261110_0013
Revises: 20261110_0012
Create Date: 2026-11-10
"""

from alembic import op
import sqlalchemy as sa


revision = "20261110_0013"
down_revision = "20261110_0012"
branch_labels = None
depends_on = None


_CLIENT_CHECKS = (
    "ck_clients_name_nonblank",
    "ck_clients_status",
    "ck_clients_deactivation_state",
    "ck_clients_location_complete",
    "ck_clients_location_coordinates",
    "ck_clients_location_mode",
    "ck_clients_client_location_required",
    "ck_clients_default_duration",
    "ck_clients_revision_positive",
)


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)
    _lock_profile_tables(include_availability=False)
    _preflight_upgrade(bind)

    op.add_column(
        "clients",
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "clients",
        sa.Column(
            "location_mode",
            sa.String(32),
            nullable=True,
            server_default=sa.text("'service_location'"),
        ),
    )
    op.add_column(
        "clients",
        sa.Column(
            "default_duration_mins",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("45"),
        ),
    )
    op.add_column(
        "clients",
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "clients",
        sa.Column("deactivated_at", sa.DateTime(), nullable=True),
    )

    op.execute(
        """
        UPDATE clients AS client
        SET location_mode = CASE
            WHEN client.default_location_address IS NULL THEN 'service_location'
            WHEN client.default_location_address = instructor.service_address
             AND client.default_lat = instructor.service_lat
             AND client.default_lng = instructor.service_lng
                THEN 'service_location'
            ELSE 'client_location'
        END,
        default_duration_mins = COALESCE(instructor.default_duration_mins, 45),
        deactivated_at = CASE
            WHEN client.status = 'deactivated' THEN client.updated_at
            ELSE NULL
        END
        FROM instructors AS instructor
        WHERE instructor.id = client.instructor_id
        """
    )

    op.alter_column(
        "clients",
        "location_mode",
        existing_type=sa.String(32),
        nullable=False,
        server_default=sa.text("'service_location'"),
    )
    op.alter_column(
        "clients",
        "default_duration_mins",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("45"),
    )

    op.create_unique_constraint(
        "uq_clients_id_instructor_id",
        "clients",
        ["id", "instructor_id"],
    )
    op.create_check_constraint(
        "ck_clients_name_nonblank",
        "clients",
        "name ~ '[^[:space:]]'",
    )
    op.create_check_constraint(
        "ck_clients_status",
        "clients",
        "status IN ('active', 'deactivated')",
    )
    op.create_check_constraint(
        "ck_clients_deactivation_state",
        "clients",
        "(status = 'active' AND deactivated_at IS NULL) OR "
        "(status = 'deactivated' AND deactivated_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_clients_location_complete",
        "clients",
        "(default_location_address IS NULL AND default_lat IS NULL AND default_lng IS NULL) OR "
        "(default_location_address IS NOT NULL "
        "AND default_location_address ~ '[^[:space:]]' "
        "AND default_lat IS NOT NULL AND default_lng IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_clients_location_coordinates",
        "clients",
        "(default_lat IS NULL OR default_lat BETWEEN -90 AND 90) AND "
        "(default_lng IS NULL OR default_lng BETWEEN -180 AND 180)",
    )
    op.create_check_constraint(
        "ck_clients_location_mode",
        "clients",
        "location_mode IN ('client_location', 'service_location')",
    )
    op.create_check_constraint(
        "ck_clients_client_location_required",
        "clients",
        "location_mode <> 'client_location' OR default_location_address IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_clients_default_duration",
        "clients",
        "default_duration_mins BETWEEN 15 AND 240 "
        "AND default_duration_mins % 15 = 0",
    )
    op.create_check_constraint(
        "ck_clients_revision_positive",
        "clients",
        "revision >= 1",
    )
    op.create_index(
        "ix_clients_active_instructor_name",
        "clients",
        ["instructor_id", sa.text("lower(name)"), "id"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "client_availability_windows",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("instructor_id", sa.String(), nullable=False),
        sa.Column("day_mask", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id", "instructor_id"],
            ["clients.id", "clients.instructor_id"],
            name="fk_client_availability_windows_client_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "day_mask BETWEEN 1 AND 127",
            name="ck_client_availability_windows_day_mask",
        ),
        sa.CheckConstraint(
            "end_time > start_time",
            name="ck_client_availability_windows_time_order",
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_client_availability_windows_position",
        ),
    )
    op.create_index(
        "ix_client_availability_windows_tenant_client_position",
        "client_availability_windows",
        ["instructor_id", "client_id", "position", "id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)
    _lock_profile_tables(include_availability=True)
    _preflight_downgrade(bind)

    op.drop_index(
        "ix_client_availability_windows_tenant_client_position",
        table_name="client_availability_windows",
    )
    op.drop_table("client_availability_windows")
    op.drop_index("ix_clients_active_instructor_name", table_name="clients")
    for check_name in reversed(_CLIENT_CHECKS):
        op.drop_constraint(check_name, "clients", type_="check")
    op.drop_constraint("uq_clients_id_instructor_id", "clients", type_="unique")
    op.drop_column("clients", "deactivated_at")
    op.drop_column("clients", "revision")
    op.drop_column("clients", "default_duration_mins")
    op.drop_column("clients", "location_mode")
    op.drop_column("clients", "notes")


def _preflight_upgrade(bind) -> None:
    invalid = bind.execute(
        sa.text(
            """
            SELECT id,
                   CASE
                       WHEN name !~ '[^[:space:]]' THEN 'a blank name'
                       WHEN status NOT IN ('active', 'deactivated')
                           THEN 'an unsupported status'
                       WHEN NOT (
                           (default_location_address IS NULL
                            AND default_lat IS NULL
                            AND default_lng IS NULL)
                           OR
                           (default_location_address IS NOT NULL
                            AND default_location_address ~ '[^[:space:]]'
                            AND default_lat IS NOT NULL
                            AND default_lng IS NOT NULL)
                       ) THEN 'a partial or blank location'
                       WHEN default_lat IS NOT NULL
                            AND NOT (default_lat BETWEEN -90 AND 90)
                           THEN 'an invalid latitude'
                       WHEN default_lng IS NOT NULL
                            AND NOT (default_lng BETWEEN -180 AND 180)
                           THEN 'an invalid longitude'
                   END AS reason
            FROM clients
            WHERE name !~ '[^[:space:]]'
               OR status NOT IN ('active', 'deactivated')
               OR NOT (
                   (default_location_address IS NULL
                    AND default_lat IS NULL
                    AND default_lng IS NULL)
                   OR
                   (default_location_address IS NOT NULL
                    AND default_location_address ~ '[^[:space:]]'
                    AND default_lat IS NOT NULL
                    AND default_lng IS NOT NULL)
               )
               OR (default_lat IS NOT NULL AND NOT (default_lat BETWEEN -90 AND 90))
               OR (default_lng IS NOT NULL AND NOT (default_lng BETWEEN -180 AND 180))
            ORDER BY id
            LIMIT 1
            """
        )
    ).mappings().first()
    if invalid is not None:
        raise RuntimeError(
            "Cannot upgrade client profile persistence: legacy client "
            f"{invalid['id']!r} has {invalid['reason']}. Remediate that row first."
        )


def _preflight_downgrade(bind) -> None:
    availability_id = bind.execute(
        sa.text("SELECT id FROM client_availability_windows ORDER BY id LIMIT 1")
    ).scalar_one_or_none()
    if availability_id is not None:
        raise RuntimeError(
            "Cannot downgrade client profile persistence while availability windows exist. "
            "Remove or export them first."
        )

    unsupported = bind.execute(
        sa.text(
            """
            SELECT client.id
            FROM clients AS client
            JOIN instructors AS instructor ON instructor.id = client.instructor_id
            WHERE client.notes <> ''
               OR client.revision <> 1
               OR client.default_duration_mins
                    <> COALESCE(instructor.default_duration_mins, 45)
               OR (
                    client.status = 'deactivated'
                    AND client.deactivated_at IS DISTINCT FROM client.updated_at
               )
               OR (
                    client.location_mode = 'service_location'
                    AND client.default_location_address IS NOT NULL
                    AND NOT (
                        client.default_location_address
                            IS NOT DISTINCT FROM instructor.service_address
                        AND client.default_lat
                            IS NOT DISTINCT FROM instructor.service_lat
                        AND client.default_lng
                            IS NOT DISTINCT FROM instructor.service_lng
                    )
               )
               OR (
                    client.location_mode = 'client_location'
                    AND client.default_location_address
                        IS NOT DISTINCT FROM instructor.service_address
                    AND client.default_lat
                        IS NOT DISTINCT FROM instructor.service_lat
                    AND client.default_lng
                        IS NOT DISTINCT FROM instructor.service_lng
               )
            ORDER BY client.id
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if unsupported is not None:
        raise RuntimeError(
            "Cannot downgrade client profile persistence while a client contains "
            "profile state the prior schema cannot reconstruct."
        )


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "This backend migration targets PostgreSQL. "
            "Set SWIM_DATABASE_URL to a PostgreSQL database."
        )


def _lock_profile_tables(*, include_availability: bool) -> None:
    tables = "instructors, clients"
    if include_availability:
        tables += ", client_availability_windows"
    # A fail-fast lock proves application traffic is quiescent before preflight.
    # NOWAIT also prevents a parent/child lock-order deadlock with an in-flight FK write.
    op.execute(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE NOWAIT")
