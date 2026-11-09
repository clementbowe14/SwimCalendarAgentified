"""Add authoritative provider configuration and weekly working hours.

Revision ID: 20261109_0010
Revises: 20261109_0009
Create Date: 2026-11-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20261109_0010"
down_revision = "20261109_0009"
branch_labels = None
depends_on = None


_TIME_PATTERN = r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)
    _preflight_upgrade_legacy_lengths(bind)

    op.add_column(
        "instructors",
        sa.Column("business_name", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("base_address", sa.String(length=300), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("base_lat", sa.Float(), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("base_lng", sa.Float(), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("service_address", sa.String(length=300), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("service_lat", sa.Float(), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("service_lng", sa.Float(), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("service_name", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "instructors",
        sa.Column("default_duration_mins", sa.Integer(), nullable=True),
    )

    op.create_check_constraint(
        "ck_instructors_base_location_complete",
        "instructors",
        """
        (
            base_address IS NULL
            AND base_lat IS NULL
            AND base_lng IS NULL
        )
        OR
        (
            base_address IS NOT NULL
            AND btrim(base_address) <> ''
            AND base_lat IS NOT NULL
            AND base_lng IS NOT NULL
        )
        """,
    )
    op.create_check_constraint(
        "ck_instructors_base_coordinates",
        "instructors",
        """
        (base_lat IS NULL OR base_lat BETWEEN -90 AND 90)
        AND (base_lng IS NULL OR base_lng BETWEEN -180 AND 180)
        """,
    )
    op.create_check_constraint(
        "ck_instructors_service_location_complete",
        "instructors",
        """
        (
            service_address IS NULL
            AND service_lat IS NULL
            AND service_lng IS NULL
        )
        OR
        (
            service_address IS NOT NULL
            AND btrim(service_address) <> ''
            AND service_lat IS NOT NULL
            AND service_lng IS NOT NULL
        )
        """,
    )
    op.create_check_constraint(
        "ck_instructors_service_coordinates",
        "instructors",
        """
        (service_lat IS NULL OR service_lat BETWEEN -90 AND 90)
        AND (service_lng IS NULL OR service_lng BETWEEN -180 AND 180)
        """,
    )
    op.create_check_constraint(
        "ck_instructors_default_duration",
        "instructors",
        """
        default_duration_mins IS NULL
        OR (
            default_duration_mins BETWEEN 15 AND 240
            AND default_duration_mins % 15 = 0
        )
        """,
    )

    op.create_table(
        "provider_working_hours",
        sa.Column("instructor_id", sa.String(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "weekday BETWEEN 0 AND 6",
            name="ck_provider_working_hours_weekday",
        ),
        sa.CheckConstraint(
            """
            (
                enabled IS TRUE
                AND start_time IS NOT NULL
                AND end_time IS NOT NULL
                AND end_time > start_time
            )
            OR
            (
                enabled IS FALSE
                AND start_time IS NULL
                AND end_time IS NULL
            )
            """,
            name="ck_provider_working_hours_state",
        ),
        sa.ForeignKeyConstraint(
            ["instructor_id"],
            ["instructors.id"],
            name="fk_provider_working_hours_instructor_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "instructor_id",
            "weekday",
            name="pk_provider_working_hours",
        ),
    )

    bind.execute(
        sa.text(
            """
            UPDATE instructors
            SET
                base_address = main_address,
                base_lat = lat,
                base_lng = lng,
                service_address = main_address,
                service_lat = lat,
                service_lng = lng,
                service_name = 'Swim lesson',
                default_duration_mins = 60
            """
        )
    )
    bind.execute(
        sa.text(
            """
            WITH normalized_invites AS (
                SELECT
                    instructor_id,
                    NULLIF(btrim(business_name), '') AS business_name,
                    preferred_duration_mins,
                    created_at,
                    token
                FROM invite_tokens
            ),
            ranked_invites AS (
                SELECT
                    instructor_id,
                    preferred_duration_mins,
                    row_number() OVER (
                        PARTITION BY instructor_id
                        ORDER BY created_at DESC, token DESC
                    ) AS invite_rank
                FROM normalized_invites
            ),
            ranked_business_names AS (
                SELECT
                    instructor_id,
                    business_name,
                    row_number() OVER (
                        PARTITION BY instructor_id
                        ORDER BY created_at DESC, token DESC
                    ) AS business_name_rank
                FROM normalized_invites
                WHERE business_name IS NOT NULL
            )
            UPDATE instructors AS instructors_to_update
            SET
                business_name = ranked_business_names.business_name,
                default_duration_mins = CASE
                    WHEN ranked_invites.preferred_duration_mins BETWEEN 15 AND 240
                         AND ranked_invites.preferred_duration_mins % 15 = 0
                    THEN ranked_invites.preferred_duration_mins
                    ELSE 60
                END
            FROM ranked_invites
            LEFT JOIN ranked_business_names
                ON ranked_business_names.instructor_id = ranked_invites.instructor_id
               AND ranked_business_names.business_name_rank = 1
            WHERE ranked_invites.invite_rank = 1
              AND ranked_invites.instructor_id = instructors_to_update.id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            WITH parsed_hours AS (
                SELECT
                    id AS instructor_id,
                    CASE
                        WHEN work_start_time ~ :time_pattern
                        THEN work_start_time::time
                        ELSE NULL
                    END AS start_time,
                    CASE
                        WHEN work_end_time ~ :time_pattern
                        THEN work_end_time::time
                        ELSE NULL
                    END AS end_time
                FROM instructors
            )
            INSERT INTO provider_working_hours (
                instructor_id,
                weekday,
                enabled,
                start_time,
                end_time
            )
            SELECT
                parsed_hours.instructor_id,
                weekdays.weekday,
                true,
                parsed_hours.start_time,
                parsed_hours.end_time
            FROM parsed_hours
            CROSS JOIN generate_series(0, 6) AS weekdays(weekday)
            WHERE parsed_hours.start_time IS NOT NULL
              AND parsed_hours.end_time IS NOT NULL
              AND parsed_hours.end_time > parsed_hours.start_time
            """
        ),
        {"time_pattern": _TIME_PATTERN},
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    location_conflict = bind.execute(
        sa.text(
            """
            SELECT id
            FROM instructors
            WHERE base_address IS NULL
               OR base_lat IS NULL
               OR base_lng IS NULL
               OR service_address IS NULL
               OR service_lat IS NULL
               OR service_lng IS NULL
               OR base_address IS DISTINCT FROM service_address
               OR base_lat IS DISTINCT FROM service_lat
               OR base_lng IS DISTINCT FROM service_lng
            ORDER BY id
            LIMIT 1
            """
        )
    ).mappings().first()
    if location_conflict is not None:
        raise RuntimeError(
            "Cannot downgrade provider configuration for instructor "
            f"{location_conflict['id']!r}: base and service locations must both "
            "be complete and identical. Align the locations before retrying."
        )

    hours_conflict = bind.execute(
        sa.text(
            """
            SELECT
                instructors.id,
                count(hours.weekday) AS row_count,
                count(hours.weekday) FILTER (WHERE hours.enabled IS TRUE) AS enabled_count,
                count(DISTINCT (hours.start_time, hours.end_time))
                    FILTER (WHERE hours.enabled IS TRUE) AS interval_count
            FROM instructors
            LEFT JOIN provider_working_hours AS hours
                ON hours.instructor_id = instructors.id
            GROUP BY instructors.id
            HAVING count(hours.weekday) <> 7
                OR count(hours.weekday) FILTER (WHERE hours.enabled IS TRUE) <> 7
                OR count(DISTINCT (hours.start_time, hours.end_time))
                    FILTER (WHERE hours.enabled IS TRUE) <> 1
            ORDER BY instructors.id
            LIMIT 1
            """
        )
    ).mappings().first()
    if hours_conflict is not None:
        raise RuntimeError(
            "Cannot downgrade provider configuration for instructor "
            f"{hours_conflict['id']!r}: all seven weekdays must be enabled with "
            "one identical interval. Normalize working hours before retrying."
        )

    invite_conflict = bind.execute(
        sa.text(
            """
            SELECT
                instructors.id,
                count(invite_tokens.token) AS invite_count
            FROM instructors
            LEFT JOIN invite_tokens
                ON invite_tokens.instructor_id = instructors.id
            GROUP BY
                instructors.id,
                instructors.business_name,
                instructors.default_duration_mins
            HAVING
                (
                    count(invite_tokens.token) = 0
                    AND (
                        instructors.business_name IS NOT NULL
                        OR instructors.default_duration_mins IS NOT NULL
                    )
                )
                OR
                (
                    count(invite_tokens.token) > 0
                    AND instructors.default_duration_mins IS NULL
                )
            ORDER BY instructors.id
            LIMIT 1
            """
        )
    ).mappings().first()
    if invite_conflict is not None:
        raise RuntimeError(
            "Cannot downgrade provider configuration for instructor "
            f"{invite_conflict['id']!r}: business name and default duration "
            "cannot be represented by the existing invite snapshots. Create or "
            "align an invite snapshot before retrying."
        )

    bind.execute(
        sa.text(
            """
            UPDATE instructors AS instructors_to_update
            SET
                main_address = instructors_to_update.service_address,
                lat = instructors_to_update.service_lat,
                lng = instructors_to_update.service_lng,
                work_start_time = to_char(hours.start_time, 'HH24:MI'),
                work_end_time = to_char(hours.end_time, 'HH24:MI')
            FROM provider_working_hours AS hours
            WHERE hours.instructor_id = instructors_to_update.id
              AND hours.weekday = 0
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE invite_tokens
            SET
                business_name = COALESCE(instructors.business_name, ''),
                preferred_duration_mins = instructors.default_duration_mins
            FROM instructors
            WHERE instructors.id = invite_tokens.instructor_id
            """
        )
    )

    op.drop_table("provider_working_hours")
    op.drop_constraint(
        "ck_instructors_default_duration",
        "instructors",
        type_="check",
    )
    op.drop_constraint(
        "ck_instructors_service_coordinates",
        "instructors",
        type_="check",
    )
    op.drop_constraint(
        "ck_instructors_service_location_complete",
        "instructors",
        type_="check",
    )
    op.drop_constraint(
        "ck_instructors_base_coordinates",
        "instructors",
        type_="check",
    )
    op.drop_constraint(
        "ck_instructors_base_location_complete",
        "instructors",
        type_="check",
    )
    op.drop_column("instructors", "default_duration_mins")
    op.drop_column("instructors", "service_name")
    op.drop_column("instructors", "service_lng")
    op.drop_column("instructors", "service_lat")
    op.drop_column("instructors", "service_address")
    op.drop_column("instructors", "base_lng")
    op.drop_column("instructors", "base_lat")
    op.drop_column("instructors", "base_address")
    op.drop_column("instructors", "business_name")


def _require_postgresql(bind: sa.engine.Connection) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Provider configuration storage requires PostgreSQL.")


def _preflight_upgrade_legacy_lengths(bind: sa.engine.Connection) -> None:
    conflicts = bind.execute(
        sa.text(
            """
            WITH ranked_business_names AS (
                SELECT
                    instructor_id AS provider_id,
                    btrim(business_name) AS normalized_business_name,
                    row_number() OVER (
                        PARTITION BY instructor_id
                        ORDER BY created_at DESC, token DESC
                    ) AS business_name_rank
                FROM invite_tokens
                WHERE NULLIF(btrim(business_name), '') IS NOT NULL
            ),
            preflight_conflicts AS (
                SELECT
                    id AS provider_id,
                    'LEGACY_MAIN_ADDRESS_TOO_LONG' AS reason_code
                FROM instructors
                WHERE char_length(main_address) > 300

                UNION ALL

                SELECT
                    provider_id,
                    'LEGACY_BUSINESS_NAME_TOO_LONG' AS reason_code
                FROM ranked_business_names
                WHERE business_name_rank = 1
                  AND char_length(normalized_business_name) > 160
            )
            SELECT provider_id, reason_code
            FROM preflight_conflicts
            ORDER BY provider_id, reason_code
            """
        )
    ).mappings().all()
    if not conflicts:
        return

    affected_providers = ", ".join(
        f"provider_id={conflict['provider_id']},reason_code={conflict['reason_code']}"
        for conflict in conflicts
    )
    raise RuntimeError(
        "Provider configuration upgrade preflight failed before schema mutation. "
        f"Affected providers: {affected_providers}. "
        "For LEGACY_MAIN_ADDRESS_TOO_LONG, correct instructors.main_address to "
        "300 characters or fewer without breaking its stored coordinate pairing. "
        "For LEGACY_BUSINESS_NAME_TOO_LONG, correct the newest nonblank "
        "invite_tokens.business_name to 160 characters or fewer. Retry the "
        "upgrade after remediation; no legacy values were truncated."
    )
